from __future__ import annotations

import argparse
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

from agent_common import SCHEMA_DIR, load_call_bundle, provenance_entry, stage_directory
from asset_common import AssetError, atomic_write_bytes, atomic_write_json, failure, load_contract, log_event, sha256_file, success
from schema_utils import ContractError, cross_validate, is_safe_relative_path, load_json, validate_schema, validate_semantics


COMPONENT = "finalize_agent_response"
MAX_SVG_BYTES = 1024 * 1024
MAX_TOTAL_SVG_BYTES = 5 * 1024 * 1024
BANNED_SVG = re.compile(r"(?:<script\b|<foreignobject\b|\bon[a-z]+\s*=|(?:href|src)\s*=\s*['\"](?:https?:|file:|data:)|base64)", re.IGNORECASE)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Validate and atomically submit Planner or Reviewer output.")
    result.add_argument("--role", choices=("planner", "reviewer"), required=True)
    result.add_argument("--mode", choices=("initial", "revision", "review"), required=True)
    result.add_argument("--call-dir", type=Path, required=True)
    result.add_argument("--planner-call-record", type=Path)
    result.add_argument("--iteration-dir", type=Path)
    result.add_argument("--output-dir", type=Path)
    result.add_argument("--output", type=Path)
    result.add_argument("--run-id", required=True)
    result.add_argument("--iteration", type=int, required=True)
    result.add_argument("--log-file", type=Path)
    result.add_argument("--schema-dir", type=Path, default=SCHEMA_DIR)
    return result


def _work_root(args: argparse.Namespace) -> Path:
    if args.role == "planner" and args.mode == "initial":
        if args.output_dir is None:
            raise AssetError("initial mode requires --output-dir", path="--output-dir", code="cli_error", exit_code=2)
        output = args.output_dir.resolve()
        if output.parent.name != "iterations" or output.name != f"{args.iteration:02d}":
            raise AssetError("output-dir must be work-root/iterations/<NN>", path=str(output), code="path_escape")
        return output.parent.parent
    if args.iteration_dir is None:
        raise AssetError("mode requires --iteration-dir", path="--iteration-dir", code="cli_error", exit_code=2)
    iteration = args.iteration_dir.resolve()
    if iteration.parent.name != "iterations" or iteration.name != f"{args.iteration:02d}":
        raise AssetError("iteration-dir must be work-root/iterations/<NN>", path=str(iteration), code="path_escape")
    return iteration.parent.parent


def _load_call_input(call_dir: Path, name: str) -> dict[str, Any]:
    return load_json(call_dir / "inputs" / name)


def _validate_identity(response: dict[str, Any], manifest: dict[str, Any], iteration: int) -> None:
    if response["task_id"] != manifest["task_id"] or response["iteration"] != iteration:
        raise AssetError("agent response task or iteration mismatch", path=str(manifest), code="iteration_mismatch", exit_code=9)


def _validate_no_full_page_raster(layout: dict[str, Any], crops: dict[str, Any], manifest: dict[str, Any]) -> None:
    crop_by_id = {item["id"]: item for item in crops["assets"]}
    manifest_by_id = {item["id"]: item for item in manifest["assets"]}
    slide_area = layout["slide"]["width_in"] * layout["slide"]["height_in"]
    source_area = layout["source"]["width_px"] * layout["source"]["height_px"]
    for element in layout["elements"]:
        if element["type"] != "image" or element["w"] * element["h"] < slide_area * 0.95:
            continue
        asset = manifest_by_id.get(element.get("asset_id"))
        crop = crop_by_id.get(element.get("asset_id"))
        if asset and asset["type"] in {"png", "jpeg"} and crop:
            left, top, right, bottom = crop["box_px"]
            if (right - left) * (bottom - top) >= source_area * 0.95:
                raise AssetError("full-page source raster cannot be used as a slide-sized image", path=f"layout.elements.{element['id']}", code="prompt_injection_guard")


def _validate_representation_decisions(
    response: dict[str, Any],
    layout: dict[str, Any],
    crops: dict[str, Any],
    manifest: dict[str, Any],
) -> None:
    elements = {item["id"]: item for item in layout["elements"]}
    crop_ids = {item["id"] for item in crops["assets"]}
    assets = {item["id"]: item for item in manifest["assets"]}
    covered_image_ids: set[str] = set()

    for index, decision in enumerate(response["representation_decisions"]):
        base = f"$.representation_decisions[{index}]"
        element_ids = set(decision["element_ids"])
        asset_ids = set(decision["asset_ids"])
        unknown_elements = element_ids - set(elements)
        unknown_assets = asset_ids - set(assets)
        if unknown_elements or unknown_assets:
            raise AssetError(
                "representation decision references an unknown element or asset",
                path=base,
                code="unknown_reference",
            )

        representation = decision["selected_representation"]
        if representation == "native":
            if any(elements[element_id]["type"] == "image" for element_id in element_ids):
                raise AssetError(
                    "native representation cannot target an image element",
                    path=base + ".element_ids",
                    code="representation_mismatch",
                )
            continue

        target_elements = [elements[element_id] for element_id in element_ids]
        if any(item["type"] != "image" for item in target_elements):
            raise AssetError(
                "asset representation must target image elements",
                path=base + ".element_ids",
                code="representation_mismatch",
            )
        referenced_assets = {item["asset_id"] for item in target_elements}
        if referenced_assets != asset_ids:
            raise AssetError(
                "representation decision asset IDs must match its image elements",
                path=base + ".asset_ids",
                code="representation_mismatch",
            )
        covered_image_ids.update(element_ids)

        for asset_id in asset_ids:
            asset = assets[asset_id]
            if representation == "crop":
                if asset["type"] not in {"png", "jpeg"} or asset["source"] != "cropped" or asset_id not in crop_ids:
                    raise AssetError(
                        "crop representation requires a cropped PNG/JPEG manifest entry and crop specification",
                        path=base + ".asset_ids",
                        code="representation_mismatch",
                    )
            elif asset["type"] != "svg":
                raise AssetError(
                    "SVG representation requires an SVG manifest entry",
                    path=base + ".asset_ids",
                    code="representation_mismatch",
                )
            if asset["contains_text"] != decision["contains_readable_text"]:
                raise AssetError(
                    "representation text declaration must match the asset manifest",
                    path=base + ".contains_readable_text",
                    code="representation_mismatch",
                )

    image_ids = {item["id"] for item in layout["elements"] if item["type"] == "image"}
    if image_ids != covered_image_ids:
        raise AssetError(
            "every image element must be covered by exactly one asset representation decision",
            path="$.representation_decisions",
            code="representation_inventory",
        )


def _finalize_initial(args: argparse.Namespace, response: dict[str, Any]) -> dict[str, Any]:
    output = args.output_dir.resolve()
    if output.exists():
        raise AssetError("iteration already exists", path=str(output), code="output_conflict", exit_code=9)
    request = _load_call_input(args.call_dir, "request.json")
    artifacts = response["artifacts"]
    layout, crops, asset_manifest = artifacts["layout"], artifacts["crops"], artifacts["asset_manifest"]
    for kind, document in (("layout", layout), ("crops", crops), ("asset_manifest", asset_manifest)):
        validate_schema(kind, document, args.schema_dir)
        validate_semantics(kind, document)
    cross_validate({"request": request, "layout": layout, "crops": crops, "asset_manifest": asset_manifest})
    _validate_no_full_page_raster(layout, crops, asset_manifest)
    _validate_representation_decisions(response, layout, crops, asset_manifest)

    generated = response.get("generated_assets", [])
    generated_by_id = {item["asset_id"]: item for item in generated}
    manifest_by_id = {item["id"]: item for item in asset_manifest["assets"]}
    total_size = 0
    for asset_id, generated_asset in generated_by_id.items():
        filename = generated_asset["filename"]
        if not is_safe_relative_path(filename, filename_only=True) or not filename.lower().endswith(".svg"):
            raise AssetError("unsafe generated SVG filename", path=filename, code="unsafe_path")
        content = generated_asset["content"].encode("utf-8")
        total_size += len(content)
        if len(content) > MAX_SVG_BYTES or total_size > MAX_TOTAL_SVG_BYTES:
            raise AssetError("generated SVG size limit exceeded", path=filename, code="asset_limit")
        if BANNED_SVG.search(generated_asset["content"]):
            raise AssetError("generated SVG contains a forbidden embedded resource or active content", path=filename, code="unsafe_svg")
        item = manifest_by_id.get(asset_id)
        if not item or item["type"] != "svg" or item["path"] != f"assets/{filename}":
            raise AssetError("generated SVG does not match asset manifest", path=asset_id, code="asset_manifest_mismatch")
        if item["source"] not in {"agent-generated", "locally-redrawn"} or item["security_status"] != "pending":
            raise AssetError("generated SVG must be pending and Agent-generated or locally redrawn", path=asset_id, code="asset_manifest_mismatch")
    declared_generated = {item["id"] for item in asset_manifest["assets"] if item["type"] == "svg" and item["source"] in {"agent-generated", "locally-redrawn"}}
    if declared_generated != set(generated_by_id):
        raise AssetError("generated SVG list and manifest declarations must match exactly", path="$.generated_assets", code="asset_manifest_mismatch")

    stage = stage_directory(output)
    try:
        atomic_write_json(stage / "layout.json", layout)
        atomic_write_json(stage / "crops.json", crops)
        atomic_write_json(stage / "asset_manifest.json", asset_manifest)
        for item in generated:
            atomic_write_bytes(stage / "assets" / item["filename"], item["content"].encode("utf-8"))
        os.replace(stage, output)
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return {"iteration_dir": str(output), "layout": str(output / "layout.json"), "crops": str(output / "crops.json"), "asset_manifest": str(output / "asset_manifest.json")}


def _verify_current_inputs(iteration_dir: Path, input_hashes: dict[str, str], names: list[str]) -> None:
    for name in names:
        actual = iteration_dir / name
        if not actual.is_file() or sha256_file(actual) != input_hashes[name]:
            raise AssetError("current iteration input changed after Agent call", path=str(actual), code="hash_conflict", exit_code=9)


def _verify_work_inputs(call_dir: Path, work_root: Path, input_hashes: dict[str, str]) -> None:
    request_copy = _load_call_input(call_dir, "request.json")
    current = {"request.json": work_root / "request.json", "source.png": work_root / request_copy["source_image"]}
    for name, path in current.items():
        if not path.is_file() or sha256_file(path) != input_hashes[name]:
            raise AssetError("work input changed after Agent call", path=str(path), code="hash_conflict", exit_code=9)


def _finalize_revision(args: argparse.Namespace, manifest: dict[str, Any], response: dict[str, Any], input_hashes: dict[str, str]) -> dict[str, Any]:
    iteration = args.iteration_dir.resolve()
    target = args.output.resolve() if args.output else iteration / "review_patch.json"
    if target != iteration / "review_patch.json":
        raise AssetError("revision output must be iteration-dir/review_patch.json", path=str(target), code="path_escape")
    if target.exists():
        raise AssetError("review patch already exists", path=str(target), code="output_conflict", exit_code=9)
    _verify_current_inputs(iteration, input_hashes, ["layout.json", "crops.json", "asset_manifest.json", "qa_report.json", "review_report.json", "review_evaluation.json"])
    patch = response["artifacts"]["review_patch"]
    validate_schema("review_patch", patch, args.schema_dir)
    validate_semantics("review_patch", patch)
    if patch["task_id"] != manifest["task_id"] or patch["from_iteration"] != args.iteration:
        raise AssetError("review patch identity mismatch", path="$.artifacts.review_patch", code="iteration_mismatch", exit_code=9)
    expected_hashes = {
        "based_on_review_sha256": sha256_file(iteration / "review_report.json"),
        "based_on_review_evaluation_sha256": sha256_file(iteration / "review_evaluation.json"),
    }
    for key, value in expected_hashes.items():
        if patch[key] != value:
            raise AssetError(f"review patch {key} mismatch", path=f"$.{key}", code="hash_conflict", exit_code=9)
    for key, filename in (("layout_sha256", "layout.json"), ("crops_sha256", "crops.json"), ("asset_manifest_sha256", "asset_manifest.json")):
        if patch["preconditions"][key] != sha256_file(iteration / filename):
            raise AssetError(f"review patch precondition mismatch: {key}", path=f"$.preconditions.{key}", code="hash_conflict", exit_code=9)
    review = load_json(iteration / "review_report.json")
    issue_ids = {item["id"] for item in review["issues"]}
    approved = set(review["approved_elements"])
    if not approved.issubset(set(patch["preserved_elements"])):
        raise AssetError("all approved elements must be preserved", path="$.preserved_elements", code="approved_element")
    for index, operation in enumerate(patch["operations"]):
        if operation["issue_id"] not in issue_ids:
            raise AssetError("patch operation references an unknown issue", path=f"$.operations[{index}].issue_id", code="unknown_issue")
        if operation.get("element_id") in approved and not operation.get("override_reason"):
            raise AssetError("modifying an approved element requires override_reason", path=f"$.operations[{index}]", code="approved_element")
    atomic_write_json(target, patch)
    return {"review_patch": str(target), "sha256": sha256_file(target)}


def _validate_review_references(response: dict[str, Any], layout: dict[str, Any], asset_manifest: dict[str, Any]) -> None:
    element_ids = {item["id"] for item in layout["elements"]}
    asset_ids = {item["id"] for item in asset_manifest["assets"]}
    issue_element_ids: set[str] = set()
    for index, issue in enumerate(response["issues"]):
        unknown_elements = set(issue["element_ids"]) - element_ids - {"slide-root"}
        unknown_assets = set(issue["asset_ids"]) - asset_ids
        if unknown_elements or unknown_assets:
            raise AssetError("review issue references an unknown element or asset", path=f"$.issues[{index}]", code="unknown_reference")
        if issue["severity"] != "suggestion":
            issue_element_ids.update(set(issue["element_ids"]) - {"slide-root"})
        action = issue["recommended_action"]
        if action.get("element_id") and action["element_id"] not in element_ids:
            raise AssetError("recommended action references an unknown element", path=f"$.issues[{index}].recommended_action.element_id", code="unknown_reference")
        if action.get("asset_id") and action["asset_id"] not in asset_ids:
            raise AssetError("recommended action references an unknown asset", path=f"$.issues[{index}].recommended_action.asset_id", code="unknown_reference")
        if action["type"] in {"recrop_asset", "replace_asset"} and not (action.get("asset_id") or issue["asset_ids"]):
            raise AssetError("asset action requires an asset target", path=f"$.issues[{index}].recommended_action", code="missing_target")
        if action["type"] in {"update_element", "update_style", "reclassify_element", "remove_element"} and not (action.get("element_id") or issue["element_ids"]):
            raise AssetError("element action requires an element target", path=f"$.issues[{index}].recommended_action", code="missing_target")
    approved = set(response["approved_elements"])
    if not approved.issubset(element_ids):
        raise AssetError("approved_elements contains an unknown element", path="$.approved_elements", code="unknown_reference")
    if approved & issue_element_ids:
        raise AssetError("approved element also has a non-suggestion issue", path="$.approved_elements", code="approved_element")


def _load_planner_record(path: Path, work_root: Path, schema_dir: Path) -> dict[str, Any]:
    if path.name != "call_record.json":
        raise AssetError("planner-call-record must name call_record.json", path=str(path), code="cli_error", exit_code=2)
    planner_call = path.resolve().parent
    manifest = load_json(planner_call / "call_manifest.json")
    mode = manifest.get("mode")
    if mode not in {"initial", "revision"}:
        raise AssetError("invalid Planner call mode", path=str(planner_call), code="call_bundle")
    _, record, _, _ = load_call_bundle(planner_call, work_root=work_root, role="planner", mode=mode, schema_dir=schema_dir)
    return record


def _finalize_review(args: argparse.Namespace, work_root: Path, call_manifest: dict[str, Any], reviewer_record: dict[str, Any], response: dict[str, Any], input_hashes: dict[str, str]) -> dict[str, Any]:
    if args.planner_call_record is None:
        raise AssetError("review mode requires --planner-call-record", path="--planner-call-record", code="cli_error", exit_code=2)
    iteration = args.iteration_dir.resolve()
    target = args.output.resolve() if args.output else iteration / "review_report.json"
    if target != iteration / "review_report.json":
        raise AssetError("review output must be iteration-dir/review_report.json", path=str(target), code="path_escape")
    if target.exists():
        raise AssetError("review report already exists", path=str(target), code="output_conflict", exit_code=9)
    _verify_current_inputs(iteration, input_hashes, ["layout.json", "qa_report.json", "asset_manifest.json", "rendered_slide.png"])
    request_path = work_root / "request.json"
    request_document = _load_call_input(args.call_dir, "request.json")
    source_path = work_root / request_document["source_image"]
    if not request_path.is_file() or sha256_file(request_path) != input_hashes["request.json"]:
        raise AssetError("request changed after Reviewer call", path=str(request_path), code="hash_conflict", exit_code=9)
    if not source_path.is_file() or sha256_file(source_path) != input_hashes["source.png"]:
        raise AssetError("source changed after Reviewer call", path=str(source_path), code="hash_conflict", exit_code=9)
    layout = load_contract("layout", iteration / "layout.json", args.schema_dir)
    asset_manifest = load_contract("asset_manifest", iteration / "asset_manifest.json", args.schema_dir)
    qa = load_contract("qa_report", iteration / "qa_report.json", args.schema_dir)
    if qa["status"] != "pass":
        raise AssetError("Reviewer cannot run after structural QA failure", path=str(iteration / "qa_report.json"), code="structural_gate", exit_code=8)
    _validate_review_references(response, layout, asset_manifest)
    planner_record = _load_planner_record(args.planner_call_record, work_root, args.schema_dir)
    if planner_record["task_id"] != call_manifest["task_id"]:
        raise AssetError("Planner provenance belongs to another task", path=str(args.planner_call_record), code="call_record")

    ordered_issues = []
    for issue in sorted(response["issues"], key=lambda item: item["id"]):
        normalized = dict(issue)
        normalized["element_ids"] = sorted(issue["element_ids"])
        normalized["asset_ids"] = sorted(issue["asset_ids"])
        ordered_issues.append(normalized)
    report = {
        "schema_version": "1.3", "task_id": response["task_id"], "iteration": response["iteration"],
        "reviewer_recommendation": response["reviewer_recommendation"], "scores": response["scores"],
        "issues": ordered_issues, "approved_elements": sorted(response["approved_elements"]), "warnings": sorted(response["warnings"]),
        "review_context": {
            "source_sha256": input_hashes["source.png"], "render_sha256": input_hashes["rendered_slide.png"],
            "layout_sha256": input_hashes["layout.json"], "qa_report_sha256": input_hashes["qa_report.json"],
            "asset_manifest_sha256": input_hashes["asset_manifest.json"], "request_sha256": input_hashes["request.json"],
            "review_rubric_sha256": input_hashes["visual-review-rubric.md"],
            "reviewer_response_schema_sha256": input_hashes["reviewer-response.schema.json"],
            "reviewer_role_version": call_manifest["role_version"],
        },
        "agent_provenance": {
            "planner": provenance_entry(planner_record), "reviewer": provenance_entry(reviewer_record),
            "review_rubric_sha256": input_hashes["visual-review-rubric.md"],
        },
    }
    validate_schema("review_report", report, args.schema_dir)
    validate_semantics("review_report", report)
    atomic_write_json(target, report)
    return {"review_report": str(target), "sha256": sha256_file(target)}


def main() -> int:
    args = parser().parse_args()
    try:
        if args.iteration < 1:
            raise AssetError("iteration must be positive", path="--iteration", code="cli_error", exit_code=2)
        valid = (args.role == "planner" and args.mode in {"initial", "revision"}) or (args.role == "reviewer" and args.mode == "review")
        if not valid:
            raise AssetError("role and mode are incompatible", path="--mode", code="cli_error", exit_code=2)
        work_root = _work_root(args)
        args.call_dir = args.call_dir.resolve()
        manifest, record, response, input_hashes = load_call_bundle(args.call_dir, work_root=work_root, role=args.role, mode=args.mode, schema_dir=args.schema_dir)
        if manifest["iteration"] != args.iteration:
            raise AssetError("call iteration does not match CLI", path="--iteration", code="iteration_mismatch", exit_code=9)
        _validate_identity(response, manifest, args.iteration)
        _verify_work_inputs(args.call_dir, work_root, input_hashes)
        if args.role == "planner" and args.mode == "initial":
            outputs = _finalize_initial(args, response)
        elif args.role == "planner":
            outputs = _finalize_revision(args, manifest, response, input_hashes)
        else:
            outputs = _finalize_review(args, work_root, manifest, record, response, input_hashes)
        log_event(args.log_file, level="info", component=COMPONENT, event="completed", message="Agent response finalized", run_id=args.run_id, iteration=args.iteration, data={"role": args.role, "mode": args.mode})
        return success(COMPONENT, outputs, run_id=args.run_id, iteration=args.iteration)
    except (ContractError, UnicodeError, json.JSONDecodeError) as exc:
        wrapped = AssetError(str(exc), path="$", code="contract_error")
        log_event(args.log_file, level="error", component=COMPONENT, event="failed", message=str(wrapped), run_id=args.run_id, iteration=args.iteration, data={"exit_code": 4})
        return failure(COMPONENT, wrapped, run_id=args.run_id, iteration=args.iteration)
    except Exception as exc:
        log_event(args.log_file, level="error", component=COMPONENT, event="failed", message=str(exc), run_id=args.run_id, iteration=args.iteration, data={"exit_code": getattr(exc, "exit_code", 70)})
        return failure(COMPONENT, exc, run_id=args.run_id, iteration=args.iteration)


if __name__ == "__main__":
    raise SystemExit(main())
