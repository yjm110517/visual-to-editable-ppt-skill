from __future__ import annotations

import argparse
import csv
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from PIL import Image

from asset_common import AssetError, failure, load_contract, log_event, sha256_file, success
from schema_utils import validate_schema, validate_semantics


COMPONENT = "render_ppt"
POWERPOINT_NAME = "Microsoft PowerPoint"
LIBREOFFICE_NAME = "LibreOffice"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Render a single-slide PPTX to a deterministic PNG.")
    result.add_argument("--input", required=True, type=Path)
    result.add_argument("--layout", required=True, type=Path)
    result.add_argument("--output", required=True, type=Path)
    result.add_argument("--report", required=True, type=Path)
    result.add_argument("--renderer", choices=("auto", "powerpoint", "libreoffice"), default="auto")
    result.add_argument("--width-px", type=int)
    result.add_argument("--height-px", type=int)
    result.add_argument("--libreoffice-path", type=Path)
    result.add_argument("--timeout-seconds", type=int, default=120)
    result.add_argument("--schema-dir", type=Path, default=Path(__file__).resolve().parents[1] / "schemas")
    result.add_argument("--log-file", type=Path)
    result.add_argument("--run-id", default="local")
    result.add_argument("--iteration", type=int)
    return result


def _write_worker_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _powerpoint_pids() -> set[int]:
    if os.name != "nt":
        return set()
    completed = subprocess.run(["tasklist", "/FI", "IMAGENAME eq POWERPNT.EXE", "/FO", "CSV", "/NH"], capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    result: set[int] = set()
    for row in csv.reader(completed.stdout.splitlines()):
        if len(row) >= 2 and row[0].casefold() == "powerpnt.exe":
            try:
                result.add(int(row[1]))
            except ValueError:
                pass
    return result


def _powerpoint_worker(argv: list[str]) -> int:
    ppt, output, width, height, state = Path(argv[0]), Path(argv[1]), int(argv[2]), int(argv[3]), Path(argv[4])
    app = presentation = None
    try:
        import pythoncom
        import win32api
        import win32com.client
        pythoncom.CoInitialize()
        previous_pids = _powerpoint_pids()
        app = win32com.client.DispatchEx("PowerPoint.Application")
        new_pids = _powerpoint_pids() - previous_pids
        pid = next(iter(new_pids), None)
        _write_worker_json(state, {"pid": pid})
        presentation = app.Presentations.Open(str(ppt.resolve()), ReadOnly=True, Untitled=False, WithWindow=False)
        if presentation.Slides.Count != 1:
            raise RuntimeError(f"expected one slide, found {presentation.Slides.Count}")
        presentation.Slides(1).Export(str(output.resolve()), "PNG", width, height)
        executable = Path(app.Path) / "POWERPNT.EXE"
        file_version = None
        if executable.is_file():
            try:
                info = win32api.GetFileVersionInfo(str(executable), "\\")
                file_version = ".".join(str(value) for value in (info["FileVersionMS"] >> 16, info["FileVersionMS"] & 0xFFFF, info["FileVersionLS"] >> 16, info["FileVersionLS"] & 0xFFFF))
            except Exception:
                file_version = None
        version = f"COM {app.Version}" + (f"; file {file_version}" if file_version else "")
        _write_worker_json(state, {"pid": pid, "status": "passed", "version": version})
        return 0
    except Exception as exc:
        current: dict[str, Any] = {}
        try:
            current = json.loads(state.read_text(encoding="utf-8")) if state.exists() else {}
        except Exception:
            pass
        current.update({"status": "failed", "message": str(exc)})
        _write_worker_json(state, current)
        return 1
    finally:
        if presentation is not None:
            try:
                presentation.Close()
            except Exception:
                pass
        if app is not None:
            try:
                app.Quit()
            except Exception:
                pass
        try:
            import pythoncom

            pythoncom.CoUninitialize()
        except Exception:
            pass


def _validate_pptx(path: Path) -> None:
    if not path.is_file():
        raise AssetError("PPTX input does not exist", path=str(path), code="missing_input", exit_code=3)
    try:
        with zipfile.ZipFile(path) as archive:
            required = {"[Content_Types].xml", "ppt/presentation.xml", "ppt/slides/slide1.xml"}
            if not required.issubset(archive.namelist()):
                raise AssetError("PPTX package is missing required parts", path=str(path), code="invalid_pptx", exit_code=3)
            slides = [name for name in archive.namelist() if name.startswith("ppt/slides/slide") and name.endswith(".xml")]
            if len(slides) != 1:
                raise AssetError("renderer accepts exactly one slide", path=str(path), code="invalid_slide_count", exit_code=3)
    except zipfile.BadZipFile as exc:
        raise AssetError("PPTX package is unreadable", path=str(path), code="invalid_pptx", exit_code=3) from exc


def _render_dimensions(args: argparse.Namespace, layout: dict[str, Any]) -> tuple[int, int]:
    if (args.width_px is None) != (args.height_px is None):
        raise AssetError("width-px and height-px must be provided together", code="invalid_dimensions", exit_code=2)
    width = args.width_px or layout["source"]["width_px"]
    height = args.height_px or layout["source"]["height_px"]
    if width < 1 or height < 1:
        raise AssetError("render dimensions must be positive", code="invalid_dimensions", exit_code=2)
    slide_ratio = layout["slide"]["width_in"] / layout["slide"]["height_in"]
    if abs(width / height - slide_ratio) / slide_ratio > 0.005:
        raise AssetError("render dimensions must preserve the slide aspect ratio", code="aspect_ratio_mismatch", exit_code=4)
    return width, height


def _kill_owned_process(pid: int | None) -> None:
    if not pid or os.name != "nt":
        return
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _render_powerpoint(ppt: Path, output: Path, width: int, height: int, timeout: int, state: Path) -> str:
    if os.name != "nt":
        raise AssetError("PowerPoint COM is available only on Windows", code="renderer_unavailable", exit_code=5)
    command = [sys.executable, str(Path(__file__).resolve()), "--_powerpoint-worker", str(ppt), str(output), str(width), str(height), str(state)]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired as exc:
        details = json.loads(state.read_text(encoding="utf-8")) if state.exists() else {}
        _kill_owned_process(details.get("pid"))
        raise AssetError("PowerPoint rendering timed out", code="render_timeout", exit_code=7) from exc
    details = json.loads(state.read_text(encoding="utf-8")) if state.exists() else {}
    if completed.returncode != 0 or details.get("status") != "passed" or not output.is_file():
        message = details.get("message") or completed.stderr.strip() or "PowerPoint rendering failed"
        unavailable = "Invalid class string" in message or "Class not registered" in message
        raise AssetError(message, code="renderer_unavailable" if unavailable else "render_failed", exit_code=5 if unavailable else 7)
    return details["version"]


def find_libreoffice(explicit: Path | None = None) -> Path | None:
    candidates: list[Path] = []
    if explicit:
        candidates.append(explicit)
    if os.environ.get("IVT_LIBREOFFICE"):
        candidates.append(Path(os.environ["IVT_LIBREOFFICE"]))
    located = shutil.which("soffice") or shutil.which("libreoffice")
    if located:
        candidates.append(Path(located))
    if os.name == "nt":
        candidates.extend((Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "LibreOffice/program/soffice.exe", Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "LibreOffice/program/soffice.exe"))
    return next((candidate.resolve() for candidate in candidates if candidate.is_file()), None)


def libreoffice_command(executable: Path, ppt: Path, output_dir: Path, profile_dir: Path, width: int, height: int) -> list[str]:
    options = json.dumps({"PixelWidth": {"type": "long", "value": width}, "PixelHeight": {"type": "long", "value": height}}, separators=(",", ":"))
    return [str(executable), "--headless", "--nologo", "--nodefault", "--nolockcheck", "--nofirststartwizard", f"-env:UserInstallation={profile_dir.resolve().as_uri()}", "--convert-to", f"png:impress_png_Export:{options}", "--outdir", str(output_dir), str(ppt)]


def _render_libreoffice(ppt: Path, output: Path, width: int, height: int, timeout: int, executable: Path | None, work_dir: Path) -> str:
    found = find_libreoffice(executable)
    if found is None:
        raise AssetError("LibreOffice executable was not found", code="renderer_unavailable", exit_code=5)
    version_result = subprocess.run([str(found), "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, check=False)
    version = (version_result.stdout or version_result.stderr).strip()
    if version_result.returncode != 0 or not version:
        raise AssetError("LibreOffice version could not be determined", code="renderer_unavailable", exit_code=5)
    export_dir = work_dir / "libreoffice-output"
    profile_dir = work_dir / "libreoffice-profile"
    export_dir.mkdir()
    profile_dir.mkdir()
    try:
        completed = subprocess.run(libreoffice_command(found, ppt, export_dir, profile_dir, width, height), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise AssetError("LibreOffice rendering timed out", code="render_timeout", exit_code=7) from exc
    candidates = list(export_dir.glob("*.png"))
    if completed.returncode != 0 or len(candidates) != 1:
        message = completed.stderr.strip() or completed.stdout.strip() or "LibreOffice rendering failed"
        raise AssetError(message, code="render_failed", exit_code=7)
    shutil.copyfile(candidates[0], output)
    return version


def _canonical_png(path: Path, width: int, height: int) -> bytes:
    try:
        with Image.open(path) as image:
            image.load()
            if image.format != "PNG" or image.size != (width, height):
                raise AssetError("renderer output has an unexpected format or size", path=str(path), code="render_validation", exit_code=7)
            normalized = image.convert("RGB")
            stream = io.BytesIO()
            normalized.save(stream, format="PNG", optimize=False, compress_level=9)
            return stream.getvalue()
    except OSError as exc:
        raise AssetError("renderer output is unreadable", path=str(path), code="render_validation", exit_code=7) from exc


def render_ppt(args: argparse.Namespace) -> dict[str, Any]:
    _validate_pptx(args.input)
    layout = load_contract("layout", args.layout, args.schema_dir)
    if args.iteration is not None and layout["metadata"]["iteration"] != args.iteration:
        raise AssetError("iteration does not match layout", code="iteration_mismatch")
    if args.output.exists() or args.report.exists():
        raise AssetError("render output already exists", path=str(args.output if args.output.exists() else args.report), code="output_collision")
    if not 1 <= args.timeout_seconds <= 3600:
        raise AssetError("timeout-seconds must be between 1 and 3600", code="invalid_timeout", exit_code=2)
    iteration_dir = args.layout.parent.resolve()
    for candidate in (args.input, args.layout, args.output, args.report):
        try:
            candidate.resolve().relative_to(iteration_dir)
        except ValueError as exc:
            raise AssetError("render path escapes the iteration directory", path=str(candidate), code="path_escape") from exc
    width, height = _render_dimensions(args, layout)
    selections = [args.renderer] if args.renderer != "auto" else ["powerpoint", "libreoffice"]
    attempts: list[dict[str, str]] = []
    warnings: list[str] = []
    rendered_bytes: bytes | None = None
    chosen_name = chosen_version = None
    failure_codes: list[int] = []
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".render-stage-", dir=args.output.parent) as temporary:
        stage = Path(temporary)
        for renderer in selections:
            raw_output = stage / f"{renderer}.png"
            try:
                if renderer == "powerpoint":
                    version = _render_powerpoint(args.input.resolve(), raw_output, width, height, args.timeout_seconds, stage / "powerpoint-state.json")
                    name = POWERPOINT_NAME
                else:
                    version = _render_libreoffice(args.input.resolve(), raw_output, width, height, args.timeout_seconds, args.libreoffice_path, stage)
                    name = LIBREOFFICE_NAME
                rendered_bytes = _canonical_png(raw_output, width, height)
                chosen_name, chosen_version = name, version
                attempts.append({"renderer": name, "status": "passed", "message": "render completed"})
                break
            except AssetError as exc:
                name = POWERPOINT_NAME if renderer == "powerpoint" else LIBREOFFICE_NAME
                attempts.append({"renderer": name, "status": "unavailable" if exc.exit_code == 5 else "failed", "message": str(exc)})
                failure_codes.append(exc.exit_code)
                if args.renderer != "auto":
                    raise
                warnings.append(f"{name} attempt failed: {exc}")
        if rendered_bytes is None or chosen_name is None or chosen_version is None:
            exit_code = 5 if failure_codes and all(code == 5 for code in failure_codes) else 7
            raise AssetError("no renderer produced a valid PNG", code="renderer_unavailable" if exit_code == 5 else "render_failed", exit_code=exit_code)
        staged_png = stage / "rendered_slide.png"
        staged_png.write_bytes(rendered_bytes)
        render_hash = sha256_file(staged_png)
        report = {
            "schema_version": "1.3",
            "renderer": chosen_name,
            "renderer_version": chosen_version,
            "fallback_used": len(attempts) > 1,
            "width_px": width,
            "height_px": height,
            "rendered_page_count": 1,
            "ppt_sha256": sha256_file(args.input),
            "render_sha256": render_hash,
            "attempts": attempts,
            "warnings": sorted(set(warnings)),
        }
        validate_schema("render_report", report, args.schema_dir)
        validate_semantics("render_report", report)
        staged_report = stage / "render_report.json"
        staged_report.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8", newline="\n")
        committed = False
        try:
            os.replace(staged_png, args.output)
            committed = True
            os.replace(staged_report, args.report)
        except Exception:
            if committed:
                args.output.unlink(missing_ok=True)
            args.report.unlink(missing_ok=True)
            raise
    return report


def main() -> int:
    args = parser().parse_args()
    try:
        log_event(args.log_file, level="info", component=COMPONENT, event="started", message="PPT rendering started", run_id=args.run_id, iteration=args.iteration)
        report = render_ppt(args)
        log_event(args.log_file, level="info", component=COMPONENT, event="completed", message="PPT rendering completed", run_id=args.run_id, iteration=args.iteration, data={"renderer": report["renderer"], "fallback_used": report["fallback_used"], "exit_code": 0})
        return success(COMPONENT, {"render": str(args.output.resolve()), "render_report": str(args.report.resolve()), "render_sha256": report["render_sha256"], "renderer": report["renderer"]}, run_id=args.run_id, iteration=args.iteration)
    except Exception as exc:
        log_event(args.log_file, level="error", component=COMPONENT, event="failed", message=str(exc), run_id=args.run_id, iteration=args.iteration, data={"exit_code": getattr(exc, "exit_code", 70)})
        return failure(COMPONENT, exc, run_id=args.run_id, iteration=args.iteration)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--_powerpoint-worker":
        raise SystemExit(_powerpoint_worker(sys.argv[2:]))
    raise SystemExit(main())
