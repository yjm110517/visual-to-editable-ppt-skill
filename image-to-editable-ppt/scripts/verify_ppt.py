from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import posixpath
import subprocess
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from pptx import Presentation

from asset_common import AssetError, atomic_write_json, failure, load_contract, log_event, sha256_file, success
from schema_utils import cross_validate, validate_build_ready, validate_schema, validate_semantics


COMPONENT = "verify_ppt"
NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "pr": "http://schemas.openxmlformats.org/package/2006/relationships",
}
OBJECT_TAGS = {f"{{{NS['p']}}}sp", f"{{{NS['p']}}}pic", f"{{{NS['p']}}}graphicFrame", f"{{{NS['p']}}}cxnSp", f"{{{NS['p']}}}grpSp"}
EMU_PER_POINT = 12700


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Verify editable PPT structure and produce qa_report.json.")
    result.add_argument("--request", required=True, type=Path)
    result.add_argument("--source", required=True, type=Path)
    result.add_argument("--iteration-dir", required=True, type=Path)
    result.add_argument("--ppt", required=True, type=Path)
    result.add_argument("--layout", required=True, type=Path)
    result.add_argument("--crops", required=True, type=Path)
    result.add_argument("--asset-manifest", required=True, type=Path)
    result.add_argument("--build-summary", required=True, type=Path)
    result.add_argument("--font-audit", required=True, type=Path)
    result.add_argument("--render", required=True, type=Path)
    result.add_argument("--render-report", required=True, type=Path)
    result.add_argument("--output", required=True, type=Path)
    result.add_argument("--schema-dir", type=Path, default=Path(__file__).resolve().parents[1] / "schemas")
    result.add_argument("--log-file", type=Path)
    result.add_argument("--run-id", default="local")
    result.add_argument("--iteration", type=int)
    return result


def _failure(target: list[str], code: str, message: str) -> None:
    target.append(f"{code}: {message}")


def _object_name(shape: ET.Element) -> str:
    item = shape.find(".//p:cNvPr", NS)
    return item.attrib.get("name", "") if item is not None else ""


def _base_id(name: str) -> str:
    return name[4:].split("#", 1)[0]


def _matches_native_type(shape: ET.Element, element: dict[str, Any]) -> bool:
    expected_type = element["type"]
    if expected_type == "image":
        return shape.tag == f"{{{NS['p']}}}pic"
    if expected_type == "text":
        return shape.tag == f"{{{NS['p']}}}sp" and shape.find("p:txBody", NS) is not None
    if expected_type == "line":
        if shape.tag == f"{{{NS['p']}}}cxnSp":
            return True
        expected_geometry = element.get("geometry", "straight")
        if expected_geometry == "curve":
            return shape.tag == f"{{{NS['p']}}}sp" and shape.find(".//a:custGeom", NS) is not None
        geometry = shape.find(".//a:prstGeom", NS)
        accepted_geometry = "arc" if expected_geometry == "arc" else "line"
        return shape.tag == f"{{{NS['p']}}}sp" and geometry is not None and geometry.attrib.get("prst") == accepted_geometry
    if expected_type == "shape":
        if shape.tag != f"{{{NS['p']}}}sp":
            return False
        geometry = shape.find(".//a:prstGeom", NS)
        return geometry is None or geometry.attrib.get("prst") != "line"
    return False


def _shape_bounds(shape: ET.Element) -> tuple[float, float, float, float] | None:
    transform = shape.find("p:spPr/a:xfrm", NS)
    if transform is None:
        transform = shape.find("p:xfrm", NS)
    if transform is None:
        return None
    offset = transform.find("a:off", NS)
    extent = transform.find("a:ext", NS)
    if offset is None or extent is None:
        return None
    x, y = float(offset.attrib.get("x", 0)), float(offset.attrib.get("y", 0))
    width, height = float(extent.attrib.get("cx", 0)), float(extent.attrib.get("cy", 0))
    angle = float(transform.attrib.get("rot", 0)) / 60000
    radians = math.radians(angle)
    rotated_width = abs(width * math.cos(radians)) + abs(height * math.sin(radians))
    rotated_height = abs(width * math.sin(radians)) + abs(height * math.cos(radians))
    center_x, center_y = x + width / 2, y + height / 2
    return center_x - rotated_width / 2, center_y - rotated_height / 2, center_x + rotated_width / 2, center_y + rotated_height / 2


def _slide_relationships(archive: zipfile.ZipFile, slide_index: int) -> dict[str, str]:
    path = f"ppt/slides/_rels/slide{slide_index}.xml.rels"
    if path not in archive.namelist():
        return {}
    root = ET.fromstring(archive.read(path))
    result = {}
    for relation in root.findall("pr:Relationship", NS):
        target = relation.attrib.get("Target", "")
        result[relation.attrib["Id"]] = posixpath.normpath(posixpath.join("ppt/slides", target))
    return result


def _media_is_valid(shape: ET.Element, relationships: dict[str, str], archive_names: set[str]) -> bool:
    ids = []
    for node in shape.findall(".//a:blip", NS):
        value = node.attrib.get(f"{{{NS['r']}}}embed")
        if value:
            ids.append(value)
    for node in shape.findall(".//*[@r:embed]", NS):
        value = node.attrib.get(f"{{{NS['r']}}}embed")
        if value:
            ids.append(value)
    return bool(ids) and all(item in relationships and relationships[item] in archive_names for item in set(ids))


def _inspect_pptx(ppt: Path, layout: dict[str, Any], summary: dict[str, Any], hard_failures: list[str]) -> dict[str, Any]:
    result: dict[str, Any] = {"slide_count": 0, "slide_width": 0, "slide_height": 0, "objects": {}, "duplicate_names": [], "untracked_names": [], "out_of_bounds": 0, "missing_media": 0}
    try:
        presentation = Presentation(ppt)
        result["slide_count"] = len(presentation.slides)
        result["slide_width"], result["slide_height"] = presentation.slide_width, presentation.slide_height
        with zipfile.ZipFile(ppt) as archive:
            archive_names = set(archive.namelist())
            for slide_index in range(1, result["slide_count"] + 1):
                slide_path = f"ppt/slides/slide{slide_index}.xml"
                if slide_path not in archive_names:
                    _failure(hard_failures, "MISSING_SLIDE_PART", slide_path)
                    continue
                root = ET.fromstring(archive.read(slide_path))
                tree = root.find(".//p:spTree", NS)
                if tree is None:
                    continue
                relationships = _slide_relationships(archive, slide_index)
                for shape in list(tree):
                    if shape.tag not in OBJECT_TAGS:
                        continue
                    name = _object_name(shape)
                    if not name.startswith("ivt:"):
                        result["untracked_names"].append(name or f"slide-{slide_index}-unnamed")
                        continue
                    if name in result["objects"]:
                        result["duplicate_names"].append(name)
                    result["objects"].setdefault(name, shape)
                    base = _base_id(name)
                    element = next((item for item in layout["elements"] if item["id"] == base), None)
                    if element and not element.get("allow_overflow", False):
                        bounds = _shape_bounds(shape)
                        tolerance = 0.5 * EMU_PER_POINT
                        if bounds is None or bounds[0] < -tolerance or bounds[1] < -tolerance or bounds[2] > result["slide_width"] + tolerance or bounds[3] > result["slide_height"] + tolerance:
                            result["out_of_bounds"] += 1
                    if element and element["type"] == "image" and not _media_is_valid(shape, relationships, archive_names):
                        result["missing_media"] += 1
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, ET.ParseError) as exc:
        _failure(hard_failures, "INVALID_PPTX", str(exc))
    return result


def _package_revision(skill_dir: Path) -> str:
    override = os.environ.get("IVT_SKILL_REVISION")
    if override:
        return override
    try:
        completed = subprocess.run(["git", "-C", str(skill_dir), "status", "--porcelain", "--untracked-files=no"], capture_output=True, text=True, encoding="utf-8", timeout=10, check=False)
        head = subprocess.run(["git", "-C", str(skill_dir), "rev-parse", "HEAD"], capture_output=True, text=True, encoding="utf-8", timeout=10, check=False)
        if completed.returncode == 0 and not completed.stdout.strip() and head.returncode == 0:
            return head.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    digest = hashlib.sha256()
    roots = [skill_dir / "SKILL.md", skill_dir / "agents", skill_dir / "references", skill_dir / "schemas", skill_dir / "scripts"]
    files: list[Path] = []
    for root in roots:
        if root.is_file():
            files.append(root)
        elif root.is_dir():
            files.extend(path for path in root.rglob("*") if path.is_file() and "node_modules" not in path.parts and "__pycache__" not in path.parts and not path.name.endswith((".pyc", ".tmp")))
    for path in sorted(files, key=lambda item: item.relative_to(skill_dir).as_posix()):
        digest.update(path.relative_to(skill_dir).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return f"package-sha256:{digest.hexdigest()}"


def _build_time() -> str:
    raw = os.environ.get("SOURCE_DATE_EPOCH")
    moment = datetime.fromtimestamp(int(raw), timezone.utc) if raw is not None else datetime.now(timezone.utc)
    return moment.isoformat().replace("+00:00", "Z")


def _version(distribution: str) -> str:
    try:
        return importlib.metadata.version(distribution)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def verify_ppt(args: argparse.Namespace) -> dict[str, Any]:
    if args.output.exists():
        raise AssetError("QA report already exists", path=str(args.output), code="output_collision")
    iteration_dir = args.iteration_dir.resolve()
    if not iteration_dir.is_dir():
        raise AssetError("iteration directory does not exist", path=str(args.iteration_dir), code="missing_input", exit_code=3)
    work_root = args.request.parent.resolve()
    try:
        iteration_dir.relative_to(work_root)
    except ValueError as exc:
        raise AssetError("iteration directory escapes request work root", path=str(args.iteration_dir), code="path_escape") from exc
    for candidate in (args.ppt, args.layout, args.crops, args.asset_manifest, args.build_summary, args.font_audit, args.render, args.render_report, args.output):
        try:
            candidate.resolve().relative_to(iteration_dir)
        except ValueError as exc:
            raise AssetError("iteration input/output escapes iteration directory", path=str(candidate), code="path_escape") from exc
    for candidate in (args.request, args.source):
        try:
            candidate.resolve().relative_to(work_root)
        except ValueError as exc:
            raise AssetError("work input escapes request work root", path=str(candidate), code="path_escape") from exc
        if not candidate.is_file():
            raise AssetError("required input does not exist", path=str(candidate), code="missing_input", exit_code=3)

    request = load_contract("request", args.request, args.schema_dir)
    layout = load_contract("layout", args.layout, args.schema_dir)
    crops = load_contract("crops", args.crops, args.schema_dir)
    manifest = load_contract("asset_manifest", args.asset_manifest, args.schema_dir)
    summary = load_contract("build_summary", args.build_summary, args.schema_dir)
    font_audit = load_contract("font_audit", args.font_audit, args.schema_dir)
    render_report = load_contract("render_report", args.render_report, args.schema_dir)
    cross_validate({"request": request, "layout": layout, "crops": crops, "asset_manifest": manifest})
    validate_build_ready(args.asset_manifest, manifest)
    iteration = args.iteration or layout["metadata"]["iteration"]
    if layout["metadata"]["iteration"] != iteration or summary["iteration"] != iteration:
        raise AssetError("iteration does not match layout/build summary", code="iteration_mismatch")
    for candidate in (args.ppt, args.render):
        if not candidate.is_file():
            raise AssetError("required generated artifact does not exist", path=str(candidate), code="missing_input", exit_code=3)

    hard_failures: list[str] = []
    warnings = list(font_audit["warnings"]) + list(render_report["warnings"])
    layout_hash, manifest_hash, ppt_hash, render_hash = sha256_file(args.layout), sha256_file(args.asset_manifest), sha256_file(args.ppt), sha256_file(args.render)
    if summary["hashes"]["layout_sha256"] != layout_hash:
        _failure(hard_failures, "LAYOUT_HASH_CONFLICT", "build summary does not match layout")
    if summary["hashes"]["asset_manifest_sha256"] != manifest_hash:
        _failure(hard_failures, "MANIFEST_HASH_CONFLICT", "build summary does not match asset manifest")
    if summary["hashes"]["output_pptx_sha256"] != ppt_hash or font_audit["ppt_sha256"] != ppt_hash or render_report["ppt_sha256"] != ppt_hash:
        _failure(hard_failures, "PPT_HASH_CONFLICT", "PPT hash differs between reports")
    if render_report["render_sha256"] != render_hash:
        _failure(hard_failures, "RENDER_HASH_CONFLICT", "render hash does not match render report")

    inspection = _inspect_pptx(args.ppt, layout, summary, hard_failures)
    expected = {item["id"]: item for item in layout["elements"]}
    actual_base_ids = {_base_id(name) for name in inspection["objects"]}
    missing_ids = sorted(set(expected) - actual_base_ids)
    unexpected_ids = sorted((actual_base_ids - set(expected)) | {f"slide-root:{name}" for name in inspection["untracked_names"]})
    if inspection["duplicate_names"]:
        _failure(hard_failures, "DUPLICATE_OBJECT_NAMES", ", ".join(sorted(set(inspection["duplicate_names"]))))
    summary_names = {name for item in summary["element_map"] for name in item["object_names"]}
    actual_names = set(inspection["objects"])
    if summary_names != actual_names:
        _failure(hard_failures, "BUILD_MAP_CONFLICT", "actual object names do not match build summary")
    if missing_ids:
        _failure(hard_failures, "MISSING_ELEMENT_IDS", ", ".join(missing_ids))
    if unexpected_ids:
        _failure(hard_failures, "UNEXPECTED_ELEMENT_IDS", ", ".join(unexpected_ids))

    native_missing = 0
    required_text = [item for item in layout["elements"] if item["type"] == "text" and item.get("editability_required", True)]
    editable_text_count = 0
    for element in layout["elements"]:
        shapes = [shape for name, shape in inspection["objects"].items() if _base_id(name) == element["id"]]
        matching = bool(shapes) and all(_matches_native_type(shape, element) for shape in shapes)
        if element.get("editable", False) and not matching:
            native_missing += 1
        if element in required_text and matching:
            editable_text_count += 1
    if native_missing:
        _failure(hard_failures, "MISSING_REQUIRED_NATIVE_OBJECTS", str(native_missing))
    text_status = "applicable" if required_text else "not_applicable"
    text_ratio = editable_text_count / len(required_text) if required_text else None
    if text_ratio is not None and text_ratio != 1.0:
        _failure(hard_failures, "EDITABLE_TEXT_RATIO", f"{text_ratio:.6f}")

    manifest_by_id = {item["id"]: item for item in manifest["assets"]}
    invalid_exemptions = 0
    for element in layout["elements"]:
        if element["type"] != "image":
            continue
        asset = manifest_by_id[element["asset_id"]]
        fields_match = element.get("contains_text", asset["contains_text"]) == asset["contains_text"] and element.get("text_editability_exempt", asset["text_editability_exempt"]) == asset["text_editability_exempt"]
        if asset["contains_text"]:
            fields_match = fields_match and asset["text_editability_exempt"] and bool(asset.get("exemption_reason")) and element.get("exemption_reason", asset.get("exemption_reason")) == asset.get("exemption_reason")
        if not fields_match:
            invalid_exemptions += 1
    if invalid_exemptions:
        _failure(hard_failures, "INVALID_TEXT_EXEMPTIONS", str(invalid_exemptions))

    if inspection["slide_count"] != 1:
        _failure(hard_failures, "SLIDE_COUNT", str(inspection["slide_count"]))
    if not expected:
        _failure(hard_failures, "EMPTY_SLIDE", "layout contains no elements")
    if inspection["slide_width"] and (abs(inspection["slide_width"] / 914400 - layout["slide"]["width_in"]) > 0.5 / 72 or abs(inspection["slide_height"] / 914400 - layout["slide"]["height_in"]) > 0.5 / 72):
        _failure(hard_failures, "SLIDE_SIZE_MISMATCH", "PPT dimensions differ from layout")
    if inspection["out_of_bounds"]:
        _failure(hard_failures, "OUT_OF_BOUNDS_SHAPES", str(inspection["out_of_bounds"]))
    if inspection["missing_media"]:
        _failure(hard_failures, "MISSING_MEDIA", str(inspection["missing_media"]))
    if font_audit["font_violations"]:
        _failure(hard_failures, "FONT_VIOLATIONS", str(font_audit["font_violations"]))
    if render_report["rendered_page_count"] != inspection["slide_count"]:
        _failure(hard_failures, "RENDERED_PAGE_COUNT", str(render_report["rendered_page_count"]))

    skill_dir = Path(__file__).resolve().parents[1]
    package = json.loads((skill_dir / "scripts" / "package.json").read_text(encoding="utf-8"))
    report = {
        "schema_version": "1.3",
        "status": "fail" if hard_failures else "pass",
        "iteration": iteration,
        "hard_failures": sorted(set(hard_failures)),
        "warnings": sorted(set(warnings)),
        "metrics": {
            "slide_count": inspection["slide_count"],
            "required_text_count": len(required_text),
            "editable_required_text_count": editable_text_count,
            "editable_text_ratio": text_ratio,
            "editable_text_status": text_status,
            "missing_required_native_objects": native_missing,
            "invalid_text_exemptions": invalid_exemptions,
            "expected_element_count": len(expected),
            "built_element_count": len(actual_base_ids & set(expected)),
            "missing_element_ids": missing_ids,
            "unexpected_element_ids": unexpected_ids,
            "out_of_bounds_shapes": inspection["out_of_bounds"],
            "missing_media": inspection["missing_media"],
            "font_violations": font_audit["font_violations"],
            "rendered_page_count": render_report["rendered_page_count"],
        },
        "rendering": {"renderer": render_report["renderer"], "renderer_version": render_report["renderer_version"], "fallback_used": render_report["fallback_used"]},
        "provenance": {
            "source_sha256": sha256_file(args.source),
            "request_sha256": sha256_file(args.request),
            "layout_sha256": layout_hash,
            "crops_sha256": sha256_file(args.crops),
            "asset_manifest_sha256": manifest_hash,
            "build_summary_sha256": sha256_file(args.build_summary),
            "ppt_sha256": ppt_hash,
            "render_sha256": render_hash,
            "skill_version": "1.3",
            "skill_revision": _package_revision(skill_dir),
            "builder": "PptxGenJS",
            "builder_version": package["dependencies"]["pptxgenjs"],
            "python_version": platform.python_version(),
            "pillow_version": _version("Pillow"),
            "python_pptx_version": _version("python-pptx"),
            "platform": platform.platform(),
            "build_time_utc": _build_time(),
        },
    }
    validate_schema("qa_report", report, args.schema_dir)
    validate_semantics("qa_report", report)
    atomic_write_json(args.output, report)
    return report


def main() -> int:
    args = parser().parse_args()
    try:
        log_event(args.log_file, level="info", component=COMPONENT, event="started", message="Structural QA started", run_id=args.run_id, iteration=args.iteration)
        report = verify_ppt(args)
        exit_code = 0 if report["status"] == "pass" else 8
        log_event(args.log_file, level="info" if exit_code == 0 else "error", component=COMPONENT, event="completed" if exit_code == 0 else "structural_failed", message="Structural QA completed", run_id=args.run_id, iteration=report["iteration"], data={"status": report["status"], "exit_code": exit_code})
        if exit_code == 0:
            return success(COMPONENT, {"qa_report": str(args.output.resolve()), "status": "pass", "ppt_sha256": report["provenance"]["ppt_sha256"], "render_sha256": report["provenance"]["render_sha256"]}, run_id=args.run_id, iteration=report["iteration"])
        print(json.dumps({"status": "error", "component": COMPONENT, "run_id": args.run_id, "iteration": report["iteration"], "outputs": {"qa_report": str(args.output.resolve())}, "error": {"exit_code": 8, "category": "structural_qa_failed", "message": "structural QA hard gate failed", "path": str(args.output)}}, ensure_ascii=False, sort_keys=True))
        return 8
    except Exception as exc:
        log_event(args.log_file, level="error", component=COMPONENT, event="failed", message=str(exc), run_id=args.run_id, iteration=args.iteration, data={"exit_code": getattr(exc, "exit_code", 70)})
        return failure(COMPONENT, exc, run_id=args.run_id, iteration=args.iteration)


if __name__ == "__main__":
    raise SystemExit(main())
