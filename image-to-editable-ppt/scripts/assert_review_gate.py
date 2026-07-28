from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from agent_common import SCHEMA_DIR, load_call_bundle
from asset_common import AssetError, failure, load_contract, log_event, sha256_file, success
from schema_utils import load_json


COMPONENT = "assert_review_gate"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Assert that independent visual review passed before delivery.")
    result.add_argument("--work-root", type=Path, required=True)
    result.add_argument("--iteration-dir", type=Path, required=True)
    result.add_argument("--run-state", type=Path, required=True)
    result.add_argument("--planner-call-record", type=Path, required=True)
    result.add_argument("--reviewer-call-record", type=Path, required=True)
    result.add_argument("--ppt", type=Path, required=True)
    result.add_argument("--run-id", required=True)
    result.add_argument("--iteration", type=int, required=True)
    result.add_argument("--log-file", type=Path)
    result.add_argument("--schema-dir", type=Path, default=SCHEMA_DIR)
    return result


def _load_record(path: Path, work_root: Path, role: str, schema_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    resolved = path.resolve()
    if resolved.name != "call_record.json":
        raise AssetError(f"{role}-call-record must name call_record.json", path=str(resolved), code="cli_error", exit_code=2)
    manifest = load_json(resolved.parent / "call_manifest.json")
    mode = manifest.get("mode")
    expected_modes = {"planner": {"initial", "revision"}, "reviewer": {"review"}}[role]
    if mode not in expected_modes:
        raise AssetError(f"invalid {role} call mode", path=str(resolved), code="call_bundle")
    manifest, record, _, _ = load_call_bundle(
        resolved.parent,
        work_root=work_root,
        role=role,
        mode=mode,
        schema_dir=schema_dir,
    )
    return manifest, record


def assert_gate(args: argparse.Namespace) -> dict[str, Any]:
    work_root = args.work_root.resolve()
    iteration = args.iteration_dir.resolve()
    if iteration != work_root / "iterations" / f"{args.iteration:02d}":
        raise AssetError("iteration-dir does not match work-root and iteration", path=str(iteration), code="iteration_mismatch", exit_code=9)
    if args.run_state.resolve() != work_root / "run_state.json":
        raise AssetError("run-state must be work-root/run_state.json", path=str(args.run_state), code="path_escape")
    required = ["qa_report.json", "review_report.json", "review_evaluation.json", "rendered_slide.png", "layout.json", "asset_manifest.json"]
    missing = [name for name in required if not (iteration / name).is_file()]
    if missing:
        raise AssetError("visual review gate artifacts are missing: " + ", ".join(missing), path=str(iteration), code="delivery_gate", exit_code=10)

    request_path = work_root / "request.json"
    state = load_contract("run_state", args.run_state, args.schema_dir)
    request = load_contract("request", request_path, args.schema_dir)
    qa = load_contract("qa_report", iteration / "qa_report.json", args.schema_dir)
    review = load_contract("review_report", iteration / "review_report.json", args.schema_dir)
    evaluation = load_contract("review_evaluation", iteration / "review_evaluation.json", args.schema_dir)
    if state["state"] not in {"review_pass", "packaging", "delivered"} or state["current_iteration"] != args.iteration:
        raise AssetError("run state has not reached visual review pass", path=str(args.run_state), code="delivery_gate", exit_code=10)
    if state["task_id"] != request["task_id"] or review["task_id"] != state["task_id"] or evaluation["task_id"] != state["task_id"]:
        raise AssetError("review gate task identities conflict", code="hash_conflict", exit_code=9)
    if qa["status"] != "pass" or evaluation["policy_decision"] != "pass":
        raise AssetError("structural QA and deterministic review policy must both pass", code="delivery_gate", exit_code=10)
    ppt = args.ppt.resolve()
    if ppt.parent != iteration or not ppt.is_file() or ppt.suffix.lower() != ".pptx":
        raise AssetError("ppt must identify the current iteration PPTX", path=str(ppt), code="path_escape")
    expected_qa_provenance = {
        "request_sha256": sha256_file(request_path),
        "source_sha256": sha256_file(work_root / request["source_image"]),
        "layout_sha256": sha256_file(iteration / "layout.json"),
        "crops_sha256": sha256_file(iteration / "crops.json"),
        "asset_manifest_sha256": sha256_file(iteration / "asset_manifest.json"),
        "build_summary_sha256": sha256_file(iteration / "build_summary.json"),
        "ppt_sha256": sha256_file(ppt),
        "render_sha256": sha256_file(iteration / "rendered_slide.png"),
    }
    if any(qa["provenance"][key] != value for key, value in expected_qa_provenance.items()):
        raise AssetError("structural QA provenance is stale", code="hash_conflict", exit_code=9)
    if evaluation["failed_visual_checks"] or any(check["status"] == "fail" for check in review["mandatory_visual_checks"].values()):
        raise AssetError("one or more mandatory visual checks failed", code="delivery_gate", exit_code=10)
    expected_inputs = {
        "request_sha256": sha256_file(request_path),
        "qa_report_sha256": sha256_file(iteration / "qa_report.json"),
        "review_report_sha256": sha256_file(iteration / "review_report.json"),
    }
    if evaluation["inputs"] != expected_inputs:
        raise AssetError("review evaluation input hashes are stale", code="hash_conflict", exit_code=9)
    expected_context = {
        "source_sha256": sha256_file(work_root / request["source_image"]),
        "render_sha256": sha256_file(iteration / "rendered_slide.png"),
        "layout_sha256": sha256_file(iteration / "layout.json"),
        "qa_report_sha256": expected_inputs["qa_report_sha256"],
        "asset_manifest_sha256": sha256_file(iteration / "asset_manifest.json"),
        "request_sha256": expected_inputs["request_sha256"],
    }
    if any(review["review_context"][key] != value for key, value in expected_context.items()):
        raise AssetError("review context hashes are stale", code="hash_conflict", exit_code=9)

    planner_manifest, planner_record = _load_record(args.planner_call_record, work_root, "planner", args.schema_dir)
    reviewer_manifest, reviewer_record = _load_record(args.reviewer_call_record, work_root, "reviewer", args.schema_dir)
    if reviewer_manifest["iteration"] != args.iteration or reviewer_record["context_id"] == planner_record["context_id"]:
        raise AssetError("Planner and Reviewer contexts are not independent for this iteration", code="context_conflict", exit_code=9)
    provenance = review["agent_provenance"]
    for role, record in (("planner", planner_record), ("reviewer", reviewer_record)):
        recorded = provenance[role]
        if recorded["call_id"] != record["call_id"] or recorded["context_id"] != record["context_id"] or recorded["parent_context_id"] is not None:
            raise AssetError(f"{role} provenance does not match its trusted call record", code="hash_conflict", exit_code=9)
    return {
        "visual_review_gate": "pass",
        "eligible_for_delivery": True,
        "iteration": args.iteration,
        "review_report_sha256": expected_inputs["review_report_sha256"],
        "review_evaluation_sha256": sha256_file(iteration / "review_evaluation.json"),
        "ppt_sha256": expected_qa_provenance["ppt_sha256"],
        "planner_context_id": planner_record["context_id"],
        "reviewer_context_id": reviewer_record["context_id"],
    }


def main() -> int:
    args = parser().parse_args()
    try:
        outputs = assert_gate(args)
        log_event(args.log_file, level="info", component=COMPONENT, event="passed", message="Independent visual-review gate passed", run_id=args.run_id, iteration=args.iteration)
        return success(COMPONENT, outputs, run_id=args.run_id, iteration=args.iteration)
    except Exception as exc:
        log_event(args.log_file, level="error", component=COMPONENT, event="failed", message=str(exc), run_id=args.run_id, iteration=args.iteration, data={"exit_code": getattr(exc, "exit_code", 70)})
        return failure(COMPONENT, exc, run_id=args.run_id, iteration=args.iteration)


if __name__ == "__main__":
    raise SystemExit(main())
