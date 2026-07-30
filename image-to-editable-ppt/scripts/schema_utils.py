from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

import yaml
from jsonschema import Draft202012Validator
from referencing import Registry, Resource


SCHEMA_VERSION = "1.3"
SCHEMA_FILES = {
    "request": "request.schema.json",
    "layout": "layout.schema.json",
    "crops": "crops.schema.json",
    "asset_manifest": "asset-manifest.schema.json",
    "qa_report": "qa-report.schema.json",
    "review_report": "review-report.schema.json",
    "review_evaluation": "review-evaluation.schema.json",
    "review_patch": "review-patch.schema.json",
    "run_state": "run-state.schema.json",
    "delivery_decision": "delivery-decision.schema.json",
    "build_summary": "build-summary.schema.json",
    "font_audit": "font-audit.schema.json",
    "render_report": "render-report.schema.json",
    "agent_role": "agent-role.schema.json",
    "agent_call_record": "agent-call-record.schema.json",
    "planner_response": "planner-response.schema.json",
    "reviewer_response": "reviewer-response.schema.json",
    "asset_processing_report": "asset-processing-report.schema.json",
}

SCHEMA_VERSIONS = {
    **{kind: "1.3" for kind in SCHEMA_FILES},
    "layout": "1.4",
    "crops": "1.4",
    "asset_manifest": "1.4",
    "qa_report": "1.4",
    "review_report": "1.4",
    "review_patch": "1.4",
    "planner_response": "1.4",
    "asset_processing_report": "1.4",
}


class ContractError(ValueError):
    def __init__(self, errors: Iterable[dict[str, Any]]):
        self.errors = list(errors)
        super().__init__("; ".join(error["message"] for error in self.errors))


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ContractError([error("$", "document must be a JSON object")])
    return value


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as stream:
        value = yaml.safe_load(stream)
    if not isinstance(value, dict):
        raise ContractError([error("$", "document must be a YAML object")])
    return value


def error(path: str, message: str, code: str = "semantic_error") -> dict[str, str]:
    return {"path": path, "code": code, "message": message}


def json_path(parts: Iterable[Any]) -> str:
    result = "$"
    for part in parts:
        result += f"[{part}]" if isinstance(part, int) else f".{part}"
    return result


def validate_schema(kind: str, document: dict[str, Any], schema_dir: Path) -> None:
    schema_path = schema_dir / SCHEMA_FILES[kind]
    schema = load_json(schema_path)
    resources = []
    for filename in SCHEMA_FILES.values():
        candidate = schema_dir / filename
        if candidate.is_file():
            candidate_schema = load_json(candidate)
            if candidate_schema.get("$id"):
                resources.append((candidate_schema["$id"], Resource.from_contents(candidate_schema)))
    validator = Draft202012Validator(schema, registry=Registry().with_resources(resources))
    failures = [
        error(json_path(item.absolute_path), item.message, "schema_error")
        for item in sorted(validator.iter_errors(document), key=lambda item: list(item.absolute_path))
    ]
    if failures:
        raise ContractError(failures)


def is_safe_relative_path(value: str, *, filename_only: bool = False) -> bool:
    if not value or "\\" in value or "\x00" in value:
        return False
    candidate = PurePosixPath(value)
    if candidate.is_absolute() or ".." in candidate.parts or "." in candidate.parts:
        return False
    if filename_only and len(candidate.parts) != 1:
        return False
    if any(part.endswith((" ", ".")) for part in candidate.parts):
        return False
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    return all(part.split(".", 1)[0].upper() not in reserved for part in candidate.parts)


def _is_utc_timestamp(value: str) -> bool:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z", value):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return value.endswith("Z") and parsed.tzinfo is not None and parsed.utcoffset() == timezone.utc.utcoffset(parsed)


def _unique(items: list[dict[str, Any]], key: str, base: str, errors: list[dict[str, str]]) -> None:
    seen: set[Any] = set()
    for index, item in enumerate(items):
        value = item.get(key)
        if value in seen:
            errors.append(error(f"{base}[{index}].{key}", f"duplicate {key}: {value}"))
        seen.add(value)


def _region_ok(region: dict[str, Any]) -> bool:
    return region["x"] + region["w"] <= 1 and region["y"] + region["h"] <= 1


def _connector_endpoints(item: dict[str, Any]) -> tuple[tuple[float, float], tuple[float, float]]:
    if item.get("geometry", "straight") == "curve":
        curve = item["curve"]
        endpoints = (
            (item["x"] + curve["start"]["x"], item["y"] + curve["start"]["y"]),
            (item["x"] + curve["end"]["x"], item["y"] + curve["end"]["y"]),
        )
    else:
        endpoints = (item["x"], item["y"]), (item["x"] + item["w"], item["y"] + item["h"])
    line = item.get("line", {})
    if line.get("begin_arrow", "none") != "none" and line.get("end_arrow", "none") == "none":
        return endpoints[1], endpoints[0]
    return endpoints


def _inside_box(point: tuple[float, float], box: dict[str, Any], *, inset: float) -> bool:
    if box["w"] <= inset * 2 or box["h"] <= inset * 2:
        return False
    return (
        box["x"] + inset < point[0] < box["x"] + box["w"] - inset
        and box["y"] + inset < point[1] < box["y"] + box["h"] - inset
    )


def _distance_to_box_boundary(point: tuple[float, float], box: dict[str, Any]) -> float:
    x, y = point
    left, top = box["x"], box["y"]
    right, bottom = left + box["w"], top + box["h"]
    if left <= x <= right and top <= y <= bottom:
        return min(x - left, right - x, y - top, bottom - y)
    dx = max(left - x, 0.0, x - right)
    dy = max(top - y, 0.0, y - bottom)
    return (dx * dx + dy * dy) ** 0.5


def _validate_review_issues(document: dict[str, Any], failures: list[dict[str, str]]) -> None:
    _unique(document["issues"], "id", "$.issues", failures)
    issues_by_id = {item["id"]: item for item in document["issues"]}
    for index, item in enumerate(document["issues"]):
        base = f"$.issues[{index}]"
        if item["severity"] != "suggestion" and not (item["element_ids"] or item["asset_ids"] or "source_region" in item or "render_region" in item):
            failures.append(error(base, "non-suggestion issue requires an element, asset, or region"))
        for field in ("source_region", "render_region"):
            if field in item and not _region_ok(item[field]):
                failures.append(error(base + f".{field}", "normalized region exceeds page bounds"))
    for check_name, check in document["mandatory_visual_checks"].items():
        base = f"$.mandatory_visual_checks.{check_name}"
        if check["status"] == "fail":
            if not check["issue_ids"]:
                failures.append(error(base + ".issue_ids", "a failed mandatory visual check must reference at least one issue"))
            for issue_id in check["issue_ids"]:
                issue = issues_by_id.get(issue_id)
                if issue is None:
                    failures.append(error(base + ".issue_ids", f"unknown issue id: {issue_id}"))
                elif issue["severity"] == "suggestion":
                    failures.append(error(base + ".issue_ids", "a failed mandatory visual check cannot reference only a suggestion"))
        elif check["issue_ids"]:
            failures.append(error(base + ".issue_ids", "pass and not_applicable checks cannot reference issues"))
    failed_checks = [
        name for name, check in document["mandatory_visual_checks"].items()
        if check["status"] == "fail"
    ]
    if document["reviewer_recommendation"] == "pass" and failed_checks:
        failures.append(error("$.reviewer_recommendation", "pass is forbidden while a mandatory visual check fails"))


def validate_semantics(kind: str, document: dict[str, Any]) -> None:
    failures: list[dict[str, str]] = []
    expected_version = SCHEMA_VERSIONS[kind]
    if document.get("schema_version") != expected_version:
        failures.append(error("$.schema_version", f"expected {expected_version}"))

    if kind == "request":
        policy = document["review_policy"]
        if policy["warning_floor_score"] > policy["pass_score"]:
            failures.append(error("$.review_policy.warning_floor_score", "must not exceed pass_score"))
    elif kind == "layout":
        elements = document["elements"]
        _unique(elements, "id", "$.elements", failures)
        styles = document["styles"]
        width, height = document["slide"]["width_in"], document["slide"]["height_in"]
        element_ids = {item["id"] for item in elements}
        for index, item in enumerate(elements):
            base = f"$.elements[{index}]"
            if item["type"] != "line" and (item["w"] <= 0 or item["h"] <= 0):
                failures.append(error(base, "non-line elements require positive width and height"))
            if item["type"] == "line" and item["w"] == 0 and item["h"] == 0:
                failures.append(error(base, "line elements require non-zero width or height"))
            if not item.get("allow_overflow", False) and (item["x"] + item["w"] > width or item["y"] + item["h"] > height):
                failures.append(error(base, "element exceeds slide bounds"))
            if "style_ref" in item and item["style_ref"] not in styles:
                failures.append(error(base + ".style_ref", "unknown style reference"))
            if item["type"] == "shape" and not item.get("fill") and not item.get("line"):
                style = styles.get(item.get("style_ref", ""), {})
                if not style.get("fill") and not style.get("line"):
                    failures.append(error(base, "shape requires a visible fill or line"))
            if item["type"] == "text":
                if item.get("editability_required", True) is False and not item.get("exemption_reason"):
                    failures.append(error(base + ".exemption_reason", "non-required text requires an exemption reason"))
                style = styles.get(item.get("style_ref", ""), {})
                element_font = item.get("font_face", style.get("font_face"))
                element_size = item.get("font_size_pt", style.get("font_size_pt"))
                element_color = item.get("color", style.get("color"))
                runs = item.get("runs", [{"text": item.get("text", "")}])
                for run_index, run in enumerate(runs):
                    if not run.get("font_face", element_font):
                        failures.append(error(f"{base}.runs[{run_index}].font_face", "font_face is unresolved after style inheritance"))
                    if not run.get("font_size_pt", element_size):
                        failures.append(error(f"{base}.runs[{run_index}].font_size_pt", "font_size_pt is unresolved after style inheritance"))
                    if not run.get("color", element_color):
                        failures.append(error(f"{base}.runs[{run_index}].color", "color is unresolved after style inheritance"))
            if item["type"] == "line":
                for field in ("from_id", "to_id"):
                    if item.get(field) and item[field] not in element_ids:
                        failures.append(error(base + f".{field}", "unknown connection element id"))
                if bool(item.get("from_id")) != bool(item.get("to_id")):
                    failures.append(error(base, "semantic connectors require both from_id and to_id"))
                if item.get("from_id") == item.get("to_id") and item.get("from_id"):
                    failures.append(error(base, "connector source and destination must differ"))
                if item.get("from_id"):
                    if item.get("geometry", "straight") == "arc":
                        failures.append(error(base + ".geometry", "semantic connectors require straight or endpoint-controlled curve geometry"))
                    line = item.get("line", {})
                    if line.get("end_arrow", "none") == "none" and line.get("begin_arrow", "none") == "none":
                        failures.append(error(base + ".line", "semantic connectors require a destination arrowhead"))
        groups = document.get("relationship_groups", [])
        _unique(groups, "id", "$.relationship_groups", failures)
        by_id = {item["id"]: item for item in elements}
        for group_index, group in enumerate(groups):
            base = f"$.relationship_groups[{group_index}]"
            nodes = group["node_ids"]
            connectors = group["connector_ids"]
            expected_count = len(nodes) if group["kind"] == "closed_cycle" else len(nodes) - 1
            if len(connectors) != expected_count:
                failures.append(error(base + ".connector_ids", f"{group['kind']} requires {expected_count} connectors"))
                continue
            for node_id in nodes:
                if node_id not in by_id or by_id[node_id]["type"] == "line":
                    failures.append(error(base + ".node_ids", f"unknown or invalid node: {node_id}"))
            for connector_index, connector_id in enumerate(connectors):
                connector = by_id.get(connector_id)
                if connector is None or connector.get("type") != "line":
                    failures.append(error(base + ".connector_ids", f"unknown line connector: {connector_id}"))
                    continue
                source_id = nodes[connector_index]
                target_id = nodes[(connector_index + 1) % len(nodes)]
                if connector.get("from_id") != source_id or connector.get("to_id") != target_id:
                    failures.append(error(base + f".connector_ids[{connector_index}]", f"must connect {source_id} to {target_id}"))
                start, end = _connector_endpoints(connector)
                source = by_id.get(source_id)
                target = by_id.get(target_id)
                if source and _distance_to_box_boundary(start, source) > 0.18:
                    failures.append(error(f"$.elements[{elements.index(connector)}]", "connector start is more than 0.18 in from source boundary"))
                if target and _distance_to_box_boundary(end, target) > 0.18:
                    failures.append(error(f"$.elements[{elements.index(connector)}]", "connector end is more than 0.18 in from target boundary"))
                if source and _inside_box(start, source, inset=0.12):
                    failures.append(error(f"$.elements[{elements.index(connector)}]", "connector start enters the source content safe area"))
                if target and _inside_box(end, target, inset=0.12):
                    failures.append(error(f"$.elements[{elements.index(connector)}]", "connector end enters the target content safe area"))
    elif kind == "crops":
        assets = document["assets"]
        _unique(assets, "id", "$.assets", failures)
        _unique(assets, "output", "$.assets", failures)
        if not is_safe_relative_path(document["source"]):
            failures.append(error("$.source", "unsafe relative source path"))
        for index, item in enumerate(assets):
            left, top, right, bottom = item["box_px"]
            if right <= left or bottom <= top:
                failures.append(error(f"$.assets[{index}].box_px", "crop box must be non-empty"))
            if not is_safe_relative_path(item["output"], filename_only=True):
                failures.append(error(f"$.assets[{index}].output", "unsafe output filename"))
            exclusions = item.get("semantic_exclusion_boxes_px", [])
            if exclusions and not item["remove_background"]:
                failures.append(error(f"$.assets[{index}].semantic_exclusion_boxes_px", "semantic exclusions require background removal"))
            for exclusion_index, exclusion in enumerate(exclusions):
                ex_left, ex_top, ex_right, ex_bottom = exclusion
                if ex_right <= ex_left or ex_bottom <= ex_top:
                    failures.append(error(f"$.assets[{index}].semantic_exclusion_boxes_px[{exclusion_index}]", "exclusion box must be non-empty"))
                if ex_left < left or ex_top < top or ex_right > right or ex_bottom > bottom:
                    failures.append(error(f"$.assets[{index}].semantic_exclusion_boxes_px[{exclusion_index}]", "exclusion box must stay inside the unpadded crop box"))
    elif kind == "asset_manifest":
        assets = document["assets"]
        _unique(assets, "id", "$.assets", failures)
        _unique(assets, "path", "$.assets", failures)
        for index, item in enumerate(assets):
            base = f"$.assets[{index}]"
            if not is_safe_relative_path(item["path"]):
                failures.append(error(base + ".path", "unsafe asset path"))
            if item["contains_text"] and not item["text_editability_exempt"]:
                failures.append(error(base, "text-bearing assets require an editability exemption"))
            if item["source"] == "cropped" and "boundary_policy" not in item:
                failures.append(error(base + ".boundary_policy", "cropped assets require a boundary policy"))
    elif kind in {"review_report", "reviewer_response"}:
        _validate_review_issues(document, failures)
    elif kind == "review_patch":
        if document["to_iteration"] != document["from_iteration"] + 1:
            failures.append(error("$.to_iteration", "must equal from_iteration + 1"))
        for index, operation in enumerate(document["operations"]):
            base = f"$.operations[{index}]"
            changes = operation["changes"]
            if operation["type"] == "update_element" and ({"id", "type"} & set(changes)):
                failures.append(error(base + ".changes", "update_element cannot change id or type"))
            if operation["type"] == "replace_asset" and "generated_svg" in changes:
                generated = changes["generated_svg"]
                content = generated["content"]
                if len(content.encode("utf-8")) > 1024 * 1024:
                    failures.append(error(base + ".changes.generated_svg.content", "generated SVG exceeds 1 MiB"))
                if "\x00" in content or re.search(r"(?is)<(?:script|foreignObject|text|tspan|textPath)\b|(?:href|xlink:href)\s*=\s*['\"](?!#)|(?:https?|file|data):", content):
                    failures.append(error(base + ".changes.generated_svg.content", "generated SVG contains forbidden active, text, or external content"))
                if generated["contains_text"] and (not generated["text_editability_exempt"] or not generated.get("exemption_reason")):
                    failures.append(error(base + ".changes.generated_svg", "text-bearing generated SVG requires an exemption"))
        transition = document.get("contract_transition")
        if transition and set(transition["asset_boundary_policies"]) == set():
            failures.append(error("$.contract_transition.asset_boundary_policies", "migration requires explicit policies"))
    elif kind == "qa_report":
        metrics = document["metrics"]
        if metrics["editable_text_status"] == "not_applicable" and metrics["editable_text_ratio"] is not None:
            failures.append(error("$.metrics.editable_text_ratio", "must be null when status is not_applicable"))
        if metrics["editable_text_status"] == "applicable" and metrics["editable_text_ratio"] is None:
            failures.append(error("$.metrics.editable_text_ratio", "must be numeric when status is applicable"))
        if document["status"] == "pass" and document["hard_failures"]:
            failures.append(error("$.hard_failures", "must be empty when status is pass"))
        if document["status"] == "fail" and not document["hard_failures"]:
            failures.append(error("$.hard_failures", "must not be empty when status is fail"))
        if document["status"] == "pass" and metrics["asset_boundary_violations"]:
            failures.append(error("$.metrics.asset_boundary_violations", "must be zero when status is pass"))
    elif kind == "asset_processing_report":
        _unique(document["assets"], "asset_id", "$.assets", failures)
        failed = [item for item in document["assets"] if item["status"] == "failed"]
        if document["status"] == "passed" and failed:
            failures.append(error("$.status", "passed report cannot contain failed assets"))
        if document["status"] == "failed" and not failed:
            failures.append(error("$.status", "failed report requires at least one failed asset"))
        for index, item in enumerate(document["assets"]):
            if item["status"] == "passed" and item["failure_codes"]:
                failures.append(error(f"$.assets[{index}].failure_codes", "passed assets cannot have failure codes"))
            if item["status"] == "failed" and not item["failure_codes"]:
                failures.append(error(f"$.assets[{index}].failure_codes", "failed assets require failure codes"))
    elif kind == "font_audit":
        if document["font_violations"] != sum(len(item["violations"]) for item in document["runs"]):
            failures.append(error("$.font_violations", "must equal the total run violation count"))
        if (document["status"] == "pass") != (document["font_violations"] == 0):
            failures.append(error("$.status", "must match font_violations"))
    elif kind == "render_report":
        if document["attempts"][-1]["status"] != "passed":
            failures.append(error("$.attempts", "final render attempt must pass"))
        if document["attempts"][-1]["renderer"] != document["renderer"]:
            failures.append(error("$.renderer", "must match the successful attempt"))
        if document["fallback_used"] != (len(document["attempts"]) > 1):
            failures.append(error("$.fallback_used", "must reflect whether a fallback attempt was used"))
    elif kind == "run_state":
        if document["current_iteration"] > document["max_iterations"]:
            failures.append(error("$.current_iteration", "must not exceed max_iterations"))
        allowed = {
            "input_pending": {"planning", "failed"}, "planning": {"spec_ready", "failed"},
            "spec_ready": {"building", "failed"}, "building": {"structural_pass", "structural_fail", "failed"},
            "structural_fail": {"planning", "failed"}, "structural_pass": {"reviewing", "failed"},
            "reviewing": {"review_evaluating", "failed"},
            "review_evaluating": {"review_pass", "review_revise", "review_fail", "review_warning_candidate", "failed"},
            "review_revise": {"planning", "failed"}, "review_pass": {"packaging", "failed"},
            "review_fail": {"failed"}, "review_warning_candidate": {"awaiting_user_acceptance", "failed"},
            "awaiting_user_acceptance": {"packaging", "failed"}, "packaging": {"delivered", "failed"},
            "delivered": set(), "failed": set(),
        }
        for index, item in enumerate(document["history"]):
            if item["to"] not in allowed[item["from"]]:
                failures.append(error(f"$.history[{index}]", "history contains a forbidden transition"))
            if (item["artifact"] is None) != (item["artifact_sha256"] is None):
                failures.append(error(f"$.history[{index}]", "artifact and artifact_sha256 must both be null or both be present"))
            if item["artifact"] is not None and not is_safe_relative_path(item["artifact"]):
                failures.append(error(f"$.history[{index}].artifact", "must be a safe work-root-relative path"))
            if not _is_utc_timestamp(item["timestamp_utc"]):
                failures.append(error(f"$.history[{index}].timestamp_utc", "must be a valid UTC timestamp"))
            if index and document["history"][index - 1]["to"] != item["from"]:
                failures.append(error(f"$.history[{index}].from", "history transition chain is discontinuous"))
        if document["history"] and document["history"][-1]["to"] != document["state"]:
            failures.append(error("$.state", "must equal the final history transition target"))
        pending = document.get("pending_decision")
        if pending and pending["iteration"] != document["current_iteration"]:
            failures.append(error("$.pending_decision.iteration", "must match current_iteration"))
        acceptance = document.get("acceptance")
        if acceptance and pending and acceptance["warning_candidate_sha256"] != pending["review_evaluation_sha256"]:
            failures.append(error("$.acceptance.warning_candidate_sha256", "must match pending review evaluation"))
        if acceptance and not _is_utc_timestamp(acceptance["decision_at_utc"]):
            failures.append(error("$.acceptance.decision_at_utc", "must be a valid UTC timestamp"))
        delivery = document.get("delivery")
        if delivery and delivery["accepted_iteration"] != document["current_iteration"]:
            failures.append(error("$.delivery.accepted_iteration", "must match current_iteration"))
        if delivery:
            prefix = delivery["output_name"] + "_"
            expected_suffixes = {"editable.pptx", "assets.zip", "preview.png", "qa_report.json", "review_report.json", "review_evaluation.json", "delivery_decision.json"}
            if set(delivery["files"]) != {prefix + suffix for suffix in expected_suffixes}:
                failures.append(error("$.delivery.files", "must contain the exact seven delivery filenames"))
    elif kind == "build_summary":
        expected = document["expected_element_count"]
        built = document["built_element_count"]
        if expected != len(document["build_order"]):
            failures.append(error("$.build_order", "length must equal expected_element_count"))
        if expected != len(document["element_map"]):
            failures.append(error("$.element_map", "length must equal expected_element_count"))
        if built != expected or document["missing_element_ids"] or document["unexpected_element_ids"]:
            failures.append(error("$", "successful build summary requires complete element reconciliation"))
    elif kind == "agent_role":
        if set(document["model_policy"]["required_capabilities"]) != {"image-input", "structured-json"}:
            failures.append(error("$.model_policy.required_capabilities", "both image-input and structured-json are required"))
    elif kind == "agent_call_record":
        if document["status"] == "succeeded" and not document["context_id"]:
            failures.append(error("$.context_id", "successful calls require a context id"))
        mode = document["model_selection_mode"]
        requested = document["requested_model"]
        if mode == "runtime-default" and requested is not None:
            failures.append(error("$.requested_model", "runtime-default calls cannot request a model"))
        if mode == "explicit" and requested is None:
            failures.append(error("$.requested_model", "explicit calls require a requested model"))
    elif kind == "planner_response":
        if document["mode"] == "initial":
            assets = document.get("generated_assets", [])
            _unique(assets, "asset_id", "$.generated_assets", failures)
            _unique(assets, "filename", "$.generated_assets", failures)
            decisions = document.get("representation_decisions", [])
            _unique(decisions, "id", "$.representation_decisions", failures)
            raster_signals = {
                "inner_shadow",
                "soft_shadow",
                "three_dimensional",
                "texture",
                "photographic_detail",
            }
            for index, decision in enumerate(decisions):
                base = f"$.representation_decisions[{index}]"
                if not _region_ok(decision["source_region"]):
                    failures.append(error(base + ".source_region", "normalized region exceeds source bounds"))
                representation = decision["selected_representation"]
                visual_kind = decision["visual_kind"]
                signals = set(decision["complexity_signals"])
                if visual_kind == "complex_icon" and representation == "native":
                    failures.append(error(base + ".selected_representation", "complex source icons cannot be replaced by native placeholders"))
                if visual_kind in {"photograph", "texture"} and representation != "crop":
                    failures.append(error(base + ".selected_representation", f"{visual_kind} requires a source crop"))
                if signals & raster_signals and representation != "crop":
                    failures.append(error(base + ".selected_representation", "raster-dependent effects require a source crop"))
                if representation == "native" and decision["fidelity_risk"] == "high":
                    failures.append(error(base + ".fidelity_risk", "high-risk visuals cannot use native representation"))
                if decision["contains_readable_text"] and visual_kind != "brand_mark":
                    failures.append(error(base + ".contains_readable_text", "only an inseparable brand mark may retain readable text in an asset"))
            total = 0
            for index, item in enumerate(assets):
                size = len(item["content"].encode("utf-8"))
                total += size
                if size > 1024 * 1024:
                    failures.append(error(f"$.generated_assets[{index}].content", "generated SVG exceeds 1 MiB"))
                if "\x00" in item["content"]:
                    failures.append(error(f"$.generated_assets[{index}].content", "generated SVG contains NUL"))
            if total > 5 * 1024 * 1024:
                failures.append(error("$.generated_assets", "generated SVG assets exceed 5 MiB total"))
    elif kind == "review_evaluation":
        dimensions = [item["dimension"] for item in document["score_adjustments"]]
        if len(dimensions) != len(set(dimensions)):
            failures.append(error("$.score_adjustments", "score adjustment dimensions must be unique"))
        if document["policy_decision"] == "pass" and document["failed_visual_checks"]:
            failures.append(error("$.policy_decision", "pass is forbidden while a mandatory visual check fails"))
    elif kind == "delivery_decision":
        status = document["status"]
        if not _is_utc_timestamp(document["timestamp_utc"]):
            failures.append(error("$.timestamp_utc", "must be a valid UTC timestamp"))
        for field in ("approval", "rejection"):
            if field in document and not _is_utc_timestamp(document[field]["decision_at_utc"]):
                failures.append(error(f"$.{field}.decision_at_utc", "must be a valid UTC timestamp"))
        if status == "pass" and document["warnings"]:
            failures.append(error("$.warnings", "normal pass cannot include warnings"))
        if status == "pass_with_warnings" and document["approval"]["warning_candidate_sha256"] != document["review_evaluation_sha256"]:
            failures.append(error("$.approval.warning_candidate_sha256", "must match review_evaluation_sha256"))
    if failures:
        raise ContractError(failures)


def cross_validate(documents: dict[str, dict[str, Any]]) -> None:
    failures: list[dict[str, str]] = []
    manifest_ids = {item["id"] for item in documents.get("asset_manifest", {}).get("assets", [])}
    if "crops" in documents and "asset_manifest" in documents:
        crops_by_id = {item["id"]: item for item in documents["crops"]["assets"]}
        for index, item in enumerate(documents["crops"]["assets"]):
            if item["id"] not in manifest_ids:
                failures.append(error(f"$.crops.assets[{index}].id", "crop asset is missing from asset manifest"))
        for index, asset in enumerate(documents["asset_manifest"]["assets"]):
            if asset["source"] != "cropped":
                continue
            crop = crops_by_id.get(asset["id"])
            if crop is None:
                failures.append(error(f"$.asset_manifest.assets[{index}].id", "cropped asset is missing from crops"))
                continue
            policy = asset["boundary_policy"]
            if policy == "transparent":
                if asset["type"] != "png" or crop["mode"] != "rgba" or not crop["remove_background"]:
                    failures.append(error(f"$.asset_manifest.assets[{index}].boundary_policy", "transparent requires PNG, rgba, and remove_background=true"))
            elif policy in {"source_tile", "shape_mask"} and crop["remove_background"]:
                failures.append(error(f"$.asset_manifest.assets[{index}].boundary_policy", f"{policy} requires remove_background=false"))
    if "layout" in documents and "asset_manifest" in documents:
        manifest_by_id = {item["id"]: item for item in documents["asset_manifest"]["assets"]}
        for index, item in enumerate(documents["layout"]["elements"]):
            if item.get("asset_id") and item["asset_id"] not in manifest_ids:
                failures.append(error(f"$.layout.elements[{index}].asset_id", "unknown asset reference"))
            elif item.get("asset_id"):
                asset = manifest_by_id[item["asset_id"]]
                if item.get("contains_text", asset["contains_text"]) != asset["contains_text"]:
                    failures.append(error(f"$.layout.elements[{index}].contains_text", "must match asset manifest"))
                if item.get("text_editability_exempt", asset["text_editability_exempt"]) != asset["text_editability_exempt"]:
                    failures.append(error(f"$.layout.elements[{index}].text_editability_exempt", "must match asset manifest"))
        for asset_id, asset in manifest_by_id.items():
            if asset.get("boundary_policy") != "shape_mask":
                continue
            references = [item for item in documents["layout"]["elements"] if item.get("asset_id") == asset_id]
            if not references or any(item.get("rounding") is not True for item in references):
                failures.append(error("$.layout.elements", f"all references to shape_mask asset {asset_id} require rounding=true"))
    if "asset_processing_report" in documents and "asset_manifest" in documents:
        report_by_id = {item["asset_id"]: item for item in documents["asset_processing_report"]["assets"]}
        for index, asset in enumerate(documents["asset_manifest"]["assets"]):
            if asset["source"] != "cropped":
                continue
            result = report_by_id.get(asset["id"])
            if result is None:
                failures.append(error(f"$.asset_manifest.assets[{index}].id", "cropped asset lacks processing evidence"))
            elif result["boundary_policy"] != asset["boundary_policy"]:
                failures.append(error(f"$.asset_processing_report.assets[{asset['id']}].boundary_policy", "must match asset manifest"))
    if "asset_processing_report" in documents and "crops" in documents:
        report_by_id = {item["asset_id"]: item for item in documents["asset_processing_report"]["assets"]}
        for index, crop in enumerate(documents["crops"]["assets"]):
            result = report_by_id.get(crop["id"])
            if result is not None and result["semantic_exclusion_boxes_px"] != crop.get("semantic_exclusion_boxes_px", []):
                failures.append(error(f"$.asset_processing_report.assets[{crop['id']}].semantic_exclusion_boxes_px", "must match crops contract"))
    if "planner_response" in documents and "asset_manifest" in documents:
        response = documents["planner_response"]
        manifest_by_id = {item["id"]: item for item in documents["asset_manifest"]["assets"]}
        for index, decision in enumerate(response.get("representation_decisions", [])):
            if decision["selected_representation"] != "crop":
                continue
            for asset_id in decision["asset_ids"]:
                asset = manifest_by_id.get(asset_id)
                if asset is None:
                    failures.append(error(f"$.planner_response.representation_decisions[{index}].asset_ids", f"unknown asset: {asset_id}"))
                elif asset.get("boundary_policy") != decision["boundary_policy"]:
                    failures.append(error(f"$.planner_response.representation_decisions[{index}].boundary_policy", f"must match asset {asset_id}"))
    if "request" in documents and "layout" in documents:
        if documents["request"]["topic"] != documents["layout"]["metadata"]["topic"]:
            failures.append(error("$.layout.metadata.topic", "must match request topic"))
    if failures:
        raise ContractError(failures)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_build_ready(manifest_path: Path, document: dict[str, Any]) -> None:
    failures: list[dict[str, str]] = []
    base = manifest_path.parent.resolve()
    for index, item in enumerate(document["assets"]):
        path = (base / PurePosixPath(item["path"])).resolve()
        try:
            path.relative_to(base)
        except ValueError:
            failures.append(error(f"$.assets[{index}].path", "asset escapes manifest directory"))
            continue
        required = ("width_px", "height_px", "size_bytes", "sha256")
        for field in required:
            if field not in item:
                failures.append(error(f"$.assets[{index}].{field}", "required for build-ready"))
        if not path.is_file():
            failures.append(error(f"$.assets[{index}].path", "asset file does not exist"))
            continue
        if path.is_symlink():
            failures.append(error(f"$.assets[{index}].path", "symbolic links are not allowed"))
        if item.get("size_bytes") != path.stat().st_size:
            failures.append(error(f"$.assets[{index}].size_bytes", "does not match file size"))
        if item.get("sha256", "").lower() != sha256_file(path):
            failures.append(error(f"$.assets[{index}].sha256", "does not match file hash"))
        if item["security_status"] != "passed":
            failures.append(error(f"$.assets[{index}].security_status", "must be passed for build-ready"))
    if failures:
        raise ContractError(failures)
