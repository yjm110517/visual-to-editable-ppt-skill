from __future__ import annotations

import argparse
import math
import os
import re
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException

from asset_common import AssetError, atomic_write_bytes, atomic_write_json, failure, load_contract, log_event, manifest_relative_path, resolve_under, sha256_file, success


DISALLOWED_ELEMENTS = {"script", "foreignobject", "image", "text", "tspan", "textpath"}
LINK_ATTRIBUTES = {"href", "src"}
URL_PATTERN = re.compile(r"url\(([^)]+)\)", re.IGNORECASE)
NUMBER_PATTERN = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?(?:px)?$")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Sanitize manifest SVG assets without overwriting originals.")
    result.add_argument("--asset-dir", required=True, type=Path)
    result.add_argument("--asset-manifest", required=True, type=Path)
    result.add_argument("--report", required=True, type=Path)
    result.add_argument("--schema-dir", type=Path, default=Path(__file__).resolve().parents[1] / "schemas")
    result.add_argument("--max-svg-bytes", type=int, default=10 * 1024 * 1024)
    result.add_argument("--log-file", type=Path)
    result.add_argument("--run-id", default="local")
    result.add_argument("--iteration", type=int)
    return result


def local_name(value: str) -> str:
    return value.rsplit("}", 1)[-1].lower()


def _parse_number(value: str | None) -> float | None:
    if value is None or not NUMBER_PATTERN.fullmatch(value.strip()):
        return None
    return float(value.strip().removesuffix("px"))


def _view_box(root: ET.Element) -> tuple[str, tuple[float, float, float, float]]:
    raw = root.attrib.get("viewBox") or root.attrib.get("viewbox")
    if not raw:
        raise AssetError("SVG requires a viewBox", code="invalid_viewbox")
    parts = [part for part in re.split(r"[\s,]+", raw.strip()) if part]
    if len(parts) != 4:
        raise AssetError("viewBox must contain four numbers", code="invalid_viewbox")
    try:
        values = tuple(float(part) for part in parts)
    except ValueError as exc:
        raise AssetError("viewBox contains a non-numeric value", code="invalid_viewbox") from exc
    if not all(math.isfinite(value) for value in values) or values[2] <= 0 or values[3] <= 0:
        raise AssetError("viewBox width and height must be finite and positive", code="invalid_viewbox")
    normalized = " ".join(format(value, ".15g") for value in values)
    root.attrib["viewBox"] = normalized
    root.attrib.pop("viewbox", None)
    return normalized, values


def _safe_url(value: str) -> bool:
    cleaned = value.strip().strip("'\"")
    return cleaned.startswith("#") and len(cleaned) > 1


def _inspect_element(element: ET.Element) -> None:
    name = local_name(element.tag)
    if name in DISALLOWED_ELEMENTS:
        raise AssetError(f"disallowed SVG element: {name}", code="unsafe_svg")
    for key, value in element.attrib.items():
        attribute = local_name(key)
        if attribute.startswith("on"):
            raise AssetError(f"event handler attribute is not allowed: {attribute}", code="unsafe_svg")
        if attribute in LINK_ATTRIBUTES and not _safe_url(value):
            raise AssetError(f"external link is not allowed: {value}", code="unsafe_svg")
        for match in URL_PATTERN.finditer(value):
            if not _safe_url(match.group(1)):
                raise AssetError(f"external URL is not allowed: {match.group(1)}", code="unsafe_svg")
        lowered = value.lower()
        if "javascript:" in lowered or "data:" in lowered or "file:" in lowered or "@import" in lowered:
            raise AssetError("active or embedded resource reference is not allowed", code="unsafe_svg")
    if name == "style":
        text = element.text or ""
        lowered = text.lower()
        if "@import" in lowered or "javascript:" in lowered or "data:" in lowered or "file:" in lowered:
            raise AssetError("unsafe CSS content", code="unsafe_svg")
        for match in URL_PATTERN.finditer(text):
            if not _safe_url(match.group(1)):
                raise AssetError("external CSS URL is not allowed", code="unsafe_svg")


def _strip_metadata(element: ET.Element) -> None:
    for child in list(element):
        if local_name(child.tag) == "metadata":
            element.remove(child)
        else:
            _strip_metadata(child)


def _sort_attributes(element: ET.Element) -> None:
    values = sorted(element.attrib.items())
    element.attrib.clear()
    element.attrib.update(values)
    for child in element:
        _sort_attributes(child)


def sanitize_bytes(content: bytes) -> tuple[bytes, str, int, int]:
    lowered = content.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        raise AssetError("DOCTYPE and entity declarations are not allowed", code="unsafe_xml")
    try:
        root = SafeET.fromstring(content)
    except (DefusedXmlException, ET.ParseError) as exc:
        raise AssetError("invalid or unsafe XML", code="unsafe_xml") from exc
    if local_name(root.tag) != "svg":
        raise AssetError("document root must be svg", code="invalid_svg")
    view_box, values = _view_box(root)
    for element in root.iter():
        _inspect_element(element)
    _strip_metadata(root)
    _sort_attributes(root)
    width = _parse_number(root.attrib.get("width"))
    height = _parse_number(root.attrib.get("height"))
    width_px = max(1, math.ceil(width if width and width > 0 else values[2]))
    height_px = max(1, math.ceil(height if height and height > 0 else values[3]))
    output = ET.tostring(root, encoding="utf-8", xml_declaration=True, short_empty_elements=True) + b"\n"
    return output, view_box, width_px, height_px


def sanitize_manifest(args: argparse.Namespace) -> dict[str, Any]:
    if args.max_svg_bytes < 1:
        raise AssetError("max-svg-bytes must be positive", path="--max-svg-bytes")
    manifest = load_contract("asset_manifest", args.asset_manifest, args.schema_dir)
    original_manifest = args.asset_manifest.read_bytes()
    if args.report.exists():
        raise AssetError("security report already exists", path=str(args.report), code="output_collision")
    asset_dir = args.asset_dir.resolve()
    manifest_base = args.asset_manifest.parent.resolve()
    results: list[dict[str, Any]] = []
    staged_specs: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    asset_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".svg-stage-", dir=asset_dir.parent) as temporary:
        stage_dir = Path(temporary)
        for index, item in enumerate(manifest["assets"]):
            if item["type"] != "svg":
                continue
            result: dict[str, Any] = {"asset_id": item["id"], "input_path": item["path"], "status": "rejected", "rejected_rules": []}
            try:
                source = resolve_under(manifest_base, item["path"])
                try:
                    source.relative_to(asset_dir)
                except ValueError as exc:
                    raise AssetError("SVG source must be inside asset-dir", path=item["path"], code="path_escape") from exc
                if not source.is_file():
                    raise AssetError("SVG source does not exist", path=item["path"], code="missing_input", exit_code=3)
                if source.stat().st_size > args.max_svg_bytes:
                    raise AssetError("SVG exceeds maximum file size", path=item["path"], code="svg_too_large")
                source_hash = sha256_file(source)
                sanitized, view_box, width_px, height_px = sanitize_bytes(source.read_bytes())
                destination = resolve_under(asset_dir, f"{item['id']}.sanitized.svg", filename_only=True)
                if destination.exists():
                    raise AssetError("sanitized SVG output already exists", path=str(destination), code="output_collision")
                staged = stage_dir / destination.name
                staged.write_bytes(sanitized)
                sanitized_hash = sha256_file(staged)
                result.update({"output_path": manifest_relative_path(args.asset_manifest, destination), "source_sha256": source_hash, "sanitized_sha256": sanitized_hash, "view_box": view_box, "width_px": width_px, "height_px": height_px, "status": "passed"})
                staged_specs.append({"staged": staged, "destination": destination, "item": item, "result": result})
            except AssetError as exc:
                result["rejected_rules"].append(exc.detail["code"])
                result["message"] = exc.detail["message"]
                failures.append(result)
            results.append(result)
        report = {"schema_version": "1.3", "results": results}
        if failures:
            atomic_write_json(args.report, report)
            raise AssetError(f"{len(failures)} SVG asset(s) failed security validation", path=str(args.report), code="unsafe_svg")
        committed: list[Path] = []
        try:
            for spec in staged_specs:
                os.replace(spec["staged"], spec["destination"])
                committed.append(spec["destination"])
                spec["item"].update({"path": spec["result"]["output_path"], "width_px": spec["result"]["width_px"], "height_px": spec["result"]["height_px"], "size_bytes": spec["destination"].stat().st_size, "sha256": spec["result"]["sanitized_sha256"], "view_box": spec["result"]["view_box"], "security_status": "passed"})
            atomic_write_json(args.asset_manifest, manifest)
            atomic_write_json(args.report, report)
        except Exception:
            for path in committed:
                path.unlink(missing_ok=True)
            atomic_write_bytes(args.asset_manifest, original_manifest)
            args.report.unlink(missing_ok=True)
            raise
    return {"asset_manifest": str(args.asset_manifest.resolve()), "report": str(args.report.resolve()), "sanitized_count": len(staged_specs)}


def main() -> int:
    args = parser().parse_args()
    component = "sanitize_svg"
    try:
        log_event(args.log_file, level="info", component=component, event="started", message="SVG sanitization started", run_id=args.run_id, iteration=args.iteration)
        outputs = sanitize_manifest(args)
        log_event(args.log_file, level="info", component=component, event="completed", message="SVG sanitization completed", run_id=args.run_id, iteration=args.iteration, data={"count": outputs["sanitized_count"], "exit_code": 0})
        return success(component, outputs, run_id=args.run_id, iteration=args.iteration)
    except Exception as exc:
        log_event(args.log_file, level="error", component=component, event="failed", message=str(exc), run_id=args.run_id, iteration=args.iteration, data={"exit_code": getattr(exc, "exit_code", 70)})
        return failure(component, exc, run_id=args.run_id, iteration=args.iteration)


if __name__ == "__main__":
    raise SystemExit(main())
