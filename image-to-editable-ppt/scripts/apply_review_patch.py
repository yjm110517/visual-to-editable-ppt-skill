from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from defusedxml import ElementTree as SafeET

from asset_common import AssetError, atomic_write_bytes, atomic_write_json, failure, load_contract, log_event, sha256_file, success
from contract_migration import migrate_v13_spec_bundle
from iteration_common import append_transition, commit_state, require_under, utc_now
from schema_utils import ContractError, cross_validate, is_safe_relative_path, validate_schema, validate_semantics


COMPONENT = "apply_review_patch"
SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas"
DERIVED_FIELDS = ("width_px", "height_px", "size_bytes", "sha256")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Apply a validated review patch to a new iteration transactionally.")
    result.add_argument("--work-root", type=Path, required=True)
    result.add_argument("--run-state", type=Path, required=True)
    result.add_argument("--current-dir", type=Path, required=True)
    result.add_argument("--patch", type=Path, required=True)
    result.add_argument("--next-dir", type=Path, required=True)
    result.add_argument("--schema-dir", type=Path, default=SCHEMA_DIR)
    result.add_argument("--run-id", required=True)
    result.add_argument("--log-file", type=Path)
    return result


def _index(items: list[dict[str, Any]], key: str, label: str) -> dict[str, dict[str, Any]]:
    result = {item[key]: item for item in items}
    if len(result) != len(items):
        raise AssetError(f"duplicate {label}", code="contract_error")
    return result


def _item(items: list[dict[str, Any]], key: str, value: str, label: str) -> dict[str, Any]:
    matches = [item for item in items if item.get(key) == value]
    if len(matches) != 1:
        raise AssetError(f"unknown or duplicate {label}: {value}", path=value, code="unknown_reference")
    return matches[0]


def _approved_affected(operation: dict[str, Any], layout: dict[str, Any], approved: set[str]) -> set[str]:
    affected: set[str] = set()
    element_id = operation.get("element_id")
    if element_id in approved:
        affected.add(element_id)
    if operation["type"] == "update_style":
        affected.update(item["id"] for item in layout["elements"] if item["id"] in approved and item.get("style_ref") == operation["style_id"])
    if operation["type"] in {"recrop_asset", "replace_asset"}:
        affected.update(item["id"] for item in layout["elements"] if item["id"] in approved and item.get("asset_id") == operation["asset_id"])
    return affected


def _invalidate_manifest(item: dict[str, Any]) -> None:
    for field in DERIVED_FIELDS:
        item.pop(field, None)
    item["security_status"] = "pending"


def _remove_svg_report_entry(stage: Path, asset_id: str) -> None:
    path = stage / "svg_security_report.json"
    if not path.is_file():
        return
    report = json.loads(path.read_text(encoding="utf-8"))
    results = [item for item in report.get("results", []) if item.get("asset_id") != asset_id]
    if results:
        atomic_write_json(path, {**report, "results": results})
    else:
        path.unlink()


def _remove_processing_report_entry(stage: Path, asset_id: str) -> None:
    path = stage / "asset_processing_report.json"
    if not path.is_file():
        return
    report = json.loads(path.read_text(encoding="utf-8"))
    assets = [item for item in report.get("assets", []) if item.get("asset_id") != asset_id]
    if assets:
        atomic_write_json(path, {**report, "assets": assets, "asset_manifest_sha256": "0" * 64})
    else:
        path.unlink()


def _remove_asset_file(stage: Path, manifest_item: dict[str, Any]) -> None:
    relative = manifest_item.get("path")
    if relative and is_safe_relative_path(relative):
        candidate = (stage / relative).resolve()
        try:
            candidate.relative_to(stage.resolve())
        except ValueError:
            return
        if candidate.is_file():
            candidate.unlink()


def _svg_view_box(content: str) -> str:
    try:
        root = SafeET.fromstring(content.encode("utf-8"))
    except Exception as exc:
        raise AssetError("replacement SVG is not safe well-formed XML", code="svg_xml") from exc
    view_box = root.attrib.get("viewBox")
    if not view_box:
        raise AssetError("replacement SVG requires viewBox", code="svg_viewbox")
    values = view_box.replace(",", " ").split()
    try:
        numbers = [float(value) for value in values]
    except ValueError as exc:
        raise AssetError("replacement SVG viewBox is invalid", code="svg_viewbox") from exc
    if len(numbers) != 4 or numbers[2] <= 0 or numbers[3] <= 0:
        raise AssetError("replacement SVG viewBox must have positive dimensions", code="svg_viewbox")
    return " ".join(values)


def _apply_operation(stage: Path, layout: dict[str, Any], crops: dict[str, Any], manifest: dict[str, Any], operation: dict[str, Any], approved: set[str]) -> None:
    affected = _approved_affected(operation, layout, approved)
    if affected and not operation.get("override_reason"):
        raise AssetError("modifying an approved element or dependency requires override_reason", path=",".join(sorted(affected)), code="approved_element")
    before = json.dumps([layout, crops, manifest], ensure_ascii=False, sort_keys=True)
    kind = operation["type"]
    changes = operation["changes"]
    if kind == "update_element":
        element = _item(layout["elements"], "id", operation["element_id"], "element")
        if {"id", "type"} & set(changes):
            raise AssetError("update_element cannot change id or type", code="contract_error")
        element.update(copy.deepcopy(changes))
    elif kind == "update_style":
        style_id = operation["style_id"]
        if style_id not in layout["styles"]:
            raise AssetError("unknown style", path=style_id, code="unknown_reference")
        layout["styles"][style_id].update(copy.deepcopy(changes))
    elif kind == "recrop_asset":
        crop = _item(crops["assets"], "id", operation["asset_id"], "crop asset")
        asset = _item(manifest["assets"], "id", operation["asset_id"], "manifest asset")
        old = copy.deepcopy(asset)
        crop_changes = copy.deepcopy(changes)
        boundary_policy = crop_changes.pop("boundary_policy", None)
        crop.update(crop_changes)
        if boundary_policy is not None:
            asset["boundary_policy"] = boundary_policy
        _remove_asset_file(stage, old)
        _invalidate_manifest(asset)
        _remove_svg_report_entry(stage, operation["asset_id"])
        _remove_processing_report_entry(stage, operation["asset_id"])
    elif kind == "replace_asset":
        asset_id = operation["asset_id"]
        asset = _item(manifest["assets"], "id", asset_id, "manifest asset")
        if "replacement_asset_id" in changes:
            replacement = changes["replacement_asset_id"]
            _item(manifest["assets"], "id", replacement, "replacement asset")
            for element in layout["elements"]:
                if element.get("asset_id") == asset_id:
                    element["asset_id"] = replacement
        else:
            generated = changes["generated_svg"]
            filename = generated["filename"]
            if not is_safe_relative_path(filename, filename_only=True):
                raise AssetError("unsafe generated SVG filename", path=filename, code="unsafe_path")
            relative = f"assets/{filename}"
            if any(item["id"] != asset_id and item["path"] == relative for item in manifest["assets"]):
                raise AssetError("generated SVG path collides with another asset", path=relative, code="output_collision")
            old = copy.deepcopy(asset)
            _remove_asset_file(stage, old)
            content = generated["content"]
            target = stage / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(target, content.encode("utf-8"))
            asset.clear()
            asset.update({"id": asset_id, "type": "svg", "path": relative, "source": "agent-generated", "view_box": _svg_view_box(content), "recolorable": generated["recolorable"], "contains_text": generated["contains_text"], "text_editability_exempt": generated["text_editability_exempt"], "security_status": "pending"})
            if generated.get("exemption_reason"):
                asset["exemption_reason"] = generated["exemption_reason"]
            crops["assets"] = [item for item in crops["assets"] if item["id"] != asset_id]
            _remove_svg_report_entry(stage, asset_id)
            _remove_processing_report_entry(stage, asset_id)
    elif kind == "reclassify_element":
        element = _item(layout["elements"], "id", operation["element_id"], "element")
        replacement = copy.deepcopy(changes["replacement"])
        if replacement.get("id") != element["id"] or replacement.get("type") == element["type"]:
            raise AssetError("reclassification requires the same id and a different type", code="contract_error")
        layout["elements"][layout["elements"].index(element)] = replacement
    elif kind == "add_element":
        element = copy.deepcopy(changes["element"])
        if not element.get("id") or any(item["id"] == element["id"] for item in layout["elements"]):
            raise AssetError("add_element requires a new element id", code="duplicate_id")
        layout["elements"].append(element)
    elif kind == "remove_element":
        element = _item(layout["elements"], "id", operation["element_id"], "element")
        layout["elements"].remove(element)
    else:
        raise AssetError("unsupported patch operation", code="contract_error")
    after = json.dumps([layout, crops, manifest], ensure_ascii=False, sort_keys=True)
    if before == after:
        raise AssetError("patch operation produced no material change", path=operation["issue_id"], code="no_op")


def _copy_inputs(current: Path, stage: Path) -> None:
    stage.mkdir(exist_ok=True)
    for name in ("layout.json", "crops.json", "asset_manifest.json", "asset_processing_report.json", "svg_security_report.json"):
        source = current / name
        if source.is_file():
            shutil.copy2(source, stage / name)
    if (current / "assets").is_dir():
        shutil.copytree(current / "assets", stage / "assets")


def apply_patch(args: argparse.Namespace) -> dict[str, Any]:
    work_root = args.work_root.resolve()
    state_path = require_under(args.run_state, work_root)
    current = require_under(args.current_dir, work_root)
    patch_path = require_under(args.patch, work_root)
    next_dir = require_under(args.next_dir, work_root, must_exist=False)
    expected_current = work_root / "iterations" / current.name
    state = load_contract("run_state", state_path, args.schema_dir)
    if state["state"] != "review_revise" or current != expected_current or current.name != f"{state['current_iteration']:02d}":
        raise AssetError("patch requires the current review_revise iteration", code="state_conflict", exit_code=9)
    if next_dir != work_root / "iterations" / f"{state['current_iteration'] + 1:02d}" or next_dir.exists():
        raise AssetError("next-dir must be the absent consecutive iteration", path=str(next_dir), code="output_conflict", exit_code=9)
    patch = load_contract("review_patch", patch_path, args.schema_dir)
    if patch["task_id"] != state["task_id"] or patch["from_iteration"] != state["current_iteration"] or patch["to_iteration"] != state["current_iteration"] + 1:
        raise AssetError("patch identity does not match run state", code="iteration_mismatch", exit_code=9)
    review_path, evaluation_path = current / "review_report.json", current / "review_evaluation.json"
    review = load_contract("review_report", review_path, args.schema_dir)
    evaluation = load_contract("review_evaluation", evaluation_path, args.schema_dir)
    if evaluation["policy_decision"] != "revise":
        raise AssetError("patch can only apply after policy revise", code="policy_conflict", exit_code=9)
    expected = {"based_on_review_sha256": sha256_file(review_path), "based_on_review_evaluation_sha256": sha256_file(evaluation_path)}
    expected.update({field: sha256_file(current / name) for field, name in (("layout_sha256", "layout.json"), ("crops_sha256", "crops.json"), ("asset_manifest_sha256", "asset_manifest.json"))})
    for field in ("based_on_review_sha256", "based_on_review_evaluation_sha256"):
        if patch[field] != expected[field]:
            raise AssetError("patch review input is stale", path=field, code="hash_conflict", exit_code=9)
    for field in ("layout_sha256", "crops_sha256", "asset_manifest_sha256"):
        if patch["preconditions"][field] != expected[field]:
            raise AssetError("patch precondition is stale", path=field, code="hash_conflict", exit_code=9)
    issue_ids = {item["id"] for item in review["issues"]}
    if any(item["issue_id"] not in issue_ids for item in patch["operations"]):
        raise AssetError("patch references an unknown review issue", code="unknown_issue")
    approved = set(review["approved_elements"])
    if not approved.issubset(set(patch["preserved_elements"])):
        raise AssetError("all approved elements must be listed as preserved", code="approved_element")

    iterations = current.parent
    stage = Path(tempfile.mkdtemp(prefix=f".{next_dir.name}-patch-", dir=iterations))
    installed = False
    try:
        _copy_inputs(current, stage)
        for field, name in (("layout_sha256", "layout.json"), ("crops_sha256", "crops.json"), ("asset_manifest_sha256", "asset_manifest.json")):
            if sha256_file(stage / name) != expected[field]:
                raise AssetError("current iteration changed while it was staged", path=name, code="hash_conflict", exit_code=9)
        raw_layout = json.loads((stage / "layout.json").read_text(encoding="utf-8"))
        raw_crops = json.loads((stage / "crops.json").read_text(encoding="utf-8"))
        raw_manifest = json.loads((stage / "asset_manifest.json").read_text(encoding="utf-8"))
        versions = {raw_layout.get("schema_version"), raw_crops.get("schema_version"), raw_manifest.get("schema_version")}
        if versions == {"1.3"}:
            if "contract_transition" not in patch:
                raise AssetError("v1.3 source iteration requires contract_transition", code="contract_transition")
            layout, crops, manifest = migrate_v13_spec_bundle(stage, patch["contract_transition"])
        elif versions == {"1.4"}:
            if "contract_transition" in patch:
                raise AssetError("v1.4 source iteration cannot be migrated again", code="contract_transition")
            layout = load_contract("layout", stage / "layout.json", args.schema_dir)
            crops = load_contract("crops", stage / "crops.json", args.schema_dir)
            manifest = load_contract("asset_manifest", stage / "asset_manifest.json", args.schema_dir)
        else:
            raise AssetError("spec bundle versions are incomplete or inconsistent", code="contract_transition")
        applied = []
        for operation in patch["operations"]:
            _apply_operation(stage, layout, crops, manifest, operation, approved)
            applied.append(operation["issue_id"])
        layout["metadata"]["iteration"] = patch["to_iteration"]
        for kind, name, document in (("layout", "layout.json", layout), ("crops", "crops.json", crops), ("asset_manifest", "asset_manifest.json", manifest)):
            validate_schema(kind, document, args.schema_dir)
            validate_semantics(kind, document)
            atomic_write_json(stage / name, document)
        cross_validate({"layout": layout, "crops": crops, "asset_manifest": manifest})
        state_after_planning = copy.deepcopy(state)
        state_after_planning["current_iteration"] = patch["to_iteration"]
        state_after_planning = append_transition(state_after_planning, "planning", "review_patch_started", artifact=patch_path, work_root=work_root)
        state_after_ready = copy.deepcopy(state_after_planning)
        state_after_ready["history"].append({"from": "planning", "to": "spec_ready", "reason": "review_patch_applied", "artifact": (next_dir / "layout.json").relative_to(work_root).as_posix(), "artifact_sha256": sha256_file(stage / "layout.json"), "timestamp_utc": utc_now()})
        state_after_ready["state"] = "spec_ready"
        validate_schema("run_state", state_after_ready, args.schema_dir)
        validate_semantics("run_state", state_after_ready)
        for field, name in (("layout_sha256", "layout.json"), ("crops_sha256", "crops.json"), ("asset_manifest_sha256", "asset_manifest.json")):
            if sha256_file(current / name) != expected[field]:
                raise AssetError("current iteration changed during patch application", path=name, code="hash_conflict", exit_code=9)
        os.replace(stage, next_dir)
        installed = True
        try:
            commit_state(state_path, state_after_ready)
        except Exception:
            shutil.rmtree(next_dir, ignore_errors=True)
            raise
        return {"next_iteration": str(next_dir), "applied_operations": applied, "skipped_operations": [], "layout_sha256": sha256_file(next_dir / "layout.json"), "crops_sha256": sha256_file(next_dir / "crops.json"), "asset_manifest_sha256": sha256_file(next_dir / "asset_manifest.json")}
    finally:
        if not installed:
            shutil.rmtree(stage, ignore_errors=True)


def main() -> int:
    args = parser().parse_args()
    try:
        outputs = apply_patch(args)
        log_event(args.log_file, level="info", component=COMPONENT, event="completed", message="Review patch applied", run_id=args.run_id, iteration=None, data={"applied": len(outputs["applied_operations"])})
        return success(COMPONENT, outputs, run_id=args.run_id, iteration=None)
    except (ContractError, json.JSONDecodeError) as caught:
        error = AssetError(str(caught), code="contract_error")
    except Exception as caught:
        error = caught
    log_event(args.log_file, level="error", component=COMPONENT, event="failed", message=str(error), run_id=args.run_id, iteration=None, data={"exit_code": getattr(error, "exit_code", 70)})
    return failure(COMPONENT, error, run_id=args.run_id, iteration=None)


if __name__ == "__main__":
    raise SystemExit(main())
