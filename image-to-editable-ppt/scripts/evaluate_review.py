from __future__ import annotations

import argparse
from collections import Counter
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from agent_common import SCHEMA_DIR
from asset_common import AssetError, atomic_write_json, failure, load_contract, log_event, sha256_file, success
from schema_utils import validate_schema, validate_semantics


COMPONENT = "evaluate_review"
DIMENSIONS = ["content_accuracy", "layout_similarity", "typography_similarity", "visual_style_similarity", "asset_quality"]
CATEGORY_DIMENSION = {
    "content": "content_accuracy", "user_requirement": "content_accuracy", "layout": "layout_similarity",
    "typography": "typography_similarity", "style": "visual_style_similarity", "asset": "asset_quality",
}
WEIGHTS = {
    "content_accuracy": Decimal("0.25"), "layout_similarity": Decimal("0.20"),
    "typography_similarity": Decimal("0.15"), "visual_style_similarity": Decimal("0.10"),
    "asset_quality": Decimal("0.10"), "editability": Decimal("0.20"),
}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Deterministically evaluate a visual review report.")
    result.add_argument("--request", type=Path, required=True)
    result.add_argument("--qa-report", type=Path, required=True)
    result.add_argument("--review-report", type=Path, required=True)
    result.add_argument("--output", type=Path, required=True)
    result.add_argument("--run-id", required=True)
    result.add_argument("--iteration", type=int, required=True)
    result.add_argument("--log-file", type=Path)
    result.add_argument("--schema-dir", type=Path, default=SCHEMA_DIR)
    return result


def _score_cap(issues: list[dict[str, Any]], dimension: str) -> tuple[int, str | None]:
    relevant = [issue for issue in issues if CATEGORY_DIMENSION.get(issue["category"]) == dimension]
    severities = Counter(issue["severity"] for issue in relevant)
    if severities["critical"]:
        return 59, "critical_issue_present"
    if severities["major"]:
        return 89, "major_issue_present"
    if severities["minor"] >= 2:
        return 94, "multiple_minor_issues_present"
    if severities["minor"] == 1:
        return 97, "minor_issue_present"
    return 100, None


def _editability(qa: dict[str, Any]) -> int:
    metrics = qa["metrics"]
    ratio_ok = metrics["editable_text_status"] == "not_applicable" or metrics["editable_text_ratio"] == 1.0
    return 100 if ratio_ok and metrics["missing_required_native_objects"] == 0 and metrics["invalid_text_exemptions"] == 0 and not metrics["missing_element_ids"] else 0


def _issue_counts(issues: list[dict[str, Any]]) -> dict[str, int]:
    result = {"critical_recoverable": 0, "critical_irrecoverable": 0, "critical_unknown": 0, "major": 0, "minor": 0, "suggestion": 0}
    for issue in sorted(issues, key=lambda item: item["id"]):
        if issue["severity"] == "critical":
            result[f"critical_{issue['recoverability']}"] += 1
        else:
            result[issue["severity"]] += 1
    return result


def _policy(
    request: dict[str, Any],
    review: dict[str, Any],
    scores: dict[str, int],
    failed_visual_checks: list[str],
) -> tuple[str, list[str]]:
    issues = review["issues"]
    current = review["iteration"]
    remaining = current < request["review_policy"]["max_iterations"]
    critical_or_major = [item for item in issues if item["severity"] in {"critical", "major"}]
    if any(item["recoverability"] == "irrecoverable" for item in critical_or_major):
        return "fail", ["irrecoverable_critical_or_major_issue"]
    if any(item["recoverability"] == "unknown" for item in critical_or_major):
        return ("revise", ["unknown_critical_or_major_issue"]) if remaining else ("fail", ["unknown_issue_at_iteration_limit"])
    if critical_or_major:
        return ("revise", ["recoverable_critical_or_major_issue"]) if remaining else ("fail", ["critical_or_major_issue_at_iteration_limit"])
    if failed_visual_checks:
        return (
            ("revise", ["mandatory_visual_check_failed"])
            if remaining
            else ("fail", ["mandatory_visual_check_failed_at_iteration_limit"])
        )
    policy = request["review_policy"]
    content_ok = scores["content_accuracy"] >= policy["min_content_accuracy"]
    editability_ok = scores["editability"] >= policy["required_editability_score"]
    overall_ok = scores["overall_score"] >= policy["pass_score"]
    if content_ok and editability_ok and overall_ok:
        return "pass", ["all_review_thresholds_met"]
    if remaining:
        reasons = []
        if not content_ok:
            reasons.append("content_threshold_not_met")
        if not editability_ok:
            reasons.append("editability_threshold_not_met")
        if not overall_ok:
            reasons.append("overall_threshold_not_met")
        return "revise", reasons
    if content_ok and editability_ok and policy["warning_floor_score"] <= scores["overall_score"] < policy["pass_score"]:
        return "warning_candidate", ["iteration_limit_reached", "score_within_warning_band"]
    reasons = ["iteration_limit_reached"]
    if not content_ok:
        reasons.append("content_threshold_not_met")
    if not editability_ok:
        reasons.append("editability_threshold_not_met")
    if scores["overall_score"] < policy["warning_floor_score"]:
        reasons.append("score_below_warning_floor")
    return "fail", reasons


def _relation(recommendation: str, policy: str) -> tuple[str, str]:
    if policy == "warning_candidate":
        return "policy_override", "iteration_limit_reached"
    if recommendation == policy:
        return "exact_match", "same_decision"
    order = {"pass": 0, "revise": 1, "fail": 2}
    if recommendation not in order or policy not in order:
        return "not_comparable", "decision_spaces_not_comparable"
    if order[policy] > order[recommendation]:
        return "policy_stricter", "deterministic_policy_is_more_restrictive"
    return "policy_looser", "deterministic_policy_is_less_restrictive"


def evaluate(request: dict[str, Any], qa: dict[str, Any], review: dict[str, Any], *, request_path: Path, qa_path: Path, review_path: Path) -> dict[str, Any]:
    if qa["status"] != "pass":
        raise AssetError("structural QA must pass before review evaluation", path=str(qa_path), code="structural_gate", exit_code=8)
    if request["task_id"] != review["task_id"] or qa["iteration"] != review["iteration"]:
        raise AssetError("request, QA, and review identity mismatch", path="$", code="iteration_mismatch", exit_code=9)
    if review["review_context"]["request_sha256"] != sha256_file(request_path) or review["review_context"]["qa_report_sha256"] != sha256_file(qa_path):
        raise AssetError("review context hashes do not match evaluation inputs", path="$.review_context", code="hash_conflict", exit_code=9)

    scores = dict(review["scores"])
    adjustments = []
    for dimension in DIMENSIONS:
        cap, reason = _score_cap(review["issues"], dimension)
        raw = scores[dimension]
        if raw > cap:
            scores[dimension] = cap
            adjustments.append({"dimension": dimension, "raw_score": raw, "applied_cap": cap, "computed_score": cap, "reason": reason})
    scores["editability"] = _editability(qa)
    weighted = sum(Decimal(scores[key]) * weight for key, weight in WEIGHTS.items())
    scores["overall_score"] = int(weighted.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    failed_visual_checks = sorted(
        name
        for name, check in review["mandatory_visual_checks"].items()
        if check["status"] == "fail"
    )
    decision, reasons = _policy(request, review, scores, failed_visual_checks)
    relation, relation_reason = _relation(review["reviewer_recommendation"], decision)
    return {
        "schema_version": "1.3", "task_id": review["task_id"], "iteration": review["iteration"],
        "reviewer_recommendation": review["reviewer_recommendation"], "computed_scores": scores,
        "issue_counts": _issue_counts(review["issues"]), "failed_visual_checks": failed_visual_checks,
        "anchor_consistent": not adjustments,
        "score_adjustments": adjustments, "policy_decision": decision,
        "recommendation_relation": relation, "recommendation_relation_reason": relation_reason,
        "decision_reasons": reasons,
        "inputs": {"review_report_sha256": sha256_file(review_path), "qa_report_sha256": sha256_file(qa_path), "request_sha256": sha256_file(request_path)},
    }


def main() -> int:
    args = parser().parse_args()
    try:
        if args.iteration < 1:
            raise AssetError("iteration must be positive", path="--iteration", code="cli_error", exit_code=2)
        target = args.output.resolve()
        if target.exists():
            raise AssetError("review evaluation already exists", path=str(target), code="output_conflict", exit_code=9)
        request = load_contract("request", args.request, args.schema_dir)
        qa = load_contract("qa_report", args.qa_report, args.schema_dir)
        review = load_contract("review_report", args.review_report, args.schema_dir)
        if review["iteration"] != args.iteration:
            raise AssetError("CLI iteration does not match review", path="--iteration", code="iteration_mismatch", exit_code=9)
        evaluation = evaluate(request, qa, review, request_path=args.request, qa_path=args.qa_report, review_path=args.review_report)
        validate_schema("review_evaluation", evaluation, args.schema_dir)
        validate_semantics("review_evaluation", evaluation)
        atomic_write_json(target, evaluation)
        log_event(args.log_file, level="info", component=COMPONENT, event="completed", message="Review evaluation completed", run_id=args.run_id, iteration=args.iteration, data={"policy_decision": evaluation["policy_decision"]})
        return success(COMPONENT, {"review_evaluation": str(target), "sha256": sha256_file(target), "policy_decision": evaluation["policy_decision"]}, run_id=args.run_id, iteration=args.iteration)
    except Exception as exc:
        log_event(args.log_file, level="error", component=COMPONENT, event="failed", message=str(exc), run_id=args.run_id, iteration=args.iteration, data={"exit_code": getattr(exc, "exit_code", 70)})
        return failure(COMPONENT, exc, run_id=args.run_id, iteration=args.iteration)


if __name__ == "__main__":
    raise SystemExit(main())
