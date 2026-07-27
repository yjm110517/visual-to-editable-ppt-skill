from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from asset_common import AssetError, failure, load_contract, log_event, sha256_file, success
from iteration_common import TERMINAL_STATES, append_transition, canonical_message_sha256, commit_state, require_under, utc_now
from schema_utils import ContractError, cross_validate, load_json, validate_schema, validate_semantics


COMPONENT = "manage_run_state"
SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Manage deterministic image-to-editable-PPT run state.")
    commands = result.add_subparsers(dest="command", required=True)

    def add_common(command: argparse.ArgumentParser) -> None:
        command.add_argument("--schema-dir", type=Path, default=SCHEMA_DIR)
        command.add_argument("--run-id", required=True)
        command.add_argument("--log-file", type=Path)

    init = commands.add_parser("init")
    add_common(init)
    init.add_argument("--work-root", type=Path, required=True)
    init.add_argument("--request", type=Path, required=True)
    init.add_argument("--output", type=Path, required=True)
    advance = commands.add_parser("advance")
    add_common(advance)
    advance.add_argument("--work-root", type=Path, required=True)
    advance.add_argument("--state", type=Path, required=True)
    advance.add_argument("--event", choices=("inputs_resolved", "spec_validated", "build_started", "structural_result", "replan_after_structure", "review_started", "review_ready", "evaluation_result", "await_acceptance", "abort"), required=True)
    advance.add_argument("--artifact", type=Path)
    advance.add_argument("--reason")
    response = commands.add_parser("warning-response")
    add_common(response)
    response.add_argument("--work-root", type=Path, required=True)
    response.add_argument("--state", type=Path, required=True)
    response.add_argument("--decision", choices=("accept", "reject"), required=True)
    response.add_argument("--actor-type", choices=("user", "acceptance_role"), required=True)
    response.add_argument("--actor-id")
    response.add_argument("--message-file", type=Path, required=True)
    return result


def _validate_state(state: dict[str, Any], schema_dir: Path) -> None:
    validate_schema("run_state", state, schema_dir)
    validate_semantics("run_state", state)


def _load_state(path: Path, work_root: Path, schema_dir: Path) -> dict[str, Any]:
    expected = work_root / "run_state.json"
    if path.resolve() != expected.resolve():
        raise AssetError("state must be work-root/run_state.json", path=str(path), code="path_escape")
    return load_contract("run_state", require_under(path, work_root), schema_dir)


def _request(work_root: Path, schema_dir: Path) -> tuple[dict[str, Any], Path]:
    path = work_root / "request.json"
    return load_contract("request", require_under(path, work_root), schema_dir), path


def _assert_binding(state: dict[str, Any], request: dict[str, Any], request_path: Path) -> None:
    if state["task_id"] != request["task_id"] or state["request_sha256"] != sha256_file(request_path) or state["max_iterations"] != request["review_policy"]["max_iterations"]:
        raise AssetError("run state does not match the current request", path=str(request_path), code="hash_conflict", exit_code=9)


def _iteration(work_root: Path, number: int) -> Path:
    return work_root / "iterations" / f"{number:02d}"


def initialize(args: argparse.Namespace) -> dict[str, Any]:
    work_root = args.work_root.resolve()
    if args.request.resolve() != work_root / "request.json" or args.output.resolve() != work_root / "run_state.json":
        raise AssetError("request and output must be directly inside work-root", path=str(work_root), code="path_escape")
    if args.output.exists():
        raise AssetError("run state already exists", path=str(args.output), code="output_conflict", exit_code=9)
    request = load_contract("request", require_under(args.request, work_root), args.schema_dir)
    state = {"schema_version": "1.3", "task_id": request["task_id"], "request_sha256": sha256_file(args.request), "state": "input_pending", "current_iteration": 0, "max_iterations": request["review_policy"]["max_iterations"], "history": []}
    _validate_state(state, args.schema_dir)
    commit_state(args.output, state)
    return state


def _require_artifact(args: argparse.Namespace, event: str) -> Path:
    if args.artifact is None:
        raise AssetError(f"{event} requires --artifact", path="--artifact", code="cli_error", exit_code=2)
    return args.artifact


def _load_matching(kind: str, path: Path, state: dict[str, Any], work_root: Path, schema_dir: Path) -> dict[str, Any]:
    path = require_under(path, work_root)
    document = load_contract(kind, path, schema_dir)
    iteration = document.get("iteration", document.get("metadata", {}).get("iteration"))
    if iteration is not None and iteration != state["current_iteration"]:
        raise AssetError("artifact iteration does not match run state", path=str(path), code="iteration_mismatch", exit_code=9)
    if document.get("task_id") and document["task_id"] != state["task_id"]:
        raise AssetError("artifact task does not match run state", path=str(path), code="task_mismatch", exit_code=9)
    return document


def advance(args: argparse.Namespace) -> dict[str, Any]:
    work_root = args.work_root.resolve()
    state_path = require_under(args.state, work_root)
    state = _load_state(state_path, work_root, args.schema_dir)
    request, request_path = _request(work_root, args.schema_dir)
    _assert_binding(state, request, request_path)
    event = args.event
    artifact: Path | None = args.artifact
    target: str
    reason = event
    updated = state
    if event == "inputs_resolved":
        if state["state"] != "input_pending":
            raise AssetError("inputs_resolved requires input_pending", code="state_conflict", exit_code=9)
        artifact = request_path
        updated["current_iteration"] = 1
        target = "planning"
    elif event == "spec_validated":
        if state["state"] != "planning":
            raise AssetError("spec_validated requires planning", code="state_conflict", exit_code=9)
        iteration = _iteration(work_root, state["current_iteration"])
        documents = {kind: load_contract(kind, iteration / name, args.schema_dir) for kind, name in (("layout", "layout.json"), ("crops", "crops.json"), ("asset_manifest", "asset_manifest.json"))}
        if documents["layout"]["metadata"]["iteration"] != state["current_iteration"]:
            raise AssetError("layout iteration does not match run state", code="iteration_mismatch", exit_code=9)
        cross_validate(documents)
        artifact = iteration / "layout.json"
        target = "spec_ready"
    elif event == "build_started":
        if state["state"] != "spec_ready":
            raise AssetError("build_started requires spec_ready", code="state_conflict", exit_code=9)
        target, artifact = "building", None
    elif event == "structural_result":
        if state["state"] != "building":
            raise AssetError("structural_result requires building", code="state_conflict", exit_code=9)
        artifact = _require_artifact(args, event)
        qa = _load_matching("qa_report", artifact, state, work_root, args.schema_dir)
        target = "structural_pass" if qa["status"] == "pass" else "structural_fail"
        reason = f"structural_{qa['status']}"
    elif event == "replan_after_structure":
        if state["state"] != "structural_fail":
            raise AssetError("replan_after_structure requires structural_fail", code="state_conflict", exit_code=9)
        if state["current_iteration"] < state["max_iterations"]:
            updated["current_iteration"] += 1
            target = "planning"
        else:
            target = "failed"
            reason = "structural_failure_at_iteration_limit"
    elif event == "review_started":
        if state["state"] != "structural_pass":
            raise AssetError("review_started requires structural_pass", code="state_conflict", exit_code=9)
        target, artifact = "reviewing", None
    elif event == "review_ready":
        if state["state"] != "reviewing":
            raise AssetError("review_ready requires reviewing", code="state_conflict", exit_code=9)
        artifact = _require_artifact(args, event)
        _load_matching("review_report", artifact, state, work_root, args.schema_dir)
        target = "review_evaluating"
    elif event == "evaluation_result":
        if state["state"] != "review_evaluating":
            raise AssetError("evaluation_result requires review_evaluating", code="state_conflict", exit_code=9)
        artifact = _require_artifact(args, event)
        evaluation = _load_matching("review_evaluation", artifact, state, work_root, args.schema_dir)
        target = {"pass": "review_pass", "revise": "review_revise", "fail": "review_fail", "warning_candidate": "review_warning_candidate"}[evaluation["policy_decision"]]
        reason = f"policy_decision_{evaluation['policy_decision']}"
    elif event == "await_acceptance":
        if state["state"] != "review_warning_candidate" or state["current_iteration"] != state["max_iterations"]:
            raise AssetError("await_acceptance requires a final-iteration warning candidate", code="state_conflict", exit_code=9)
        artifact = _require_artifact(args, event)
        evaluation = _load_matching("review_evaluation", artifact, state, work_root, args.schema_dir)
        if evaluation["policy_decision"] != "warning_candidate":
            raise AssetError("evaluation is not a warning candidate", path=str(artifact), code="policy_conflict", exit_code=9)
        review_path = artifact.parent / "review_report.json"
        review = _load_matching("review_report", review_path, state, work_root, args.schema_dir)
        warnings = sorted(set(review["warnings"] or [item["description"] for item in review["issues"] if item["severity"] in {"minor", "suggestion"}] or evaluation["decision_reasons"]))
        updated["pending_decision"] = {"iteration": state["current_iteration"], "review_evaluation_sha256": sha256_file(artifact), "warnings": warnings}
        target = "awaiting_user_acceptance"
    elif event == "abort":
        if state["state"] in TERMINAL_STATES:
            raise AssetError("terminal state cannot be aborted", code="state_conflict", exit_code=9)
        target = "failed"
        reason = args.reason or "aborted"
    else:
        raise AssetError("unsupported state event", code="cli_error", exit_code=2)
    updated = append_transition(updated, target, reason, artifact=artifact, work_root=work_root)
    _validate_state(updated, args.schema_dir)
    commit_state(state_path, updated)
    return updated


def warning_response(args: argparse.Namespace) -> dict[str, Any]:
    work_root = args.work_root.resolve()
    state_path = require_under(args.state, work_root)
    state = _load_state(state_path, work_root, args.schema_dir)
    request, request_path = _request(work_root, args.schema_dir)
    _assert_binding(state, request, request_path)
    if state["state"] != "awaiting_user_acceptance" or "acceptance" in state:
        raise AssetError("warning response requires an unanswered awaiting state", code="state_conflict", exit_code=9)
    message = args.message_file.resolve()
    digest = canonical_message_sha256(message)
    pending = state["pending_decision"]
    state["acceptance"] = {"outcome": "accepted" if args.decision == "accept" else "rejected", "actor_type": args.actor_type, "actor_id": args.actor_id, "message_sha256": digest, "decision_at_utc": utc_now(), "warning_candidate_sha256": pending["review_evaluation_sha256"]}
    if args.decision == "reject":
        evaluation = work_root / "iterations" / f"{state['current_iteration']:02d}" / "review_evaluation.json"
        state = append_transition(state, "failed", "warning_candidate_rejected", artifact=evaluation, work_root=work_root)
    _validate_state(state, args.schema_dir)
    commit_state(state_path, state)
    return state


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "init":
            state = initialize(args)
        elif args.command == "advance":
            state = advance(args)
        else:
            state = warning_response(args)
        log_event(args.log_file, level="info", component=COMPONENT, event="completed", message="Run state updated", run_id=args.run_id, iteration=state["current_iteration"], data={"state": state["state"]})
        return success(COMPONENT, {"run_state": str((args.output if args.command == "init" else args.state).resolve()), "state": state["state"], "current_iteration": state["current_iteration"]}, run_id=args.run_id, iteration=state["current_iteration"] or None)
    except (ContractError, json.JSONDecodeError) as exc:
        wrapped = AssetError(str(exc), code="contract_error")
        log_event(args.log_file, level="error", component=COMPONENT, event="failed", message=str(wrapped), run_id=args.run_id, iteration=None, data={"exit_code": 4})
        return failure(COMPONENT, wrapped, run_id=args.run_id, iteration=None)
    except Exception as exc:
        log_event(args.log_file, level="error", component=COMPONENT, event="failed", message=str(exc), run_id=args.run_id, iteration=None, data={"exit_code": getattr(exc, "exit_code", 70)})
        return failure(COMPONENT, exc, run_id=args.run_id, iteration=None)


if __name__ == "__main__":
    raise SystemExit(main())
