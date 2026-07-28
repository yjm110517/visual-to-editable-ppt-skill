from __future__ import annotations

import hashlib
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

import yaml

from asset_common import AssetError, canonical_json_bytes, contains_reparse_point, sha256_file
from schema_utils import ContractError, load_json, validate_schema, validate_semantics


SKILL_DIR = Path(__file__).resolve().parents[1]
AGENT_DIR = SKILL_DIR / "agents"
SCHEMA_DIR = SKILL_DIR / "schemas"
REFERENCE_DIR = SKILL_DIR / "references"
CALL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def normalized_text_bytes(path: Path) -> bytes:
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    return text.encode("utf-8")


def sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def canonical_yaml_hash(path: Path) -> str:
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssetError("agent role configuration must be an object", path=str(path), code="agent_config")
    return sha256_bytes(canonical_json_bytes(value))


def _within(path: Path, root: Path) -> Path:
    root = root.resolve()
    lexical = path if path.is_absolute() else root / path
    if contains_reparse_point(lexical, root):
        raise AssetError("symbolic links and reparse points are not allowed", path=str(path), code="reparse_point")
    resolved = lexical.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise AssetError("path escapes authorized work root", path=str(path), code="path_escape") from exc
    return resolved


def ensure_under(path: Path, root: Path) -> Path:
    return _within(path, root)


def load_role(role: str, schema_dir: Path = SCHEMA_DIR) -> tuple[dict[str, Any], Path, Path, Path]:
    filename = "planner.yaml" if role == "planner" else "visual_reviewer.yaml"
    config_path = AGENT_DIR / filename
    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise ValueError("configuration must be an object")
        validate_schema("agent_role", config, schema_dir)
        validate_semantics("agent_role", config)
    except (OSError, ValueError, yaml.YAMLError, ContractError) as exc:
        raise AssetError(str(exc), path=str(config_path), code="agent_config", exit_code=2) from exc
    expected = "layout-planner" if role == "planner" else "visual-reviewer"
    if config["role_id"] != expected:
        raise AssetError("role_id does not match selected role", path=str(config_path), code="agent_config", exit_code=2)
    prompt_path = (AGENT_DIR / config["prompt_file"]).resolve()
    output_schema = (AGENT_DIR / config["output_schema"]).resolve()
    for candidate, label in ((prompt_path, "prompt"), (output_schema, "output schema")):
        try:
            candidate.relative_to(SKILL_DIR.resolve())
        except ValueError as exc:
            raise AssetError(f"{label} escapes the Skill directory", path=str(candidate), code="agent_config", exit_code=2) from exc
        if not candidate.is_file():
            raise AssetError(f"{label} does not exist", path=str(candidate), code="missing_input", exit_code=3)
    return config, config_path, prompt_path, output_schema


def expected_call_dir(work_root: Path, iteration: int, role: str, call_id: str) -> Path:
    if not CALL_ID_PATTERN.fullmatch(call_id):
        raise AssetError("unsafe call id", path=call_id, code="cli_error", exit_code=2)
    return work_root.resolve() / ".agent-calls" / f"{iteration:02d}" / role / call_id


def stage_directory(target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent))


def copy_input(source: Path, destination: Path, *, authorized_root: Path | None = None) -> str:
    if authorized_root is not None:
        source = ensure_under(source, authorized_root)
    if not source.is_file():
        raise AssetError("input file does not exist", path=str(source), code="missing_input", exit_code=3)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    before, after = sha256_file(source), sha256_file(destination)
    if before != after:
        raise AssetError("input changed while the call package was created", path=str(source), code="hash_conflict", exit_code=9)
    return after


def load_call_bundle(call_dir: Path, *, work_root: Path, role: str, mode: str, schema_dir: Path = SCHEMA_DIR) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    call_dir = ensure_under(call_dir, work_root)
    if not call_dir.is_dir():
        raise AssetError("call directory does not exist", path=str(call_dir), code="missing_input", exit_code=3)
    allowed = {"call_manifest.json", "system_prompt.md", "inputs", "raw_response.json", "call_record.json"}
    actual = {item.name for item in call_dir.iterdir()}
    if actual != allowed:
        raise AssetError("call directory contains missing or unexpected entries", path=str(call_dir), code="call_bundle")
    manifest = load_json(call_dir / "call_manifest.json")
    if manifest.get("schema_version") != "1.3" or manifest.get("role") != role or manifest.get("mode") != mode:
        raise AssetError("call manifest role or mode mismatch", path=str(call_dir / "call_manifest.json"), code="call_bundle")
    expected = expected_call_dir(work_root, int(manifest["iteration"]), role, str(manifest["call_id"]))
    if call_dir != expected:
        raise AssetError("call directory does not match manifest identity", path=str(call_dir), code="call_bundle")

    config, config_path, prompt_path, output_schema_path = load_role(role, schema_dir)
    expected_config_hash = canonical_yaml_hash(config_path)
    expected_prompt_hash = sha256_bytes(normalized_text_bytes(prompt_path))
    expected_schema_hash = sha256_file(output_schema_path)
    if manifest.get("config_sha256") != expected_config_hash or manifest.get("prompt_sha256") != expected_prompt_hash or manifest.get("output_schema_sha256") != expected_schema_hash:
        raise AssetError("agent role configuration changed after call preparation", path=str(call_dir), code="hash_conflict", exit_code=9)
    if sha256_file(call_dir / "system_prompt.md") != expected_prompt_hash:
        raise AssetError("system prompt hash mismatch", path=str(call_dir / "system_prompt.md"), code="hash_conflict", exit_code=9)

    input_hashes: dict[str, str] = {}
    for item in manifest.get("inputs", []):
        name = item.get("name")
        filename = item.get("filename")
        if not isinstance(name, str) or not isinstance(filename, str) or Path(filename).is_absolute() or ".." in Path(filename).parts:
            raise AssetError("invalid call input entry", path=str(call_dir / "call_manifest.json"), code="call_bundle")
        path = ensure_under(call_dir / "inputs" / filename, call_dir / "inputs")
        digest = sha256_file(path) if path.is_file() else ""
        if digest != item.get("sha256"):
            raise AssetError("call input hash mismatch", path=str(path), code="hash_conflict", exit_code=9)
        input_hashes[name] = digest
    base_inputs = set(config["input_profiles"][mode])
    actual_inputs = set(input_hashes)
    if actual_inputs != base_inputs:
        raise AssetError("call input allowlist does not match role profile", path=str(call_dir), code="input_allowlist")

    record = load_json(call_dir / "call_record.json")
    validate_schema("agent_call_record", record, schema_dir)
    validate_semantics("agent_call_record", record)
    comparisons = {
        "task_id": manifest["task_id"], "iteration": manifest["iteration"], "role": role,
        "role_version": config["role_version"],
        "model_selection_mode": manifest["model_selection_mode"],
        "requested_model": manifest["requested_model"],
        "config_sha256": expected_config_hash, "prompt_sha256": expected_prompt_hash,
        "output_schema_sha256": expected_schema_hash, "input_sha256": input_hashes,
        "parameters": manifest["parameters"], "call_id": manifest["call_id"], "parent_context_id": None,
    }
    for key, expected_value in comparisons.items():
        if record.get(key) != expected_value:
            raise AssetError(f"call record field does not match manifest: {key}", path=str(call_dir / "call_record.json"), code="call_record")
    if record["status"] != "succeeded":
        raise AssetError("agent call did not succeed", path=str(call_dir / "call_record.json"), code="agent_runtime", exit_code=5)

    response = load_json(call_dir / "raw_response.json")
    response_kind = "planner_response" if role == "planner" else "reviewer_response"
    validate_schema(response_kind, response, schema_dir)
    validate_semantics(response_kind, response)
    return manifest, record, response, input_hashes


def provenance_entry(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "model_selection_mode": record["model_selection_mode"],
        "requested_model": record["requested_model"],
        "config_sha256": record["config_sha256"],
        "prompt_sha256": record["prompt_sha256"],
        "temperature": record["parameters"]["temperature"],
        "top_p": record["parameters"]["top_p"],
        "seed": record["parameters"]["seed"],
        "call_id": record["call_id"],
        "context_id": record["context_id"],
        "parent_context_id": record["parent_context_id"],
    }
