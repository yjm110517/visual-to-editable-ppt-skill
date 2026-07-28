from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from agent_common import SCHEMA_DIR, load_call_bundle
from asset_common import AssetError, failure, load_contract, log_event, success
from manage_run_state import advance as advance_run_state
from schema_utils import load_json


COMPONENT = "run_review_checkpoint"
SCRIPT_DIR = Path(__file__).resolve().parent


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Open the mandatory independent visual-review checkpoint.")
    result.add_argument("--work-root", type=Path, required=True)
    result.add_argument("--iteration-dir", type=Path, required=True)
    result.add_argument("--run-state", type=Path, required=True)
    result.add_argument("--planner-call-record", type=Path, required=True)
    result.add_argument("--call-id", required=True)
    result.add_argument("--model-selection-mode", choices=("runtime-default", "explicit", "allowlist"), default="runtime-default")
    result.add_argument("--requested-model")
    result.add_argument("--run-id", required=True)
    result.add_argument("--iteration", type=int, required=True)
    result.add_argument("--log-file", type=Path)
    result.add_argument("--schema-dir", type=Path, default=SCHEMA_DIR)
    return result


def _parse_result(completed: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise AssetError("prepare_agent_call returned invalid JSON", code="invalid_subprocess_result", exit_code=70) from exc
    if completed.returncode != 0 or result.get("status") != "ok":
        error = result.get("error") or {}
        raise AssetError(
            error.get("message") or "Reviewer call package creation failed",
            path=error.get("path", "$"),
            code=error.get("category", "review_checkpoint"),
            exit_code=error.get("exit_code", completed.returncode or 70),
        )
    return result


def open_checkpoint(args: argparse.Namespace) -> dict[str, Any]:
    work_root = args.work_root.resolve()
    iteration = args.iteration_dir.resolve()
    state_path = args.run_state.resolve()
    if iteration != work_root / "iterations" / f"{args.iteration:02d}":
        raise AssetError("iteration-dir does not match work-root and iteration", path=str(iteration), code="iteration_mismatch", exit_code=9)
    if state_path != work_root / "run_state.json":
        raise AssetError("run-state must be work-root/run_state.json", path=str(state_path), code="path_escape")
    state = load_contract("run_state", state_path, args.schema_dir)
    request = load_contract("request", work_root / "request.json", args.schema_dir)
    qa = load_contract("qa_report", iteration / "qa_report.json", args.schema_dir)
    if state["state"] != "structural_pass" or state["current_iteration"] != args.iteration:
        raise AssetError("review checkpoint requires current state structural_pass", path=str(state_path), code="state_conflict", exit_code=9)
    if state["task_id"] != request["task_id"] or qa["status"] != "pass" or qa["iteration"] != args.iteration:
        raise AssetError("request, run state, and structural QA do not identify one passing iteration", code="structural_gate", exit_code=8)

    planner_record_path = args.planner_call_record.resolve()
    if planner_record_path.name != "call_record.json":
        raise AssetError("planner-call-record must name call_record.json", path=str(planner_record_path), code="cli_error", exit_code=2)
    planner_dir = planner_record_path.parent
    planner_manifest = load_json(planner_dir / "call_manifest.json")
    planner_mode = planner_manifest.get("mode")
    if planner_mode not in {"initial", "revision"}:
        raise AssetError("planner-call-record is not a Planner initial or revision call", path=str(planner_record_path), code="call_bundle")
    _, planner_record, _, _ = load_call_bundle(
        planner_dir,
        work_root=work_root,
        role="planner",
        mode=planner_mode,
        schema_dir=args.schema_dir,
    )
    if planner_record["task_id"] != state["task_id"]:
        raise AssetError("Planner call belongs to another task", path=str(planner_record_path), code="call_record", exit_code=9)

    source = (work_root / request["source_image"]).resolve()
    call_dir = work_root / ".agent-calls" / f"{args.iteration:02d}" / "reviewer" / args.call_id
    command = [
        sys.executable,
        str(SCRIPT_DIR / "prepare_agent_call.py"),
        "--role", "reviewer",
        "--mode", "review",
        "--work-root", str(work_root),
        "--request", str(work_root / "request.json"),
        "--source", str(source),
        "--render", str(iteration / "rendered_slide.png"),
        "--layout", str(iteration / "layout.json"),
        "--qa-report", str(iteration / "qa_report.json"),
        "--asset-manifest", str(iteration / "asset_manifest.json"),
        "--iteration", str(args.iteration),
        "--model-selection-mode", args.model_selection_mode,
        "--call-id", args.call_id,
        "--output-dir", str(call_dir),
        "--run-id", args.run_id,
        "--schema-dir", str(args.schema_dir),
    ]
    if args.requested_model:
        command.extend(["--requested-model", args.requested_model])
    if args.log_file:
        command.extend(["--log-file", str(args.log_file)])
    prepared = _parse_result(subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False))
    try:
        updated = advance_run_state(
            argparse.Namespace(
                work_root=work_root,
                state=state_path,
                event="review_started",
                artifact=None,
                reason=None,
                schema_dir=args.schema_dir,
                run_id=args.run_id,
                log_file=args.log_file,
            )
        )
    except Exception:
        shutil.rmtree(call_dir, ignore_errors=True)
        raise
    return {
        "call_dir": prepared["outputs"]["call_dir"],
        "call_manifest": prepared["outputs"]["call_manifest"],
        "planner_call_record": str(planner_record_path),
        "run_state": updated["state"],
        "visual_review_status": "pending",
        "deliverable": False,
        "required_next_action": "execute_reviewer_in_fresh_context_then_finalize_and_evaluate",
    }


def main() -> int:
    args = parser().parse_args()
    try:
        outputs = open_checkpoint(args)
        log_event(args.log_file, level="info", component=COMPONENT, event="opened", message="Mandatory visual-review checkpoint opened", run_id=args.run_id, iteration=args.iteration)
        return success(COMPONENT, outputs, run_id=args.run_id, iteration=args.iteration)
    except Exception as exc:
        log_event(args.log_file, level="error", component=COMPONENT, event="failed", message=str(exc), run_id=args.run_id, iteration=args.iteration, data={"exit_code": getattr(exc, "exit_code", 70)})
        return failure(COMPONENT, exc, run_id=args.run_id, iteration=args.iteration)


if __name__ == "__main__":
    raise SystemExit(main())
