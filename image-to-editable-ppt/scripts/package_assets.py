from __future__ import annotations

import argparse
import os
import tempfile
import zipfile
from pathlib import Path

from asset_common import AssetError, canonical_json_bytes, failure, log_event, sha256_file, success
from validate_assets import validate_asset_set


FIXED_TIME = (1980, 1, 1, 0, 0, 0)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Create a deterministic ZIP from the validated asset allowlist.")
    result.add_argument("--asset-dir", required=True, type=Path)
    result.add_argument("--asset-manifest", required=True, type=Path)
    result.add_argument("--output", required=True, type=Path)
    result.add_argument("--layout", type=Path)
    result.add_argument("--svg-report", type=Path)
    result.add_argument("--schema-dir", type=Path, default=Path(__file__).resolve().parents[1] / "schemas")
    result.add_argument("--log-file", type=Path)
    result.add_argument("--run-id", default="local")
    result.add_argument("--iteration", type=int)
    return result


def _entry(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, FIXED_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (0o100644 & 0xFFFF) << 16
    return info


def package_assets(args: argparse.Namespace) -> dict:
    output = args.output.resolve()
    if output.exists():
        raise AssetError("output ZIP already exists", path=str(output), code="output_collision")
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest, paths = validate_asset_set(asset_dir=args.asset_dir, manifest_path=args.asset_manifest, schema_dir=args.schema_dir, layout_path=args.layout, svg_report_path=args.svg_report)
    manifest_bytes = canonical_json_bytes(manifest)
    entries: list[tuple[str, bytes]] = [("asset_manifest.json", manifest_bytes)]
    for item in manifest["assets"]:
        entries.append((item["path"], paths[item["id"]].read_bytes()))
    entries.sort(key=lambda item: item[0])
    with tempfile.TemporaryDirectory(prefix=".asset-zip-", dir=output.parent) as temporary:
        staged = Path(temporary) / output.name
        with zipfile.ZipFile(staged, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name, content in entries:
                archive.writestr(_entry(name), content, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
        expected_names = [name for name, _ in entries]
        with zipfile.ZipFile(staged, "r") as archive:
            if sorted(archive.namelist()) != sorted(expected_names):
                raise AssetError("ZIP entry set does not match manifest", path=str(output), code="zip_integrity")
            if archive.read("asset_manifest.json") != manifest_bytes:
                raise AssetError("ZIP internal manifest differs", path=str(output), code="zip_integrity")
            for item in manifest["assets"]:
                if archive.read(item["path"]) != paths[item["id"]].read_bytes():
                    raise AssetError("ZIP asset content differs", path=item["path"], code="zip_integrity")
        os.replace(staged, output)
    return {"zip": str(output), "sha256": sha256_file(output), "entries": [name for name, _ in entries]}


def main() -> int:
    args = parser().parse_args()
    component = "package_assets"
    try:
        log_event(args.log_file, level="info", component=component, event="started", message="Asset package started", run_id=args.run_id, iteration=args.iteration)
        outputs = package_assets(args)
        log_event(args.log_file, level="info", component=component, event="completed", message="Asset package completed", run_id=args.run_id, iteration=args.iteration, data={"entries": len(outputs["entries"]), "exit_code": 0})
        return success(component, outputs, run_id=args.run_id, iteration=args.iteration)
    except Exception as exc:
        log_event(args.log_file, level="error", component=component, event="failed", message=str(exc), run_id=args.run_id, iteration=args.iteration, data={"exit_code": getattr(exc, "exit_code", 70)})
        return failure(component, exc, run_id=args.run_id, iteration=args.iteration)


if __name__ == "__main__":
    raise SystemExit(main())
