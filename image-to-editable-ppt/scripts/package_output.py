from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from asset_common import AssetError, contains_reparse_point, failure, load_contract, log_event, sha256_file, success
from iteration_common import append_transition, commit_state, require_under
from package_assets import package_assets
from schema_utils import ContractError, is_safe_relative_path, validate_schema, validate_semantics
from validate_assets import validate_asset_set


COMPONENT = "package_output"
SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Package only a delivery-gate-approved iteration.")
    result.add_argument("--work-root", type=Path, required=True)
    result.add_argument("--run-state", type=Path, required=True)
    result.add_argument("--delivery-decision", type=Path, required=True)
    result.add_argument("--ppt", type=Path, required=True)
    result.add_argument("--dist-root", type=Path, required=True)
    result.add_argument("--output-name", required=True)
    result.add_argument("--schema-dir", type=Path, default=SCHEMA_DIR)
    result.add_argument("--run-id", required=True)
    result.add_argument("--log-file", type=Path)
    return result


def _tree_hashes(root: Path) -> dict[str, str]:
    return {path.relative_to(root).as_posix(): sha256_file(path) for path in sorted(root.rglob("*")) if path.is_file()}


def _verify_chain(work_root: Path, iteration: Path, ppt: Path, state: dict[str, Any], decision: dict[str, Any], schema_dir: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    request_path = work_root / "request.json"
    qa_path, review_path, evaluation_path = iteration / "qa_report.json", iteration / "review_report.json", iteration / "review_evaluation.json"
    layout_path, crops_path, manifest_path = iteration / "layout.json", iteration / "crops.json", iteration / "asset_manifest.json"
    render_path = iteration / "rendered_slide.png"
    request = load_contract("request", request_path, schema_dir)
    qa = load_contract("qa_report", qa_path, schema_dir)
    review = load_contract("review_report", review_path, schema_dir)
    evaluation = load_contract("review_evaluation", evaluation_path, schema_dir)
    manifest = load_contract("asset_manifest", manifest_path, schema_dir)
    accepted = decision["accepted_iteration"]
    if qa["status"] != "pass" or qa["iteration"] != accepted or review["iteration"] != accepted or evaluation["iteration"] != accepted:
        raise AssetError("accepted reports must be structural pass and share one iteration", code="delivery_gate", exit_code=10)
    expected_status = "pass" if evaluation["policy_decision"] == "pass" else "pass_with_warnings" if evaluation["policy_decision"] == "warning_candidate" else None
    if decision["status"] != expected_status:
        raise AssetError("delivery status conflicts with review policy", code="delivery_gate", exit_code=10)
    decision_hashes = {"request_sha256": sha256_file(request_path), "qa_report_sha256": sha256_file(qa_path), "review_report_sha256": sha256_file(review_path), "review_evaluation_sha256": sha256_file(evaluation_path)}
    if any(decision[key] != value for key, value in decision_hashes.items()):
        raise AssetError("delivery decision report hashes are stale", code="hash_conflict", exit_code=9)
    if evaluation["inputs"] != {"request_sha256": decision_hashes["request_sha256"], "qa_report_sha256": decision_hashes["qa_report_sha256"], "review_report_sha256": decision_hashes["review_report_sha256"]}:
        raise AssetError("review evaluation hash chain is stale", code="hash_conflict", exit_code=9)
    provenance = qa["provenance"]
    expected_provenance = {
        "request_sha256": sha256_file(request_path), "layout_sha256": sha256_file(layout_path),
        "crops_sha256": sha256_file(crops_path), "asset_manifest_sha256": sha256_file(manifest_path),
        "ppt_sha256": sha256_file(ppt), "render_sha256": sha256_file(render_path),
    }
    if any(provenance[key] != value for key, value in expected_provenance.items()):
        raise AssetError("QA provenance does not match accepted files", code="hash_conflict", exit_code=9)
    context = review["review_context"]
    expected_context = {"layout_sha256": expected_provenance["layout_sha256"], "qa_report_sha256": decision_hashes["qa_report_sha256"], "asset_manifest_sha256": expected_provenance["asset_manifest_sha256"], "request_sha256": decision_hashes["request_sha256"], "render_sha256": expected_provenance["render_sha256"]}
    if any(context[key] != value for key, value in expected_context.items()):
        raise AssetError("review context does not match accepted files", code="hash_conflict", exit_code=9)
    if decision["status"] == "pass_with_warnings":
        acceptance = state.get("acceptance")
        approval = decision.get("approval")
        if not acceptance or acceptance["outcome"] != "accepted" or not approval or approval["message_sha256"] != acceptance["message_sha256"] or approval["warning_candidate_sha256"] != sha256_file(evaluation_path):
            raise AssetError("warning approval evidence does not match run state", code="delivery_gate", exit_code=10)
    validate_asset_set(asset_dir=iteration / "assets", manifest_path=manifest_path, schema_dir=schema_dir, layout_path=layout_path, svg_report_path=iteration / "svg_security_report.json")
    return request, qa, review, evaluation


def package(args: argparse.Namespace) -> dict[str, Any]:
    work_root = args.work_root.resolve()
    state_path = require_under(args.run_state, work_root)
    decision_path = require_under(args.delivery_decision, work_root)
    if state_path != work_root / "run_state.json" or decision_path != work_root / "delivery_decision.json":
        raise AssetError("state and decision must use canonical work-root paths", code="path_escape")
    if not is_safe_relative_path(args.output_name, filename_only=True):
        raise AssetError("output-name must be a safe single path component", path=args.output_name, code="unsafe_path")
    state = load_contract("run_state", state_path, args.schema_dir)
    decision = load_contract("delivery_decision", decision_path, args.schema_dir)
    if state["state"] != "packaging" or decision["status"] not in {"pass", "pass_with_warnings"} or decision["accepted_iteration"] != state["current_iteration"]:
        raise AssetError("delivery gate is not in an approved packaging state", code="delivery_gate", exit_code=10)
    if state["task_id"] != decision["task_id"] or state["request_sha256"] != decision["request_sha256"]:
        raise AssetError("delivery decision does not match run state", code="hash_conflict", exit_code=9)
    packaging_events = [item for item in state["history"] if item["to"] == "packaging"]
    expected_artifact = decision_path.relative_to(work_root).as_posix()
    if not packaging_events or packaging_events[-1]["artifact"] != expected_artifact or packaging_events[-1]["artifact_sha256"] != sha256_file(decision_path):
        raise AssetError("delivery decision is not the state-authorized packaging artifact", code="hash_conflict", exit_code=9)
    accepted = decision["accepted_iteration"]
    iteration = work_root / "iterations" / f"{accepted:02d}"
    ppt = require_under(args.ppt, work_root)
    if ppt.parent != iteration or ppt.suffix.lower() != ".pptx":
        raise AssetError("PPT must be inside the accepted iteration", path=str(ppt), code="path_escape")
    _verify_chain(work_root, iteration, ppt, state, decision, args.schema_dir)
    dist_lexical = args.dist_root.absolute()
    drive_root = Path(dist_lexical.anchor)
    if contains_reparse_point(dist_lexical, drive_root):
        raise AssetError("dist-root cannot contain symbolic links or reparse points", path=str(dist_lexical), code="reparse_point")
    dist_root = dist_lexical.resolve()
    dist_root.mkdir(parents=True, exist_ok=True)
    target = dist_root / args.output_name
    stage = Path(tempfile.mkdtemp(prefix=f".{args.output_name}-delivery-", dir=dist_root))
    try:
        names = {
            f"{args.output_name}_editable.pptx": ppt,
            f"{args.output_name}_preview.png": iteration / "rendered_slide.png",
            f"{args.output_name}_qa_report.json": iteration / "qa_report.json",
            f"{args.output_name}_review_report.json": iteration / "review_report.json",
            f"{args.output_name}_review_evaluation.json": iteration / "review_evaluation.json",
            f"{args.output_name}_delivery_decision.json": decision_path,
        }
        for name, source in names.items():
            shutil.copy2(source, stage / name)
        assets_zip = stage / f"{args.output_name}_assets.zip"
        package_assets(SimpleNamespace(asset_dir=iteration / "assets", asset_manifest=iteration / "asset_manifest.json", output=assets_zip, layout=iteration / "layout.json", svg_report=iteration / "svg_security_report.json", schema_dir=args.schema_dir, log_file=None, run_id=args.run_id, iteration=accepted))
        expected_names = set(names) | {assets_zip.name}
        if {path.name for path in stage.iterdir() if path.is_file()} != expected_names or any(path.is_dir() for path in stage.iterdir()):
            raise AssetError("delivery staging file set is not exact", code="delivery_integrity", exit_code=10)
        hashes = _tree_hashes(stage)
        installed = False
        if target.exists():
            if contains_reparse_point(target, dist_root):
                raise AssetError("delivery target cannot be a symbolic link or reparse point", path=str(target), code="reparse_point")
            if not target.is_dir() or _tree_hashes(target) != hashes:
                raise AssetError("existing delivery directory conflicts with accepted output", path=str(target), code="output_conflict", exit_code=9)
        else:
            os.replace(stage, target)
            installed = True
        updated = copy.deepcopy(state)
        updated["delivery"] = {"accepted_iteration": accepted, "delivery_decision_sha256": sha256_file(decision_path), "output_name": args.output_name, "files": hashes}
        updated = append_transition(updated, "delivered", "delivery_package_committed", artifact=decision_path, work_root=work_root)
        validate_schema("run_state", updated, args.schema_dir)
        validate_semantics("run_state", updated)
        try:
            commit_state(state_path, updated)
        except Exception:
            if installed:
                shutil.rmtree(target, ignore_errors=True)
            raise
        return {"dist": str(target), "accepted_iteration": accepted, "status": decision["status"], "files": hashes, "idempotent": not installed}
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)


def main() -> int:
    args = parser().parse_args()
    try:
        outputs = package(args)
        log_event(args.log_file, level="info", component=COMPONENT, event="completed", message="Accepted iteration packaged", run_id=args.run_id, iteration=outputs["accepted_iteration"], data={"status": outputs["status"]})
        return success(COMPONENT, outputs, run_id=args.run_id, iteration=outputs["accepted_iteration"])
    except (ContractError, json.JSONDecodeError) as caught:
        error = AssetError(str(caught), code="contract_error")
    except Exception as caught:
        error = caught
    log_event(args.log_file, level="error", component=COMPONENT, event="failed", message=str(error), run_id=args.run_id, iteration=None, data={"exit_code": getattr(error, "exit_code", 70)})
    return failure(COMPONENT, error, run_id=args.run_id, iteration=None)


if __name__ == "__main__":
    raise SystemExit(main())
