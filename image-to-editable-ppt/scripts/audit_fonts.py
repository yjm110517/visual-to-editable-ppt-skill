from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from asset_common import AssetError, atomic_write_json, failure, load_contract, log_event, sha256_file, success
from schema_utils import validate_schema, validate_semantics


NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
}
COMPONENT = "audit_fonts"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Audit native PowerPoint text runs and installed fonts.")
    result.add_argument("--ppt", required=True, type=Path)
    result.add_argument("--layout", required=True, type=Path)
    result.add_argument("--build-summary", required=True, type=Path)
    result.add_argument("--output", required=True, type=Path)
    result.add_argument("--schema-dir", type=Path, default=Path(__file__).resolve().parents[1] / "schemas")
    result.add_argument("--log-file", type=Path)
    result.add_argument("--run-id", default="local")
    result.add_argument("--iteration", type=int)
    return result


def _normalize_font(value: str) -> str:
    return " ".join(value.split()).casefold()


def installed_font_names() -> set[str]:
    override = os.environ.get("IVT_AVAILABLE_FONTS")
    if override is not None:
        return {_normalize_font(item) for item in override.split(";") if item.strip()}
    names: set[str] = set()
    if os.name == "nt":
        try:
            import winreg

            keys = (
                (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"),
                (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"),
            )
            for hive, key_name in keys:
                try:
                    with winreg.OpenKey(hive, key_name) as key:
                        for index in range(winreg.QueryInfoKey(key)[1]):
                            display = winreg.EnumValue(key, index)[0].split(" (", 1)[0]
                            for family in display.split(" & "):
                                if family.strip():
                                    names.add(_normalize_font(family))
                except OSError:
                    continue
        except ImportError:
            pass
    else:
        executable = shutil.which("fc-list")
        if executable:
            completed = subprocess.run([executable, ":", "family"], capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
            if completed.returncode == 0:
                for line in completed.stdout.splitlines():
                    for family in line.split(","):
                        if family.strip():
                            names.add(_normalize_font(family))
    return names


def _object_name(shape: ET.Element) -> str:
    item = shape.find(".//p:cNvPr", NS)
    return item.attrib.get("name", "unnamed") if item is not None else "unnamed"


def _expected_runs(build_summary: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    values: dict[str, list[dict[str, Any]]] = {}
    for item in build_summary["typography"]["font_resolutions"]:
        values.setdefault(f"ivt:{item['element_id']}", []).append(item)
    for items in values.values():
        items.sort(key=lambda item: item["run_index"])
    return values


def _run_record(
    run: ET.Element,
    *,
    slide_index: int,
    object_name: str,
    container: str,
    paragraph_index: int,
    run_index: int,
    expected: dict[str, Any] | None,
    installed: set[str],
) -> dict[str, Any]:
    properties = run.find("a:rPr", NS)
    actual_faces: dict[str, str | None] = {}
    for kind in ("latin", "ea", "cs"):
        node = properties.find(f"a:{kind}", NS) if properties is not None else None
        actual_faces[kind] = node.attrib.get("typeface") if node is not None else None
    raw_size = properties.attrib.get("sz") if properties is not None else None
    actual_size = int(raw_size) / 100 if raw_size and raw_size.isdigit() else None
    expected_face = expected.get("font_face") if expected else None
    expected_size = expected.get("font_size_pt") if expected else None
    violations: list[str] = []
    is_installed: bool | None = None
    if expected_face:
        is_installed = _normalize_font(expected_face) in installed
        if not is_installed:
            violations.append("FONT_NOT_INSTALLED")
        for kind, face in actual_faces.items():
            if face != expected_face:
                violations.append(f"{kind.upper()}_FONT_MISMATCH")
    if expected_size is not None and (actual_size is None or abs(actual_size - float(expected_size)) > 0.01):
        violations.append("FONT_SIZE_MISMATCH")
    if expected is None:
        violations.append("UNEXPECTED_TEXT_RUN")
    return {
        "slide_index": slide_index,
        "object_name": object_name,
        "container": container,
        "paragraph_index": paragraph_index,
        "run_index": run_index,
        "expected_font_face": expected_face,
        "expected_size_pt": expected_size,
        "actual_font_faces": actual_faces,
        "actual_size_pt": actual_size,
        "installed": is_installed,
        "compliant": not violations,
        "violations": sorted(set(violations)),
    }


def audit_fonts(args: argparse.Namespace) -> dict[str, Any]:
    if not args.ppt.is_file():
        raise AssetError("PPTX input does not exist", path=str(args.ppt), code="missing_input", exit_code=3)
    if args.output.exists():
        raise AssetError("font audit output already exists", path=str(args.output), code="output_collision")
    layout = load_contract("layout", args.layout, args.schema_dir)
    summary = load_contract("build_summary", args.build_summary, args.schema_dir)
    if args.iteration is not None and (layout["metadata"]["iteration"] != args.iteration or summary["iteration"] != args.iteration):
        raise AssetError("iteration does not match layout/build summary", code="iteration_mismatch")
    if summary["hashes"]["output_pptx_sha256"] != sha256_file(args.ppt):
        raise AssetError("PPTX hash does not match build summary", path=str(args.ppt), code="hash_conflict", exit_code=9)
    expected_by_object = _expected_runs(summary)
    installed = installed_font_names()
    warnings: list[str] = []
    if not installed:
        raise AssetError("installed fonts could not be enumerated", code="font_environment_unavailable", exit_code=5)
    records: list[dict[str, Any]] = []
    seen_counts: dict[str, int] = {}
    try:
        with zipfile.ZipFile(args.ppt) as archive:
            slide_names = sorted(
                (name for name in archive.namelist() if name.startswith("ppt/slides/slide") and name.endswith(".xml")),
                key=lambda value: int(Path(value).stem.removeprefix("slide")),
            )
            for slide_index, slide_name in enumerate(slide_names, start=1):
                root = ET.fromstring(archive.read(slide_name))
                tree = root.find(".//p:spTree", NS)
                if tree is None:
                    continue
                for shape in list(tree):
                    object_name = _object_name(shape)
                    text_bodies: list[tuple[str, ET.Element]] = []
                    direct = shape.find("p:txBody", NS)
                    if direct is not None:
                        text_bodies.append(("text_box", direct))
                    for body in shape.findall(".//a:tc/a:txBody", NS):
                        text_bodies.append(("table_cell", body))
                    for container, body in text_bodies:
                        object_run_index = seen_counts.get(object_name, 0)
                        for paragraph_index, paragraph in enumerate(body.findall("a:p", NS)):
                            for run in list(paragraph):
                                if run.tag not in {f"{{{NS['a']}}}r", f"{{{NS['a']}}}fld"}:
                                    continue
                                expected_items = expected_by_object.get(object_name, [])
                                expected = expected_items[object_run_index] if object_run_index < len(expected_items) else None
                                records.append(_run_record(run, slide_index=slide_index, object_name=object_name, container=container, paragraph_index=paragraph_index, run_index=object_run_index, expected=expected, installed=installed))
                                object_run_index += 1
                        seen_counts[object_name] = object_run_index
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as exc:
        raise AssetError("PPTX package is unreadable", path=str(args.ppt), code="invalid_pptx", exit_code=3) from exc

    for object_name, expected_items in expected_by_object.items():
        actual_count = seen_counts.get(object_name, 0)
        for expected in expected_items[actual_count:]:
            records.append({
                "slide_index": 1,
                "object_name": object_name,
                "container": "text_box",
                "paragraph_index": 0,
                "run_index": expected["run_index"],
                "expected_font_face": expected["font_face"],
                "expected_size_pt": expected["font_size_pt"],
                "actual_font_faces": {"latin": None, "ea": None, "cs": None},
                "actual_size_pt": None,
                "installed": _normalize_font(expected["font_face"]) in installed,
                "compliant": False,
                "violations": ["MISSING_TEXT_RUN"],
            })
    tracked_objects = set(expected_by_object)
    for record in records:
        if record["object_name"] not in tracked_objects and "untracked text object" not in warnings:
            warnings.append("untracked text object")
    used_fonts = sorted({record["expected_font_face"] for record in records if record["expected_font_face"]})
    missing_fonts = sorted({font for font in used_fonts if _normalize_font(font) not in installed})
    violation_count = sum(len(record["violations"]) for record in records)
    report = {
        "schema_version": "1.3",
        "status": "pass" if violation_count == 0 else "fail",
        "ppt_sha256": sha256_file(args.ppt),
        "font_violations": violation_count,
        "used_fonts": used_fonts,
        "missing_fonts": missing_fonts,
        "runs": records,
        "warnings": sorted(set(warnings)),
    }
    validate_schema("font_audit", report, args.schema_dir)
    validate_semantics("font_audit", report)
    atomic_write_json(args.output, report)
    return report


def main() -> int:
    args = parser().parse_args()
    try:
        log_event(args.log_file, level="info", component=COMPONENT, event="started", message="Font audit started", run_id=args.run_id, iteration=args.iteration)
        report = audit_fonts(args)
        log_event(args.log_file, level="info", component=COMPONENT, event="completed", message="Font audit completed", run_id=args.run_id, iteration=args.iteration, data={"font_violations": report["font_violations"], "exit_code": 0})
        return success(COMPONENT, {"font_audit": str(args.output.resolve()), "font_violations": report["font_violations"], "status": report["status"]}, run_id=args.run_id, iteration=args.iteration)
    except Exception as exc:
        log_event(args.log_file, level="error", component=COMPONENT, event="failed", message=str(exc), run_id=args.run_id, iteration=args.iteration, data={"exit_code": getattr(exc, "exit_code", 70)})
        return failure(COMPONENT, exc, run_id=args.run_id, iteration=args.iteration)


if __name__ == "__main__":
    raise SystemExit(main())
