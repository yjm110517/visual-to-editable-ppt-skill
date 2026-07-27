from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from PIL import Image

from asset_common import AssetError, failure, load_contract, log_event, resolve_under, sha256_file, success
from schema_utils import ContractError, cross_validate, validate_build_ready


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Validate the build-ready asset allowlist.")
    result.add_argument("--asset-dir", required=True, type=Path)
    result.add_argument("--asset-manifest", required=True, type=Path)
    result.add_argument("--layout", type=Path)
    result.add_argument("--svg-report", type=Path)
    result.add_argument("--emit-resolved-assets", action="store_true")
    result.add_argument("--schema-dir", type=Path, default=Path(__file__).resolve().parents[1] / "schemas")
    result.add_argument("--log-file", type=Path)
    result.add_argument("--run-id", default="local")
    result.add_argument("--iteration", type=int)
    return result


def _load_svg_results(report_path: Path) -> dict[str, dict[str, Any]]:
    if not report_path.is_file():
        raise AssetError("SVG security report is required", path=str(report_path), code="missing_svg_report")
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AssetError("SVG security report is unreadable", path=str(report_path), code="invalid_svg_report") from exc
    if report.get("schema_version") != "1.3" or not isinstance(report.get("results"), list):
        raise AssetError("SVG security report has an invalid contract", path=str(report_path), code="invalid_svg_report")
    return {item["asset_id"]: item for item in report["results"] if isinstance(item, dict) and "asset_id" in item}


def resolve_asset(asset_id: str, *, manifest: dict[str, Any], asset_dir: Path, manifest_path: Path) -> Path:
    matches = [item for item in manifest["assets"] if item["id"] == asset_id]
    if len(matches) != 1:
        raise AssetError("unknown or duplicate asset id", path=asset_id, code="unknown_asset")
    path = resolve_under(manifest_path.parent, matches[0]["path"])
    try:
        path.relative_to(asset_dir.resolve())
    except ValueError as exc:
        raise AssetError("manifest asset is outside asset-dir", path=matches[0]["path"], code="path_escape") from exc
    return path


def validate_asset_set(*, asset_dir: Path, manifest_path: Path, schema_dir: Path, layout_path: Path | None = None, svg_report_path: Path | None = None) -> tuple[dict[str, Any], dict[str, Path]]:
    manifest = load_contract("asset_manifest", manifest_path, schema_dir)
    try:
        validate_build_ready(manifest_path, manifest)
    except ContractError as exc:
        raise AssetError(str(exc), path=str(manifest_path), code="contract_error") from exc
    layout = load_contract("layout", layout_path, schema_dir) if layout_path else None
    if layout:
        try:
            cross_validate({"layout": layout, "asset_manifest": manifest})
        except ContractError as exc:
            raise AssetError(str(exc), path=str(layout_path), code="contract_error") from exc
    svg_assets = [item for item in manifest["assets"] if item["type"] == "svg"]
    report_path = svg_report_path or manifest_path.parent / "svg_security_report.json"
    svg_results = _load_svg_results(report_path) if svg_assets else {}
    paths: dict[str, Path] = {}
    allowed_extensions = {"png": {".png"}, "jpeg": {".jpg", ".jpeg"}, "svg": {".svg"}}
    for index, item in enumerate(manifest["assets"]):
        path = resolve_asset(item["id"], manifest=manifest, asset_dir=asset_dir, manifest_path=manifest_path)
        if not path.is_file():
            raise AssetError("manifest asset is missing", path=item["path"], code="missing_asset", exit_code=3)
        if path.suffix.lower() not in allowed_extensions[item["type"]]:
            raise AssetError("asset extension does not match manifest type", path=item["path"], code="type_mismatch")
        if path.stat().st_size != item["size_bytes"] or sha256_file(path) != item["sha256"].lower():
            raise AssetError("asset size or hash does not match manifest", path=item["path"], code="integrity_mismatch")
        if item["type"] in {"png", "jpeg"}:
            try:
                with Image.open(path) as image:
                    image.verify()
                with Image.open(path) as image:
                    expected_format = "PNG" if item["type"] == "png" else "JPEG"
                    if image.format != expected_format or image.width != item["width_px"] or image.height != item["height_px"]:
                        raise AssetError("raster format or dimensions do not match manifest", path=item["path"], code="type_mismatch")
            except OSError as exc:
                raise AssetError("raster asset is unreadable", path=item["path"], code="unreadable_asset") from exc
        else:
            report = svg_results.get(item["id"])
            report_matches = report and all(
                (
                    report.get("status") == "passed",
                    report.get("output_path") == item["path"],
                    report.get("sanitized_sha256") == item["sha256"],
                    report.get("view_box") == item["view_box"],
                    report.get("width_px") == item["width_px"],
                    report.get("height_px") == item["height_px"],
                )
            )
            if not report_matches:
                raise AssetError("SVG is missing a matching passed security report", path=item["path"], code="svg_report_mismatch")
        paths[item["id"]] = path
    return manifest, paths


def main() -> int:
    args = parser().parse_args()
    component = "validate_assets"
    try:
        log_event(args.log_file, level="info", component=component, event="started", message="Asset allowlist validation started", run_id=args.run_id, iteration=args.iteration)
        manifest, paths = validate_asset_set(asset_dir=args.asset_dir, manifest_path=args.asset_manifest, schema_dir=args.schema_dir, layout_path=args.layout, svg_report_path=args.svg_report)
        outputs = {"asset_manifest": str(args.asset_manifest.resolve()), "asset_count": len(paths), "asset_ids": sorted(paths)}
        if args.emit_resolved_assets:
            if not args.layout:
                raise AssetError("--layout is required with --emit-resolved-assets", path="--layout", code="contract_error")
            by_id = {item["id"]: item for item in manifest["assets"]}
            outputs.update({
                "manifest_sha256": sha256_file(args.asset_manifest),
                "layout_sha256": sha256_file(args.layout),
                "resolved_assets": [
                    {
                        "id": asset_id,
                        "type": by_id[asset_id]["type"],
                        "path": str(paths[asset_id]),
                        "width_px": by_id[asset_id]["width_px"],
                        "height_px": by_id[asset_id]["height_px"],
                        "size_bytes": by_id[asset_id]["size_bytes"],
                        "sha256": by_id[asset_id]["sha256"].lower(),
                    }
                    for asset_id in sorted(paths)
                ],
            })
        log_event(args.log_file, level="info", component=component, event="completed", message="Asset allowlist validation completed", run_id=args.run_id, iteration=args.iteration, data={"count": len(paths), "exit_code": 0})
        return success(component, outputs, run_id=args.run_id, iteration=args.iteration)
    except Exception as exc:
        log_event(args.log_file, level="error", component=component, event="failed", message=str(exc), run_id=args.run_id, iteration=args.iteration, data={"exit_code": getattr(exc, "exit_code", 70)})
        return failure(component, exc, run_id=args.run_id, iteration=args.iteration)


if __name__ == "__main__":
    raise SystemExit(main())
