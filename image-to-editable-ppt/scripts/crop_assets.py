from __future__ import annotations

import argparse
import os
import tempfile
import warnings
from pathlib import Path
from typing import Any

from PIL import Image

from asset_common import AssetError, atomic_write_json, failure, index_assets, load_contract, log_event, manifest_relative_path, resolve_under, sha256_file, success


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Crop safe raster assets and update asset_manifest.json atomically.")
    result.add_argument("--input", required=True, type=Path)
    result.add_argument("--spec", required=True, type=Path)
    result.add_argument("--output-dir", required=True, type=Path)
    result.add_argument("--asset-manifest", required=True, type=Path)
    result.add_argument("--schema-dir", type=Path, default=Path(__file__).resolve().parents[1] / "schemas")
    result.add_argument("--max-source-pixels", type=int, default=100_000_000)
    result.add_argument("--log-file", type=Path)
    result.add_argument("--run-id", default="local")
    result.add_argument("--iteration", type=int)
    return result


def _preflight(spec: dict[str, Any], manifest: dict[str, Any], source_size: tuple[int, int], output_dir: Path, manifest_path: Path) -> list[dict[str, Any]]:
    width, height = source_size
    manifest_assets = index_assets(manifest)
    operations = []
    for index, crop in enumerate(spec["assets"]):
        base = f"$.assets[{index}]"
        if crop["id"] not in manifest_assets:
            raise AssetError("crop asset is missing from manifest", path=base + ".id")
        if crop["remove_background"]:
            raise AssetError("automatic background removal is not supported in P2", path=base + ".remove_background", code="unsupported_operation")
        suffix = Path(crop["output"]).suffix.lower()
        if suffix not in {".png", ".jpg", ".jpeg"}:
            raise AssetError("output must be PNG or JPEG", path=base + ".output", code="unsupported_format")
        if suffix in {".jpg", ".jpeg"} and crop["mode"] != "rgb":
            raise AssetError("JPEG output requires rgb mode", path=base + ".mode", code="mode_mismatch")
        left, top, right, bottom = crop["box_px"]
        padding = crop["padding_px"]
        padded = (left - padding, top - padding, right + padding, bottom + padding)
        if padded[0] < 0 or padded[1] < 0 or padded[2] > width or padded[3] > height:
            raise AssetError("crop box including padding exceeds source bounds", path=base + ".box_px", code="crop_out_of_bounds")
        destination = resolve_under(output_dir, crop["output"], filename_only=True)
        if destination.exists():
            raise AssetError("output already exists", path=base + ".output", code="output_collision")
        relative = manifest_relative_path(manifest_path, destination)
        operations.append({"crop": crop, "box": padded, "destination": destination, "relative": relative, "manifest": manifest_assets[crop["id"]], "type": "png" if suffix == ".png" else "jpeg"})
    return operations


def crop_assets(args: argparse.Namespace) -> dict[str, Any]:
    if args.max_source_pixels < 1:
        raise AssetError("max-source-pixels must be positive", path="--max-source-pixels")
    spec = load_contract("crops", args.spec, args.schema_dir)
    manifest = load_contract("asset_manifest", args.asset_manifest, args.schema_dir)
    if not args.input.is_file():
        raise AssetError("source image does not exist", path=str(args.input), code="missing_input", exit_code=3)
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    previous_pixel_limit = Image.MAX_IMAGE_PIXELS
    try:
        Image.MAX_IMAGE_PIXELS = args.max_source_pixels
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(args.input) as opened:
                opened.load()
                source = opened.copy()
    except (Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
        raise AssetError("source image exceeds safe pixel limit", path=str(args.input), code="image_too_large") from exc
    except OSError as exc:
        raise AssetError("source image is unreadable", path=str(args.input), code="unreadable_image", exit_code=3) from exc
    finally:
        Image.MAX_IMAGE_PIXELS = previous_pixel_limit
    operations = _preflight(spec, manifest, source.size, output_dir, args.asset_manifest)
    committed: list[Path] = []
    staged: list[tuple[Path, dict[str, Any]]] = []
    with tempfile.TemporaryDirectory(prefix=".crop-stage-", dir=output_dir.parent) as temporary:
        stage_dir = Path(temporary)
        for operation in operations:
            crop = source.crop(operation["box"])
            mode = "RGBA" if operation["crop"]["mode"] == "rgba" else "RGB"
            crop = crop.convert(mode)
            staged_path = stage_dir / operation["destination"].name
            if operation["type"] == "png":
                crop.save(staged_path, format="PNG", optimize=False, compress_level=9)
            else:
                crop.save(staged_path, format="JPEG", quality=95, subsampling=0, optimize=False, progressive=False)
            item = operation["manifest"]
            item.update({"type": operation["type"], "path": operation["relative"], "width_px": crop.width, "height_px": crop.height, "size_bytes": staged_path.stat().st_size, "sha256": sha256_file(staged_path), "security_status": "passed"})
            staged.append((staged_path, operation))
        try:
            for staged_path, operation in staged:
                operation["destination"].parent.mkdir(parents=True, exist_ok=True)
                os.replace(staged_path, operation["destination"])
                committed.append(operation["destination"])
            atomic_write_json(args.asset_manifest, manifest)
        except Exception:
            for path in committed:
                path.unlink(missing_ok=True)
            raise
    return {"asset_manifest": str(args.asset_manifest.resolve()), "assets": [{"id": operation["crop"]["id"], "path": str(operation["destination"]), "sha256": operation["manifest"]["sha256"]} for operation in operations]}


def main() -> int:
    args = parser().parse_args()
    component = "crop_assets"
    try:
        log_event(args.log_file, level="info", component=component, event="started", message="Raster asset crop started", run_id=args.run_id, iteration=args.iteration)
        outputs = crop_assets(args)
        log_event(args.log_file, level="info", component=component, event="completed", message="Raster asset crop completed", run_id=args.run_id, iteration=args.iteration, data={"count": len(outputs["assets"]), "exit_code": 0})
        return success(component, outputs, run_id=args.run_id, iteration=args.iteration)
    except Exception as exc:
        log_event(args.log_file, level="error", component=component, event="failed", message=str(exc), run_id=args.run_id, iteration=args.iteration, data={"exit_code": getattr(exc, "exit_code", 70)})
        return failure(component, exc, run_id=args.run_id, iteration=args.iteration)


if __name__ == "__main__":
    raise SystemExit(main())
