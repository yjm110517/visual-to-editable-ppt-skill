from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any

from asset_common import AssetError, atomic_write_json, failure, load_contract, log_event, sha256_file, success
from iteration_common import append_transition, commit_state, require_under, utc_now
from schema_utils import ContractError, validate_schema, validate_semantics


COMPONENT = "create_delivery_decision"
SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Create a deterministic delivery gate decision.")
    result.add_argument("--work-root", type=Path, required=True)
    result.add_argument("--run-state", type=Path, required=True)
    result.add_argument("--request", type=Path, required=True)
    result.add_argument("--iteration-dir", type=Path, required=True)
    result.add_argument("--output", type=Path, required=True)
    result.add_argument("--schema-dir", type=Path, default=SCHEMA_DIR)
    result.add_argument("--run-id", required=True)
    result.add_argument("--log-file", type=Path)
    return result


def _optional(kind: str, path: Path, schema_dir: Path) -> dict[str, Any] | None:
    return load_contract(kind, path, schema_dir) if path.is_file() else None


def create_decision(args: argparse.Namespace) -> dict[str, Any]:
    work_root = args.work_root.resolve()
    state_path = require_under(args.run_state, work_root)
    request_path = require_under(args.request, work_root)
    iteration = require_under(args.iteration_dir, work_root)
    output = require_under(args.output, work_root, must_exist=False)
    if state_path != work_root / "run_state.json" or request_path != work_root / "request.json" or output != work_root / "delivery_decision.json":
        raise AssetError("state, request, and decision must use canonical work-root paths", code="path_escape")
    if output.exists():
        raise AssetError("delivery decision already exists", path=str(output), code="output_conflict", exit_code=9)
    state = load_contract("run_state", state_path, args.schema_dir)
    request = load_contract("request", request_path, args.schema_dir)
    if state["task_id"] != request["task_id"] or state["request_sha256"] != sha256_file(request_path):
        raise AssetError("state does not match request", code="hash_conflict", exit_code=9)
    if iteration != work_root / "iterations" / f"{state['current_iteration']:02d}":
        raise AssetError("iteration-dir does not match run state", code="iteration_mismatch", exit_code=9)
    qa_path, review_path, evaluation_path = iteration / "qa_report.json", iteration / "review_report.json", iteration / "review_evaluation.json"
    qa = _optional("qa_report", qa_path, args.schema_dir)
    review = _optional("review_report", review_path, args.schema_dir)
    evaluation = _optional("review_evaluation", evaluation_path, args.schema_dir)
    for document in (qa, review, evaluation):
        if document and document.get("iteration") != state["current_iteration"]:
            raise AssetError("report iteration mismatch", code="iteration_mismatch", exit_code=9)
    if review and review["task_id"] != state["task_id"] or evaluation and evaluation["task_id"] != state["task_id"]:
        raise AssetError("report task mismatch", code="task_mismatch", exit_code=9)
    if evaluation:
        if not qa or not review:
            raise AssetError("review evaluation requires QA and review reports", code="missing_input", exit_code=3)
        expected_inputs = {"request_sha256": sha256_file(request_path), "qa_report_sha256": sha256_file(qa_path), "review_report_sha256": sha256_file(review_path)}
        if evaluation["inputs"] != expected_inputs:
            raise AssetError("review evaluation input hashes are stale", code="hash_conflict", exit_code=9)
    base = {
        "schema_version": "1.3", "task_id": state["task_id"], "request_sha256": sha256_file(request_path),
        "qa_report_sha256": sha256_file(qa_path) if qa else None,
        "review_report_sha256": sha256_file(review_path) if review else None,
        "review_evaluation_sha256": sha256_file(evaluation_path) if evaluation else None,
        "timestamp_utc": utc_now(),
    }
    if state["state"] == "review_pass":
        if not qa or qa["status"] != "pass" or not evaluation or evaluation["policy_decision"] != "pass":
            raise AssetError("review_pass state conflicts with reports", code="policy_conflict", exit_code=9)
        decision = {**base, "status": "pass", "accepted_iteration": state["current_iteration"], "warnings": [], "approved_by": "review_policy", "decision_reasons": evaluation["decision_reasons"]}
        target_state = "packaging"
    elif state["state"] == "awaiting_user_acceptance":
        acceptance = state.get("acceptance")
        if not acceptance or acceptance["outcome"] != "accepted" or not evaluation or evaluation["policy_decision"] != "warning_candidate":
            raise AssetError("warning delivery requires matching explicit acceptance", code="delivery_gate", exit_code=10)
        if acceptance["warning_candidate_sha256"] != sha256_file(evaluation_path) or state["pending_decision"]["review_evaluation_sha256"] != sha256_file(evaluation_path):
            raise AssetError("warning acceptance is stale", code="hash_conflict", exit_code=9)
        evidence = {"message_sha256": acceptance["message_sha256"], "warning_candidate_sha256": acceptance["warning_candidate_sha256"], "decision_at_utc": acceptance["decision_at_utc"]}
        decision = {**base, "status": "pass_with_warnings", "accepted_iteration": state["current_iteration"], "warnings": state["pending_decision"]["warnings"], "approved_by": acceptance["actor_type"], "approval": evidence, "decision_reasons": evaluation["decision_reasons"]}
        target_state = "packaging"
    elif state["state"] in {"review_fail", "structural_fail", "failed"}:
        acceptance = state.get("acceptance")
        rejected = acceptance and acceptance["outcome"] == "rejected"
        reasons = evaluation["decision_reasons"] if evaluation else (["warning_candidate_rejected"] if rejected else ["structural_or_orchestrator_failure"])
        decision = {**base, "status": "fail", "accepted_iteration": None, "warnings": state.get("pending_decision", {}).get("warnings", []), "approved_by": acceptance["actor_type"] if rejected else "review_policy", "decision_reasons": reasons}
        if rejected:
            decision["rejection"] = {"message_sha256": acceptance["message_sha256"], "warning_candidate_sha256": acceptance["warning_candidate_sha256"], "decision_at_utc": acceptance["decision_at_utc"]}
        target_state = "failed"
    else:
        raise AssetError("run state is not eligible for a delivery decision", code="delivery_gate", exit_code=10)
    validate_schema("delivery_decision", decision, args.schema_dir)
    validate_semantics("delivery_decision", decision)
    atomic_write_json(output, decision)
    if state["state"] != target_state:
        try:
            updated = append_transition(copy.deepcopy(state), target_state, f"delivery_decision_{decision['status']}", artifact=output, work_root=work_root)
            validate_schema("run_state", updated, args.schema_dir)
            validate_semantics("run_state", updated)
            commit_state(state_path, updated)
        except Exception:
            output.unlink(missing_ok=True)
            raise
    return {"delivery_decision": str(output), "sha256": sha256_file(output), "status": decision["status"], "accepted_iteration": decision["accepted_iteration"]}


def main() -> int:
    args = parser().parse_args()
    try:
        outputs = create_decision(args)
        log_event(args.log_file, level="info", component=COMPONENT, event="completed", message="Delivery decision created", run_id=args.run_id, iteration=outputs["accepted_iteration"], data={"status": outputs["status"]})
        return success(COMPONENT, outputs, run_id=args.run_id, iteration=outputs["accepted_iteration"])
    except (ContractError, json.JSONDecodeError) as caught:
        error = AssetError(str(caught), code="contract_error")
    except Exception as caught:
        error = caught
    log_event(args.log_file, level="error", component=COMPONENT, event="failed", message=str(error), run_id=args.run_id, iteration=None, data={"exit_code": getattr(error, "exit_code", 70)})
    return failure(COMPONENT, error, run_id=args.run_id, iteration=None)


if __name__ == "__main__":
    raise SystemExit(main())
