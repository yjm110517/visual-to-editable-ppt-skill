from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from asset_common import AssetError, atomic_write_json, is_safe_relative_path
from schema_utils import error, json_path, load_json


LEGACY_SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas" / "legacy" / "v1.3"
SPEC_FILES = {
    "layout": "layout.json",
    "crops": "crops.json",
    "asset_manifest": "asset_manifest.json",
}
LEGACY_SCHEMAS = {
    "layout": "layout.schema.json",
    "crops": "crops.schema.json",
    "asset_manifest": "asset-manifest.schema.json",
}
DERIVED_ASSET_FIELDS = ("width_px", "height_px", "size_bytes", "sha256", "view_box")


def _validate_legacy(kind: str, document: dict[str, Any]) -> None:
    schema = load_json(LEGACY_SCHEMA_DIR / LEGACY_SCHEMAS[kind])
    failures = [
        error(json_path(item.absolute_path), item.message, "legacy_schema_error")
        for item in sorted(Draft202012Validator(schema).iter_errors(document), key=lambda item: list(item.absolute_path))
    ]
    if failures:
        raise AssetError(
            "legacy v1.3 contract validation failed: " + "; ".join(item["message"] for item in failures),
            path=kind,
            code="legacy_contract",
        )


def migrate_v13_spec_bundle(
    stage: Path,
    transition: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    if transition.get("from") != "1.3" or transition.get("to") != "1.4":
        raise AssetError("unsupported contract transition", path="$.contract_transition", code="contract_transition")
    documents = {kind: load_json(stage / filename) for kind, filename in SPEC_FILES.items()}
    if {document.get("schema_version") for document in documents.values()} != {"1.3"}:
        raise AssetError("v1.3 migration requires a complete v1.3 spec bundle", code="contract_transition")
    for kind, document in documents.items():
        _validate_legacy(kind, document)

    migrated = {kind: copy.deepcopy(document) for kind, document in documents.items()}
    for document in migrated.values():
        document["schema_version"] = "1.4"

    cropped_ids = {
        item["id"]
        for item in migrated["asset_manifest"]["assets"]
        if item["source"] == "cropped"
    }
    policies = transition["asset_boundary_policies"]
    if set(policies) != cropped_ids:
        missing = sorted(cropped_ids - set(policies))
        unexpected = sorted(set(policies) - cropped_ids)
        raise AssetError(
            f"contract migration requires exact cropped asset policy coverage; missing={missing}, unexpected={unexpected}",
            path="$.contract_transition.asset_boundary_policies",
            code="contract_transition",
        )
    for item in migrated["asset_manifest"]["assets"]:
        if item["id"] in policies:
            item["boundary_policy"] = policies[item["id"]]
            # Contract 1.4 requires asset-processing evidence for every cropped
            # asset. Legacy files cannot be grandfathered in because no
            # deterministic boundary report exists for their bytes.
            for field in DERIVED_ASSET_FIELDS:
                item.pop(field, None)
            item["security_status"] = "pending"
            relative = item.get("path")
            if relative and is_safe_relative_path(relative):
                candidate = (stage / relative).resolve()
                try:
                    candidate.relative_to(stage.resolve())
                except ValueError:
                    candidate = None
                if candidate is not None and candidate.is_file():
                    candidate.unlink()
    if "relationship_groups" in transition:
        migrated["layout"]["relationship_groups"] = copy.deepcopy(transition["relationship_groups"])

    for kind, filename in SPEC_FILES.items():
        atomic_write_json(stage / filename, migrated[kind])
    return migrated["layout"], migrated["crops"], migrated["asset_manifest"]
