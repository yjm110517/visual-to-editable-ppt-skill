from __future__ import annotations

import copy
import hashlib
import os
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from asset_common import AssetError, atomic_write_json, contains_reparse_point, sha256_file


TERMINAL_STATES = {"delivered", "failed"}


def utc_now() -> str:
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    moment = datetime.fromtimestamp(int(epoch), timezone.utc) if epoch is not None else datetime.now(timezone.utc)
    return moment.isoformat().replace("+00:00", "Z")


def canonical_message_sha256(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise AssetError("response message must be readable UTF-8", path=str(path), code="message_input", exit_code=3) from exc
    normalized = unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def require_under(path: Path, root: Path, *, must_exist: bool = True) -> Path:
    root = root.resolve()
    candidate = path.resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise AssetError("path escapes work root", path=str(path), code="path_escape") from exc
    if contains_reparse_point(candidate, root):
        raise AssetError("symbolic links and reparse points are not allowed", path=str(path), code="reparse_point")
    if must_exist and not candidate.exists():
        raise AssetError("required path does not exist", path=str(path), code="missing_input", exit_code=3)
    return candidate


def relative_artifact(path: Path | None, work_root: Path) -> tuple[str | None, str | None]:
    if path is None:
        return None, None
    candidate = require_under(path, work_root)
    if not candidate.is_file():
        raise AssetError("state artifact must be a file", path=str(path), code="artifact_type")
    return candidate.relative_to(work_root.resolve()).as_posix(), sha256_file(candidate)


def append_transition(state: dict[str, Any], to_state: str, reason: str, *, artifact: Path | None, work_root: Path) -> dict[str, Any]:
    updated = copy.deepcopy(state)
    artifact_name, digest = relative_artifact(artifact, work_root)
    updated["history"].append({"from": state["state"], "to": to_state, "reason": reason, "artifact": artifact_name, "artifact_sha256": digest, "timestamp_utc": utc_now()})
    updated["state"] = to_state
    return updated


def commit_state(path: Path, state: dict[str, Any]) -> None:
    atomic_write_json(path, state)
