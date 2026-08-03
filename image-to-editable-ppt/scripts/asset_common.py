from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

from schema_utils import ContractError, is_safe_relative_path, load_json, validate_schema, validate_semantics


class AssetError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        path: str = "$",
        code: str = "asset_error",
        exit_code: int = 4,
        details: Any | None = None,
    ):
        self.detail = {"path": path, "code": code, "message": message}
        if details is not None:
            self.detail["details"] = details
        self.exit_code = exit_code
        super().__init__(message)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_bytes(path, canonical_json_bytes(value))


def contains_reparse_point(path: Path, stop: Path) -> bool:
    current = path
    while True:
        if current.exists():
            if current.is_symlink():
                return True
            attributes = getattr(current.stat(), "st_file_attributes", 0)
            if attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
                return True
        if current == stop or current.parent == current:
            return False
        current = current.parent


def resolve_under(root: Path, relative: str, *, filename_only: bool = False, reject_reparse: bool = True) -> Path:
    if not is_safe_relative_path(relative, filename_only=filename_only):
        raise AssetError("unsafe relative path", path=relative, code="unsafe_path")
    root = root.resolve()
    lexical_candidate = root / PurePosixPath(relative)
    if reject_reparse and contains_reparse_point(lexical_candidate, root):
        raise AssetError("symbolic links and reparse points are not allowed", path=relative, code="reparse_point")
    candidate = lexical_candidate.resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise AssetError("path escapes authorized root", path=relative, code="path_escape") from exc
    return candidate


def manifest_relative_path(manifest_path: Path, asset_path: Path) -> str:
    base = manifest_path.parent.resolve()
    try:
        relative = asset_path.resolve().relative_to(base)
    except ValueError as exc:
        raise AssetError("asset output must remain under the manifest directory", path=str(asset_path), code="path_escape") from exc
    return relative.as_posix()


def load_contract(kind: str, path: Path, schema_dir: Path) -> dict[str, Any]:
    if not path.is_file():
        raise AssetError("input file does not exist", path=str(path), code="missing_input", exit_code=3)
    try:
        document = load_json(path)
        validate_schema(kind, document, schema_dir)
        validate_semantics(kind, document)
        return document
    except (json.JSONDecodeError, ContractError) as exc:
        message = str(exc)
        raise AssetError(message, path=str(path), code="contract_error") from exc


def index_assets(manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {asset["id"]: asset for asset in manifest["assets"]}


def log_event(log_path: Path | None, *, level: str, component: str, event: str, message: str, run_id: str, iteration: int | None, data: dict[str, Any] | None = None) -> None:
    if log_path is None:
        return
    record = {"timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "level": level, "component": component, "event": event, "message": message, "run_id": run_id, "iteration": iteration, "data": data or {}}
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def success(component: str, outputs: dict[str, Any], *, run_id: str, iteration: int | None) -> int:
    print(json.dumps({"status": "ok", "component": component, "run_id": run_id, "iteration": iteration, "outputs": outputs, "error": None}, ensure_ascii=False, sort_keys=True))
    return 0


def failure(component: str, exc: Exception, *, run_id: str, iteration: int | None) -> int:
    if isinstance(exc, AssetError):
        code, detail = exc.exit_code, exc.detail
    else:
        code, detail = 70, {"path": "$", "code": "internal_error", "message": str(exc)}
    error_payload = {"exit_code": code, "category": detail["code"], "message": detail["message"], "path": detail["path"]}
    if "details" in detail:
        error_payload["details"] = detail["details"]
    print(json.dumps({"status": "error", "component": component, "run_id": run_id, "iteration": iteration, "outputs": {}, "error": error_payload}, ensure_ascii=False, sort_keys=True))
    return code
