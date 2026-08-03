from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import PIL

from asset_common import AssetError, atomic_write_json, failure, load_contract, log_event, sha256_file, success
from manage_run_state import advance as advance_run_state


COMPONENT = "run_pipeline"
SCRIPT_DIR = Path(__file__).resolve().parent


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Run one deterministic image-to-editable-PPT build iteration.")
    result.add_argument("--request", required=True, type=Path)
    result.add_argument("--iteration-dir", required=True, type=Path)
    result.add_argument("--output-ppt", required=True, type=Path)
    result.add_argument("--execution-mode", choices=("production", "diagnostic"), default="diagnostic")
    result.add_argument("--run-state", type=Path)
    result.add_argument("--renderer", choices=("auto", "powerpoint", "libreoffice"), default="auto")
    result.add_argument("--node", type=Path)
    result.add_argument("--libreoffice-path", type=Path)
    result.add_argument("--width-px", type=int)
    result.add_argument("--height-px", type=int)
    result.add_argument("--timeout-seconds", type=int, default=120)
    result.add_argument("--schema-dir", type=Path, default=Path(__file__).resolve().parents[1] / "schemas")
    result.add_argument("--log-file", type=Path)
    result.add_argument("--run-id", required=True)
    result.add_argument("--iteration", required=True, type=int)
    return result


def _validate_execution_state(args: argparse.Namespace) -> dict[str, Any] | None:
    if args.execution_mode == "diagnostic":
        if args.run_state is not None:
            raise AssetError(
                "diagnostic mode must not mutate run state",
                path="--run-state",
                code="cli_error",
                exit_code=2,
            )
        return None
    if args.run_state is None:
        raise AssetError(
            "production mode requires --run-state",
            path="--run-state",
            code="cli_error",
            exit_code=2,
        )
    work_root = args.request.resolve().parent
    expected = work_root / "run_state.json"
    if args.run_state.resolve() != expected:
        raise AssetError("run-state must be work-root/run_state.json", path=str(args.run_state), code="path_escape")
    state = load_contract("run_state", expected, args.schema_dir)
    request = load_contract("request", args.request, args.schema_dir)
    if state["task_id"] != request["task_id"] or state["current_iteration"] != args.iteration:
        raise AssetError("run state does not match the production iteration", path=str(expected), code="state_conflict", exit_code=9)
    if state["state"] != "building":
        raise AssetError("production pipeline requires run state building", path=str(expected), code="state_conflict", exit_code=9)
    return state


def _record_structural_result(args: argparse.Namespace, qa_report: Path) -> dict[str, Any] | None:
    if args.execution_mode != "production":
        return None
    state_args = argparse.Namespace(
        work_root=args.request.resolve().parent,
        state=args.run_state.resolve(),
        event="structural_result",
        artifact=qa_report,
        reason=None,
        schema_dir=args.schema_dir,
        run_id=args.run_id,
        log_file=args.log_file,
    )
    return advance_run_state(state_args)


def _parse_result(completed: subprocess.CompletedProcess[str], component: str) -> dict[str, Any]:
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        diagnostic = (completed.stderr or completed.stdout or "").strip().replace("\r", " ").replace("\n", " ")
        if len(diagnostic) > 500:
            diagnostic = diagnostic[:497] + "..."
        message = f"{component} returned invalid result JSON"
        if diagnostic:
            message += f": {diagnostic}"
        raise AssetError(message, code="invalid_subprocess_result", exit_code=70) from exc
    if not isinstance(payload, dict) or payload.get("component") != component or payload.get("status") not in {"ok", "error"}:
        raise AssetError(f"{component} returned a result that violates the CLI contract", code="invalid_subprocess_result", exit_code=70)
    return payload


def _run(command: list[str], component: str, *, allow_codes: set[int] | None = None) -> tuple[int, dict[str, Any]]:
    completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    payload = _parse_result(completed, component)
    allowed = allow_codes or {0}
    if completed.returncode not in allowed:
        detail = payload.get("error") or {}
        raise AssetError(detail.get("message") or f"{component} failed", path=detail.get("path", "$"), code=detail.get("category", component), exit_code=detail.get("exit_code", completed.returncode or 70))
    return completed.returncode, payload


def _python_command(script: str, *arguments: str) -> list[str]:
    return [sys.executable, str(SCRIPT_DIR / script), *arguments]


def _common(args: argparse.Namespace, log: Path) -> list[str]:
    return ["--schema-dir", str(args.schema_dir), "--run-id", args.run_id, "--iteration", str(args.iteration), "--log-file", str(log)]


def _node_executable(args: argparse.Namespace) -> Path:
    candidate = args.node or (Path(os.environ["IVT_NODE"]) if os.environ.get("IVT_NODE") else None) or (Path(shutil.which("node")) if shutil.which("node") else None)
    if candidate is None or not candidate.is_file():
        raise AssetError("Node.js executable was not found", path="--node", code="node_unavailable", exit_code=5)
    completed = subprocess.run([str(candidate), "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    try:
        major = int(completed.stdout.strip().lstrip("v").split(".", 1)[0])
    except (ValueError, IndexError):
        major = 0
    if completed.returncode != 0 or major < 20:
        raise AssetError("Node.js 20 or newer is required", path=str(candidate), code="node_version", exit_code=5)
    return candidate.resolve()


def _copy_to_stage(args: argparse.Namespace, temporary_root: Path) -> tuple[Path, Path, Path]:
    request = args.request.resolve()
    work_root = request.parent
    iteration = args.iteration_dir.resolve()
    if not request.is_file() or not iteration.is_dir():
        raise AssetError("request or iteration directory does not exist", code="missing_input", exit_code=3)
    if request.name != "request.json":
        raise AssetError("request file must be named request.json", path=str(request), code="request_path")
    if iteration.parent.name != "iterations" or iteration.parent.parent != work_root:
        raise AssetError("iteration directory must be work/<topic>/iterations/<NN>", path=str(iteration), code="iteration_boundary")
    if iteration.name != f"{args.iteration:02d}":
        raise AssetError("iteration number does not match iteration directory", path=str(iteration), code="iteration_mismatch")
    if args.output_ppt.resolve().parent != iteration:
        raise AssetError("output PPT must be directly inside iteration-dir", path=str(args.output_ppt), code="path_escape")
    layout = load_contract("layout", iteration / "layout.json", args.schema_dir)
    if layout["metadata"]["iteration"] != args.iteration:
        raise AssetError("iteration does not match layout", code="iteration_mismatch")
    request_doc = load_contract("request", request, args.schema_dir)
    source = (work_root / request_doc["source_image"]).resolve()
    try:
        source.relative_to(work_root)
    except ValueError as exc:
        raise AssetError("source image escapes work root", path=str(source), code="path_escape") from exc
    if not source.is_file():
        raise AssetError("source image does not exist", path=str(source), code="missing_input", exit_code=3)
    source_relative = source.relative_to(work_root)
    generated = [args.output_ppt.name, "build_summary.json", "font_audit.json", "rendered_slide.png", "render_report.json", "qa_report.json"]
    collision = next((iteration / name for name in generated if (iteration / name).exists()), None)
    if collision:
        raise AssetError("iteration already contains generated output", path=str(collision), code="output_collision", exit_code=9)
    staged_work = temporary_root / work_root.name
    staged_iteration = staged_work / "iterations" / iteration.name
    staged_work.mkdir(parents=True)
    shutil.copy2(request, staged_work / request.name)
    staged_source = staged_work / source_relative
    staged_source.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, staged_source)
    shutil.copytree(iteration, staged_iteration)
    return staged_work, staged_iteration, staged_source


def _commit_iteration(stage: Path, target: Path, output_name: str) -> None:
    names = ["asset_manifest.json", "asset_processing_report.json", "assets", "svg_security_report.json", output_name, "build_summary.json", "font_audit.json", "rendered_slide.png", "render_report.json", "qa_report.json", "pipeline.log"]
    names = [name for name in names if (stage / name).exists()]
    parent = target.parent.resolve()
    with tempfile.TemporaryDirectory(prefix=f".{target.name}-commit-", dir=parent) as temporary:
        root = Path(temporary)
        incoming, backup = root / "incoming", root / "backup"
        incoming.mkdir()
        backup.mkdir()
        for name in names:
            source = stage / name
            destination = incoming / name
            if source.is_dir():
                shutil.copytree(source, destination)
            else:
                shutil.copy2(source, destination)
        replaced: list[str] = []
        installed: list[str] = []
        try:
            for name in names:
                destination = target / name
                if destination.exists():
                    os.replace(destination, backup / name)
                    replaced.append(name)
                os.replace(incoming / name, destination)
                installed.append(name)
        except Exception:
            for name in reversed(installed):
                destination = target / name
                if destination.is_dir():
                    shutil.rmtree(destination, ignore_errors=True)
                else:
                    destination.unlink(missing_ok=True)
            for name in reversed(replaced):
                os.replace(backup / name, target / name)
            raise


def _preserve_failure_log(stage: Path | None, target: Path, args: argparse.Namespace, exc: Exception) -> None:
    target_log = args.log_file.resolve() if args.log_file else target / "pipeline.log"
    if stage and (stage / "pipeline.log").is_file():
        target_log.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(stage / "pipeline.log", target_log)
    log_event(target_log, level="error", component=COMPONENT, event="failed", message=str(exc), run_id=args.run_id, iteration=args.iteration, data={"exit_code": getattr(exc, "exit_code", 70)})


def run_pipeline(args: argparse.Namespace) -> dict[str, Any]:
    if args.iteration < 1:
        raise AssetError("iteration must be positive", path="--iteration", code="cli_error", exit_code=2)
    _validate_execution_state(args)
    node = _node_executable(args)
    actual_iteration = args.iteration_dir.resolve()
    if args.log_file and args.log_file.resolve() != actual_iteration / "pipeline.log":
        raise AssetError("log-file must be iteration-dir/pipeline.log", path=str(args.log_file), code="path_escape")
    stage_iteration: Path | None = None
    with tempfile.TemporaryDirectory(prefix=f".{args.request.parent.name}-pipeline-", dir=args.request.parent.parent) as temporary:
        staged_work, stage_iteration, staged_source = _copy_to_stage(args, Path(temporary))
        log = stage_iteration / "pipeline.log"
        try:
            log_event(log, level="info", component=COMPONENT, event="started", message="Single-iteration pipeline started", run_id=args.run_id, iteration=args.iteration)
            layout = stage_iteration / "layout.json"
            crops = stage_iteration / "crops.json"
            manifest = stage_iteration / "asset_manifest.json"
            processing_report = stage_iteration / "asset_processing_report.json"
            assets = stage_iteration / "assets"
            assets.mkdir(parents=True, exist_ok=True)
            request = staged_work / args.request.name
            _run(_python_command("validate_spec.py", "--phase", "preflight", "--request", str(request), "--layout", str(layout), "--crops", str(crops), "--asset-manifest", str(manifest), "--schema-dir", str(args.schema_dir)), "validate_spec")

            crop_doc = load_contract("crops", crops, args.schema_dir)
            manifest_doc = load_contract("asset_manifest", manifest, args.schema_dir)
            manifest_by_id = {item["id"]: item for item in manifest_doc["assets"]}
            pending_crops = [item for item in crop_doc["assets"] if manifest_by_id[item["id"]].get("security_status") != "passed"]
            if pending_crops:
                pending_path = stage_iteration / ".pending-crops.json"
                pending_path.write_text(json.dumps({"schema_version": "1.4", "source": crop_doc["source"], "assets": pending_crops}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
                try:
                    _run(_python_command("crop_assets.py", "--input", str(staged_source), "--spec", str(pending_path), "--contract-spec", str(crops), "--output-dir", str(assets), "--asset-manifest", str(manifest), "--processing-report", str(processing_report), *_common(args, log)), "crop_assets")
                finally:
                    pending_path.unlink(missing_ok=True)
            else:
                log_event(log, level="info", component="crop_assets", event="skipped", message="No pending raster crops", run_id=args.run_id, iteration=args.iteration)
                if not crop_doc["assets"] and not processing_report.is_file():
                    atomic_write_json(processing_report, {
                        "schema_version": "1.4",
                        "source_sha256": sha256_file(staged_source),
                        "crops_sha256": sha256_file(crops),
                        "asset_manifest_sha256": sha256_file(manifest),
                        "algorithm": {
                            "id": "edge-connected-background-v1",
                            "implementation_version": "1.0.0",
                            "python_version": ".".join(map(str, sys.version_info[:3])),
                            "pillow_version": PIL.__version__,
                        },
                        "assets": [],
                        "status": "passed",
                    })

            manifest_doc = load_contract("asset_manifest", manifest, args.schema_dir)
            svg_items = [item for item in manifest_doc["assets"] if item["type"] == "svg"]
            pending_svg = [item for item in svg_items if item["security_status"] != "passed"]
            passed_svg = [item for item in svg_items if item["security_status"] == "passed"]
            svg_report = stage_iteration / "svg_security_report.json"
            if pending_svg:
                if passed_svg:
                    if not svg_report.is_file():
                        raise AssetError("passed SVG assets require an existing security report before mixed-state sanitization", path=str(svg_report), code="missing_svg_report")
                    existing_report = json.loads(svg_report.read_text(encoding="utf-8"))
                    existing_by_id = {item.get("asset_id"): item for item in existing_report.get("results", []) if isinstance(item, dict)}
                    if any(item["id"] not in existing_by_id for item in passed_svg):
                        raise AssetError("existing SVG security report does not cover passed assets", path=str(svg_report), code="svg_report_mismatch")
                    pending_manifest = stage_iteration / ".pending-svg-manifest.json"
                    pending_report = stage_iteration / ".pending-svg-report.json"
                    atomic_write_json(pending_manifest, {"schema_version": "1.4", "assets": pending_svg})
                    try:
                        _run(_python_command("sanitize_svg.py", "--asset-dir", str(assets), "--asset-manifest", str(pending_manifest), "--report", str(pending_report), *_common(args, log)), "sanitize_svg")
                        updated_pending = {item["id"]: item for item in json.loads(pending_manifest.read_text(encoding="utf-8"))["assets"]}
                        full_manifest = json.loads(manifest.read_text(encoding="utf-8"))
                        full_manifest["assets"] = [updated_pending.get(item["id"], item) for item in full_manifest["assets"]]
                        atomic_write_json(manifest, full_manifest)
                        pending_results = json.loads(pending_report.read_text(encoding="utf-8"))["results"]
                        combined = [existing_by_id[item["id"]] for item in passed_svg] + pending_results
                        atomic_write_json(svg_report, {"schema_version": "1.3", "results": sorted(combined, key=lambda item: item["asset_id"])})
                    finally:
                        pending_manifest.unlink(missing_ok=True)
                        pending_report.unlink(missing_ok=True)
                else:
                    if svg_report.exists():
                        svg_report.unlink()
                    _run(_python_command("sanitize_svg.py", "--asset-dir", str(assets), "--asset-manifest", str(manifest), "--report", str(svg_report), *_common(args, log)), "sanitize_svg")
            elif svg_items and not svg_report.is_file():
                raise AssetError("build-ready SVG assets require svg_security_report.json", path=str(svg_report), code="missing_svg_report")
            else:
                log_event(log, level="info", component="sanitize_svg", event="skipped", message="No pending SVG assets", run_id=args.run_id, iteration=args.iteration)

            if processing_report.is_file():
                processing_document = load_contract("asset_processing_report", processing_report, args.schema_dir)
                processing_document["asset_manifest_sha256"] = sha256_file(manifest)
                atomic_write_json(processing_report, processing_document)

            _run(_python_command("validate_spec.py", "--phase", "build-ready", "--request", str(request), "--layout", str(layout), "--crops", str(crops), "--asset-manifest", str(manifest), "--asset-processing-report", str(processing_report), "--schema-dir", str(args.schema_dir)), "validate_spec")
            output = stage_iteration / args.output_ppt.name
            summary = stage_iteration / "build_summary.json"
            build_command = [str(node), str(SCRIPT_DIR / "build_slide.mjs"), "--iteration-dir", str(stage_iteration), "--layout", str(layout), "--asset-manifest", str(manifest), "--asset-processing-report", str(processing_report), "--asset-dir", str(assets), "--output", str(output), "--build-summary", str(summary), "--python", sys.executable, "--run-id", args.run_id, "--iteration", str(args.iteration), "--log-file", str(log), "--schema-dir", str(args.schema_dir)]
            if svg_items:
                build_command.extend(["--svg-report", str(svg_report)])
            _run(build_command, "build_slide")
            font_audit = stage_iteration / "font_audit.json"
            _run(_python_command("audit_fonts.py", "--ppt", str(output), "--layout", str(layout), "--build-summary", str(summary), "--output", str(font_audit), *_common(args, log)), "audit_fonts")
            render = stage_iteration / "rendered_slide.png"
            render_report = stage_iteration / "render_report.json"
            render_command = _python_command("render_ppt.py", "--input", str(output), "--layout", str(layout), "--output", str(render), "--report", str(render_report), "--renderer", args.renderer, "--timeout-seconds", str(args.timeout_seconds), *_common(args, log))
            if args.libreoffice_path:
                render_command.extend(["--libreoffice-path", str(args.libreoffice_path)])
            if args.width_px is not None:
                render_command.extend(["--width-px", str(args.width_px), "--height-px", str(args.height_px)])
            _run(render_command, "render_ppt")
            qa = stage_iteration / "qa_report.json"
            verify_command = _python_command("verify_ppt.py", "--request", str(request), "--source", str(staged_source), "--iteration-dir", str(stage_iteration), "--ppt", str(output), "--layout", str(layout), "--crops", str(crops), "--asset-manifest", str(manifest), "--asset-processing-report", str(processing_report), "--build-summary", str(summary), "--font-audit", str(font_audit), "--render", str(render), "--render-report", str(render_report), "--output", str(qa), *_common(args, log))
            verify_code, _ = _run(verify_command, "verify_ppt", allow_codes={0, 8})
            log_event(log, level="info" if verify_code == 0 else "error", component=COMPONENT, event="completed" if verify_code == 0 else "structural_failed", message="Single-iteration pipeline completed", run_id=args.run_id, iteration=args.iteration, data={"exit_code": verify_code})
            _commit_iteration(stage_iteration, actual_iteration, args.output_ppt.name)
            actual_qa = actual_iteration / "qa_report.json"
            qa_doc = json.loads(actual_qa.read_text(encoding="utf-8"))
            state = _record_structural_result(args, actual_qa)
            structural_pass = verify_code == 0
            return {
                "exit_code": verify_code,
                "outputs": {
                    "pptx": str((actual_iteration / args.output_ppt.name).resolve()),
                    "build_summary": str((actual_iteration / "build_summary.json").resolve()),
                    "font_audit": str((actual_iteration / "font_audit.json").resolve()),
                    "render": str((actual_iteration / "rendered_slide.png").resolve()),
                    "render_report": str((actual_iteration / "render_report.json").resolve()),
                    "qa_report": str(actual_qa.resolve()),
                    "pipeline_log": str((actual_iteration / "pipeline.log").resolve()),
                    "ppt_sha256": qa_doc["provenance"]["ppt_sha256"],
                    "render_sha256": qa_doc["provenance"]["render_sha256"],
                    "execution_mode": args.execution_mode,
                    "structural_status": qa_doc["status"],
                    "visual_review_status": "pending" if structural_pass else "blocked",
                    "deliverable": False,
                    "required_next_action": (
                        "run_review_checkpoint"
                        if structural_pass and args.execution_mode == "production"
                        else "diagnostic_only_not_deliverable"
                        if structural_pass
                        else "repair_structural_failure"
                    ),
                    "run_state": state["state"] if state is not None else "not_managed",
                },
            }
        except Exception as exc:
            _preserve_failure_log(stage_iteration, actual_iteration, args, exc)
            raise


def main() -> int:
    args = parser().parse_args()
    try:
        result = run_pipeline(args)
        if result["exit_code"] == 0:
            return success(COMPONENT, result["outputs"], run_id=args.run_id, iteration=args.iteration)
        print(json.dumps({"status": "error", "component": COMPONENT, "run_id": args.run_id, "iteration": args.iteration, "outputs": result["outputs"], "error": {"exit_code": 8, "category": "structural_qa_failed", "message": "structural QA hard gate failed", "path": result["outputs"]["qa_report"]}}, ensure_ascii=False, sort_keys=True))
        return 8
    except Exception as exc:
        try:
            target_log = args.log_file.resolve() if args.log_file else args.iteration_dir.resolve() / "pipeline.log"
            log_event(target_log, level="error", component=COMPONENT, event="failed", message=str(exc), run_id=args.run_id, iteration=args.iteration, data={"exit_code": getattr(exc, "exit_code", 70)})
        except Exception:
            pass
        return failure(COMPONENT, exc, run_id=args.run_id, iteration=args.iteration)


if __name__ == "__main__":
    raise SystemExit(main())
