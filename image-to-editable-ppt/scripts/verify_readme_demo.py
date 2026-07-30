from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import tempfile
import unicodedata
from pathlib import Path
from typing import Any

from PIL import Image

from assert_review_gate import assert_gate
from asset_common import AssetError, atomic_write_json, failure, sha256_file, success


COMPONENT = "verify_readme_demo"
PUBLIC_NAMES = {
    "source": "ai-learning-loop-source.png",
    "render": "ai-learning-loop-rendered.png",
    "comparison": "ai-learning-loop-comparison.webp",
}


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Verify and atomically publish the accepted README demonstration.")
    result.add_argument("--work-root", type=Path, required=True)
    result.add_argument("--iteration-dir", type=Path, required=True)
    result.add_argument("--run-state", type=Path, required=True)
    result.add_argument("--planner-call-record", type=Path, required=True)
    result.add_argument("--reviewer-call-record", type=Path, required=True)
    result.add_argument("--ppt", type=Path, required=True)
    result.add_argument("--approval-decision", choices=("accept",), required=True)
    result.add_argument("--approval-message-file", type=Path, required=True)
    result.add_argument("--readme", type=Path, required=True)
    result.add_argument("--publish-dir", type=Path, required=True)
    result.add_argument("--output", type=Path, required=True)
    result.add_argument("--run-id", required=True)
    result.add_argument("--iteration", type=int, required=True)
    result.add_argument("--schema-dir", type=Path, default=Path(__file__).resolve().parents[1] / "schemas")
    return result


def _approval_hash(path: Path) -> str:
    if not path.is_file():
        raise AssetError("approval message file is missing", path=str(path), code="missing_input", exit_code=3)
    text = path.read_text(encoding="utf-8")
    if not text:
        raise AssetError("approval message cannot be empty", path=str(path), code="approval_required", exit_code=10)
    normalized = unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _write_comparison(source: Path, render: Path, output: Path) -> None:
    with Image.open(source) as left_image, Image.open(render) as right_image:
        left = left_image.convert("RGB")
        right = right_image.convert("RGB")
        if left.size != right.size:
            raise AssetError("source and render dimensions must match for README comparison", code="comparison_mismatch")
        gap = 8
        canvas = Image.new("RGB", (left.width * 2 + gap, left.height), "white")
        canvas.paste(left, (0, 0))
        canvas.paste(right, (left.width + gap, 0))
        canvas.save(output, format="WEBP", lossless=True, method=6)


def _publish_atomic(staged: dict[str, Path], target: Path, expected_hashes: dict[str, str]) -> None:
    target.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".readme-publish-", dir=target.parent) as temporary:
        root = Path(temporary)
        incoming = root / "incoming"
        backup = root / "backup"
        incoming.mkdir()
        backup.mkdir()
        for key, source in staged.items():
            shutil.copy2(source, incoming / PUBLIC_NAMES[key])
        installed: list[Path] = []
        replaced: list[tuple[Path, Path]] = []
        try:
            for name in PUBLIC_NAMES.values():
                destination = target / name
                if destination.exists():
                    saved = backup / name
                    os.replace(destination, saved)
                    replaced.append((saved, destination))
                os.replace(incoming / name, destination)
                installed.append(destination)
            for key, expected_hash in expected_hashes.items():
                destination = target / PUBLIC_NAMES[key]
                if sha256_file(destination) != expected_hash:
                    raise AssetError(
                        "published README asset hash changed during commit",
                        path=str(destination),
                        code="hash_conflict",
                        exit_code=9,
                    )
        except Exception:
            for destination in installed:
                destination.unlink(missing_ok=True)
            for saved, destination in reversed(replaced):
                os.replace(saved, destination)
            raise


def verify_and_publish(args: argparse.Namespace) -> dict[str, Any]:
    work_root = args.work_root.resolve()
    iteration = args.iteration_dir.resolve()
    output = args.output.resolve()
    try:
        output.relative_to(work_root)
    except ValueError as exc:
        raise AssetError("verification output must stay inside work-root", path=str(output), code="path_escape") from exc
    gate_args = argparse.Namespace(
        work_root=work_root,
        iteration_dir=iteration,
        run_state=args.run_state,
        planner_call_record=args.planner_call_record,
        reviewer_call_record=args.reviewer_call_record,
        ppt=args.ppt,
        run_id=args.run_id,
        iteration=args.iteration,
        log_file=None,
        schema_dir=args.schema_dir,
    )
    gate = assert_gate(gate_args)
    approval_sha256 = _approval_hash(args.approval_message_file)
    readme = args.readme.resolve()
    if not readme.is_file():
        raise AssetError("README is missing", path=str(readme), code="missing_input", exit_code=3)
    publish_dir = args.publish_dir.resolve()
    expected_publish_dir = (readme.parent / "docs" / "assets" / "readme" / "ai-learning-loop").resolve()
    if publish_dir != expected_publish_dir:
        raise AssetError(
            "publish-dir must be the README ai-learning-loop asset directory",
            path=str(publish_dir),
            code="path_escape",
        )
    readme_text = readme.read_text(encoding="utf-8")
    expected_links = [f"docs/assets/readme/ai-learning-loop/{name}" for name in PUBLIC_NAMES.values()]
    missing_links = [link for link in expected_links if link not in readme_text]
    if missing_links:
        raise AssetError("README does not reference the complete demonstration asset set", path=str(readme), code="readme_link_mismatch")

    request_source = work_root / "source.png"
    if not request_source.is_file():
        # The request contract may use another safe relative filename.
        import json

        request = json.loads((work_root / "request.json").read_text(encoding="utf-8"))
        request_source = work_root / request["source_image"]
    render = iteration / "rendered_slide.png"
    if not request_source.is_file() or not render.is_file():
        raise AssetError("accepted source or render is missing", code="missing_input", exit_code=3)

    with tempfile.TemporaryDirectory(prefix=".readme-demo-", dir=work_root) as temporary:
        stage = Path(temporary)
        staged_source = stage / PUBLIC_NAMES["source"]
        staged_render = stage / PUBLIC_NAMES["render"]
        staged_comparison = stage / PUBLIC_NAMES["comparison"]
        shutil.copy2(request_source, staged_source)
        shutil.copy2(render, staged_render)
        _write_comparison(staged_source, staged_render, staged_comparison)
        hashes = {
            "source_sha256": sha256_file(staged_source),
            "render_sha256": sha256_file(staged_render),
            "comparison_sha256": sha256_file(staged_comparison),
        }
        _publish_atomic(
            {"source": staged_source, "render": staged_render, "comparison": staged_comparison},
            publish_dir,
            {
                "source": hashes["source_sha256"],
                "render": hashes["render_sha256"],
                "comparison": hashes["comparison_sha256"],
            },
        )

    record = {
        "schema_version": "1.4",
        "status": "pass",
        "run_id": args.run_id,
        "iteration": args.iteration,
        "approval_decision": args.approval_decision,
        "approval_message_sha256": approval_sha256,
        "visual_review_gate": gate,
        "public_assets": {
            key: {
                "path": str((args.publish_dir.resolve() / PUBLIC_NAMES[key]).relative_to(readme.parent.resolve())).replace("\\", "/"),
                "sha256": hashes[f"{key}_sha256"],
            }
            for key in PUBLIC_NAMES
        },
    }
    atomic_write_json(output, record)
    return {"verification": str(output), **hashes, "published": True}


def main() -> int:
    args = parser().parse_args()
    try:
        return success(COMPONENT, verify_and_publish(args), run_id=args.run_id, iteration=args.iteration)
    except Exception as exc:
        return failure(COMPONENT, exc, run_id=args.run_id, iteration=args.iteration)


if __name__ == "__main__":
    raise SystemExit(main())
