from __future__ import annotations

import argparse
import math
import os
import platform
import statistics
import tempfile
import warnings
from collections import Counter, deque
from pathlib import Path
from typing import Any

from PIL import Image, __version__ as PILLOW_VERSION

from asset_common import AssetError, atomic_write_bytes, atomic_write_json, failure, index_assets, load_contract, log_event, manifest_relative_path, resolve_under, sha256_file, success
from schema_utils import ContractError, cross_validate, validate_schema, validate_semantics


ALGORITHM_ID = "edge-connected-background-v1"
ALGORITHM_VERSION = "1.0.0"
EDGE_RING_PX = 4
BACKGROUND_CLUSTER_MIN = 0.70
BACKGROUND_CONFIDENCE_MIN = 0.80
BACKGROUND_ZERO_DISTANCE = 18.0
BACKGROUND_FEATHER_DISTANCE = 42.0
FOREGROUND_ALPHA = 192
SAFE_MARGIN_PX = 3
QUANTIZATION = 64
BACKGROUND_CONFIDENCE_DISTANCE = 96.0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Crop safe raster assets and update asset_manifest.json atomically.")
    result.add_argument("--input", required=True, type=Path)
    result.add_argument("--spec", required=True, type=Path)
    result.add_argument("--contract-spec", type=Path, help="Full crops contract when --spec contains only pending operations.")
    result.add_argument("--output-dir", required=True, type=Path)
    result.add_argument("--asset-manifest", required=True, type=Path)
    result.add_argument("--processing-report", required=True, type=Path)
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
        suffix = Path(crop["output"]).suffix.lower()
        if suffix not in {".png", ".jpg", ".jpeg"}:
            raise AssetError("output must be PNG or JPEG", path=base + ".output", code="unsupported_format")
        if suffix in {".jpg", ".jpeg"} and crop["mode"] != "rgb":
            raise AssetError("JPEG output requires rgb mode", path=base + ".mode", code="mode_mismatch")
        if crop["remove_background"] and (suffix != ".png" or crop["mode"] != "rgba"):
            raise AssetError("background removal requires PNG and rgba mode", path=base, code="mode_mismatch")
        left, top, right, bottom = crop["box_px"]
        padding = crop["padding_px"]
        padded = (left - padding, top - padding, right + padding, bottom + padding)
        if padded[0] < 0 or padded[1] < 0 or padded[2] > width or padded[3] > height:
            raise AssetError("crop box including padding exceeds source bounds", path=base + ".box_px", code="crop_out_of_bounds")
        relative_exclusions: list[tuple[int, int, int, int]] = []
        for exclusion_index, exclusion in enumerate(crop.get("semantic_exclusion_boxes_px", [])):
            ex_left, ex_top, ex_right, ex_bottom = exclusion
            if ex_left < left or ex_top < top or ex_right > right or ex_bottom > bottom:
                raise AssetError(
                    "semantic exclusion must stay inside the unpadded crop box",
                    path=f"{base}.semantic_exclusion_boxes_px[{exclusion_index}]",
                    code="invalid_semantic_exclusion",
                )
            relative_exclusions.append(
                (
                    ex_left - padded[0],
                    ex_top - padded[1],
                    ex_right - padded[0],
                    ex_bottom - padded[1],
                )
            )
        destination = resolve_under(output_dir, crop["output"], filename_only=True)
        if destination.exists():
            raise AssetError("output already exists", path=base + ".output", code="output_collision")
        relative = manifest_relative_path(manifest_path, destination)
        operations.append(
            {
                "crop": crop,
                "box": padded,
                "exclusions": relative_exclusions,
                "destination": destination,
                "relative": relative,
                "manifest": manifest_assets[crop["id"]],
                "type": "png" if suffix == ".png" else "jpeg",
            }
        )
    return operations


def _edge_pixels(
    image: Image.Image,
    ring: int = EDGE_RING_PX,
    exclusions: list[tuple[int, int, int, int]] | None = None,
) -> list[tuple[int, int, int]]:
    rgb = image.convert("RGB")
    width, height = rgb.size
    ring = max(1, min(ring, width // 2, height // 2))
    pixels = rgb.load()
    exclusions = exclusions or []
    result = [
        pixels[x, y]
        for y in range(height)
        for x in range(width)
        if x < ring or y < ring or x >= width - ring or y >= height - ring
        if not any(left <= x < right and top <= y < bottom for left, top, right, bottom in exclusions)
    ]
    if not result:
        raise AssetError("semantic exclusions consume the complete edge sample", code="invalid_semantic_exclusion")
    return result


def _background_model(
    image: Image.Image,
    exclusions: list[tuple[int, int, int, int]] | None = None,
) -> tuple[tuple[int, int, int], float, float]:
    edge = _edge_pixels(image, exclusions=exclusions)
    bins = Counter(tuple(channel // QUANTIZATION for channel in pixel) for pixel in edge)
    dominant, count = bins.most_common(1)[0]
    members = [pixel for pixel in edge if tuple(channel // QUANTIZATION for channel in pixel) == dominant]
    estimate = tuple(int(statistics.median(pixel[channel] for pixel in members)) for channel in range(3))
    coverage = count / len(edge)
    distances = [math.dist(pixel, estimate) for pixel in members]
    median_distance = statistics.median(distances) if distances else 255.0
    # Coverage and within-cluster consistency are separate gates. Multiplying
    # them would make a valid 80% dominant edge cluster fail even when that
    # cluster itself is extremely consistent.
    confidence = max(0.0, 1.0 - median_distance / BACKGROUND_CONFIDENCE_DISTANCE)
    return estimate, round(coverage, 6), round(confidence, 6)


def _boundary_runs(mask: list[bool]) -> int:
    longest = current = 0
    for value in mask + [False]:
        if value:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def _matte_background(
    image: Image.Image,
    exclusions: list[tuple[int, int, int, int]] | None = None,
) -> tuple[Image.Image, dict[str, Any]]:
    rgba = image.convert("RGBA")
    width, height = rgba.size
    if width < SAFE_MARGIN_PX * 2 + 1 or height < SAFE_MARGIN_PX * 2 + 1:
        raise AssetError(
            "crop is too small for the transparent safety margin",
            code="insufficient_transparent_margin",
            details={
                "measurements": {"width_px": width, "height_px": height},
                "thresholds": {"safe_margin_px": SAFE_MARGIN_PX},
                "recommended_actions": ["expand_crop_box"],
            },
        )
    exclusions = exclusions or []
    estimate, coverage, confidence = _background_model(rgba, exclusions)
    if coverage < BACKGROUND_CLUSTER_MIN or confidence < BACKGROUND_CONFIDENCE_MIN:
        reason = "low_background_coverage" if coverage < BACKGROUND_CLUSTER_MIN else "low_background_confidence"
        raise AssetError(
            f"background confidence is insufficient: coverage={coverage}, confidence={confidence}",
            code="background_confidence",
            details={
                "failure_reason": reason,
                "measurements": {
                    "background_cluster_coverage": coverage,
                    "background_confidence": confidence,
                    "estimated_background": list(estimate),
                },
                "thresholds": {
                    "background_cluster_min": BACKGROUND_CLUSTER_MIN,
                    "background_confidence_min": BACKGROUND_CONFIDENCE_MIN,
                },
                "recommended_actions": [
                    "adjust_crop_box_to_sample_a_more_consistent_outer_background",
                    "use_source_tile_only_if_the_reference_contains_a_complete_deliberate_tile",
                ],
            },
        )
    pixels = rgba.load()
    for left, top, right, bottom in exclusions:
        for y in range(top, bottom):
            for x in range(left, right):
                pixels[x, y] = (*estimate, 255)

    def distance(x: int, y: int) -> float:
        return math.dist(pixels[x, y][:3], estimate)

    boundary = (
        [(x, 0) for x in range(width)]
        + [(width - 1, y) for y in range(1, height)]
        + [(x, height - 1) for x in range(width - 2, -1, -1)]
        + [(0, y) for y in range(height - 2, 0, -1)]
    )
    foreground_on_boundary = [distance(x, y) > BACKGROUND_FEATHER_DISTANCE for x, y in boundary]
    longest_boundary_run = _boundary_runs(foreground_on_boundary)
    if longest_boundary_run >= 3:
        raise AssetError(
            "foreground content touches the crop boundary",
            code="foreground_touches_boundary",
            details={
                "measurements": {"foreground_boundary_run_px": longest_boundary_run},
                "thresholds": {"maximum_foreground_boundary_run_px": 2},
                "recommended_actions": [
                    "expand_crop_box_or_padding_without_crossing_the_source_boundary",
                    "adjust_crop_box_to_include_the_complete_shadow_and_highlight",
                ],
            },
        )

    queue: deque[tuple[int, int]] = deque()
    connected: set[tuple[int, int]] = set()
    for x, y in boundary:
        if distance(x, y) <= BACKGROUND_FEATHER_DISTANCE:
            connected.add((x, y))
            queue.append((x, y))
    while queue:
        x, y = queue.popleft()
        for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
            if 0 <= nx < width and 0 <= ny < height and (nx, ny) not in connected:
                if distance(nx, ny) <= BACKGROUND_FEATHER_DISTANCE:
                    connected.add((nx, ny))
                    queue.append((nx, ny))

    output = rgba.copy()
    out = output.load()
    for x, y in connected:
        delta = distance(x, y)
        if delta <= BACKGROUND_ZERO_DISTANCE:
            alpha = 0
        else:
            alpha = round(255 * (delta - BACKGROUND_ZERO_DISTANCE) / (BACKGROUND_FEATHER_DISTANCE - BACKGROUND_ZERO_DISTANCE))
        out[x, y] = (*out[x, y][:3], min(out[x, y][3], max(0, min(255, alpha))))
    for x, y in boundary:
        out[x, y] = (*out[x, y][:3], 0)

    foreground_points = [
        (x, y)
        for y in range(height)
        for x in range(width)
        if out[x, y][3] >= FOREGROUND_ALPHA
    ]
    clearance = min(
        (min(x, y, width - 1 - x, height - 1 - y) for x, y in foreground_points),
        default=min(width, height) // 2,
    )
    if clearance < SAFE_MARGIN_PX:
        raise AssetError(
            f"transparent foreground clearance is {clearance}px; {SAFE_MARGIN_PX}px required",
            code="insufficient_transparent_margin",
            details={
                "measurements": {"foreground_clearance_px": clearance},
                "thresholds": {"safe_margin_px": SAFE_MARGIN_PX},
                "recommended_actions": [
                    "expand_crop_box_or_padding_without_crossing_the_source_boundary",
                    "adjust_crop_box_to_center_the_complete_foreground",
                ],
            },
        )
    edge_alpha_max = max(out[x, y][3] for x, y in boundary)
    return output, {
        "estimated_background": list(estimate),
        "background_cluster_coverage": coverage,
        "background_confidence": confidence,
        "edge_alpha_max": edge_alpha_max,
        "foreground_clearance_px": clearance,
        "foreground_touches_edge": False,
    }


def _opaque_metrics(image: Image.Image) -> dict[str, Any]:
    alpha = image.convert("RGBA").getchannel("A")
    width, height = image.size
    boundary = (
        [(x, 0) for x in range(width)]
        + [(x, height - 1) for x in range(width)]
        + [(0, y) for y in range(height)]
        + [(width - 1, y) for y in range(height)]
    )
    return {
        "estimated_background": None,
        "background_cluster_coverage": 0.0,
        "background_confidence": 0.0,
        "edge_alpha_max": max(alpha.getpixel(point) for point in boundary),
        "foreground_clearance_px": 0,
        "foreground_touches_edge": False,
    }


def crop_assets(args: argparse.Namespace) -> dict[str, Any]:
    if args.max_source_pixels < 1:
        raise AssetError("max-source-pixels must be positive", path="--max-source-pixels")
    spec = load_contract("crops", args.spec, args.schema_dir)
    contract_spec_path = args.contract_spec or args.spec
    contract_spec = load_contract("crops", contract_spec_path, args.schema_dir)
    manifest = load_contract("asset_manifest", args.asset_manifest, args.schema_dir)
    contract_by_id = {item["id"]: item for item in contract_spec["assets"]}
    for item in spec["assets"]:
        if contract_by_id.get(item["id"]) != item:
            raise AssetError("operation spec must match the full crops contract", path=item["id"], code="contract_error")
    try:
        cross_validate({"crops": contract_spec, "asset_manifest": manifest})
    except ContractError as exc:
        raise AssetError(str(exc), code="contract_error") from exc
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
    report_path = args.processing_report.resolve()
    expected_report = args.asset_manifest.resolve().parent / "asset_processing_report.json"
    if report_path != expected_report:
        raise AssetError("processing-report must be next to asset_manifest.json", path=str(report_path), code="path_escape")
    committed: list[Path] = []
    staged: list[tuple[Path, dict[str, Any], dict[str, Any]]] = []
    original_manifest = args.asset_manifest.read_bytes()
    original_report = report_path.read_bytes() if report_path.exists() else None
    previous_report = load_contract("asset_processing_report", report_path, args.schema_dir) if report_path.exists() else None
    with tempfile.TemporaryDirectory(prefix=".crop-stage-", dir=output_dir.parent) as temporary:
        stage_dir = Path(temporary)
        for operation in operations:
            crop = source.crop(operation["box"])
            policy = operation["manifest"]["boundary_policy"]
            if policy == "transparent":
                try:
                    crop, metrics = _matte_background(crop, operation["exclusions"])
                except AssetError as exc:
                    details = {
                        "asset_id": operation["crop"]["id"],
                        "boundary_policy": policy,
                        "box_px": operation["crop"]["box_px"],
                        "padded_box_px": list(operation["box"]),
                        "padding_px": operation["crop"]["padding_px"],
                        **(exc.detail.get("details") or {}),
                    }
                    raise AssetError(
                        str(exc),
                        path=operation["crop"]["id"],
                        code=exc.detail["code"],
                        exit_code=exc.exit_code,
                        details=details,
                    ) from exc
            else:
                mode = "RGBA" if operation["crop"]["mode"] == "rgba" else "RGB"
                crop = crop.convert(mode)
                metrics = _opaque_metrics(crop)
            staged_path = stage_dir / operation["destination"].name
            if operation["type"] == "png":
                crop.save(staged_path, format="PNG", optimize=False, compress_level=9)
            else:
                crop.save(staged_path, format="JPEG", quality=95, subsampling=0, optimize=False, progressive=False)
            item = operation["manifest"]
            item.update({"type": operation["type"], "path": operation["relative"], "width_px": crop.width, "height_px": crop.height, "size_bytes": staged_path.stat().st_size, "sha256": sha256_file(staged_path), "security_status": "passed"})
            report_item = {
                "asset_id": item["id"],
                "boundary_policy": policy,
                "semantic_exclusion_boxes_px": operation["crop"].get("semantic_exclusion_boxes_px", []),
                **metrics,
                "output_sha256": item["sha256"],
                "status": "passed",
                "failure_codes": [],
            }
            staged.append((staged_path, operation, report_item))
        staged_manifest = stage_dir / "asset_manifest.json"
        atomic_write_json(staged_manifest, manifest)
        replaced_ids = {item["asset_id"] for _, _, item in staged}
        preserved_results = [
            item
            for item in (previous_report or {}).get("assets", [])
            if item["asset_id"] not in replaced_ids
        ]
        report = {
            "schema_version": "1.4",
            "source_sha256": sha256_file(args.input),
            "crops_sha256": sha256_file(contract_spec_path),
            "asset_manifest_sha256": sha256_file(staged_manifest),
            "algorithm": {
                "id": ALGORITHM_ID,
                "implementation_version": ALGORITHM_VERSION,
                "python_version": platform.python_version(),
                "pillow_version": PILLOW_VERSION,
            },
            "assets": sorted(
                preserved_results + [item for _, _, item in staged],
                key=lambda item: item["asset_id"],
            ),
            "status": "passed",
        }
        validate_schema("asset_processing_report", report, args.schema_dir)
        validate_semantics("asset_processing_report", report)
        try:
            cross_validate({"asset_manifest": manifest, "asset_processing_report": report})
        except ContractError as exc:
            raise AssetError(str(exc), code="contract_error") from exc
        staged_report = stage_dir / "asset_processing_report.json"
        atomic_write_json(staged_report, report)
        try:
            for staged_path, operation, _ in staged:
                operation["destination"].parent.mkdir(parents=True, exist_ok=True)
                os.replace(staged_path, operation["destination"])
                committed.append(operation["destination"])
            os.replace(staged_manifest, args.asset_manifest)
            os.replace(staged_report, report_path)
        except Exception:
            for path in committed:
                path.unlink(missing_ok=True)
            atomic_write_bytes(args.asset_manifest, original_manifest)
            if original_report is None:
                report_path.unlink(missing_ok=True)
            else:
                atomic_write_bytes(report_path, original_report)
            raise
    return {
        "asset_manifest": str(args.asset_manifest.resolve()),
        "asset_processing_report": str(report_path),
        "assets": [{"id": operation["crop"]["id"], "path": str(operation["destination"]), "sha256": operation["manifest"]["sha256"]} for operation in operations],
    }


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
