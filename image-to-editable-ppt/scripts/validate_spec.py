from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml
from schema_utils import ContractError, SCHEMA_FILES, cross_validate, load_json, load_yaml, validate_build_ready, validate_schema, validate_semantics


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Validate Image to Editable PPT contracts.")
    result.add_argument("--schema-dir", type=Path, default=Path(__file__).resolve().parents[1] / "schemas")
    result.add_argument("--phase", choices=("preflight", "build-ready"), default="preflight")
    for kind in SCHEMA_FILES:
        result.add_argument("--" + kind.replace("_", "-"), dest=kind, type=Path)
    return result


def main() -> int:
    args = parser().parse_args()
    selected = {kind: getattr(args, kind) for kind in SCHEMA_FILES if getattr(args, kind) is not None}
    if not selected:
        parser().error("at least one contract path is required")
    try:
        documents = {}
        for kind, path in selected.items():
            if not path.is_file():
                raise FileNotFoundError(path)
            document = load_yaml(path) if kind == "agent_role" else load_json(path)
            validate_schema(kind, document, args.schema_dir)
            validate_semantics(kind, document)
            documents[kind] = document
        if args.phase == "build-ready" and "asset_manifest" in documents:
            cropped_assets = [
                item["id"]
                for item in documents["asset_manifest"]["assets"]
                if item["source"] == "cropped"
            ]
            if cropped_assets and "asset_processing_report" not in documents:
                raise ContractError(
                    [
                        {
                            "path": "$.asset_processing_report",
                            "code": "missing_processing_evidence",
                            "message": (
                                "build-ready validation requires asset_processing_report "
                                f"for cropped assets: {sorted(cropped_assets)}"
                            ),
                        }
                    ]
                )
        cross_validate(documents)
        if args.phase == "build-ready" and "asset_manifest" in documents:
            validate_build_ready(selected["asset_manifest"], documents["asset_manifest"])
        print(json.dumps({"status": "ok", "component": "validate_spec", "phase": args.phase, "validated": sorted(documents), "error": None}, ensure_ascii=False))
        return 0
    except FileNotFoundError as exc:
        print(json.dumps({"status": "error", "component": "validate_spec", "error": {"exit_code": 3, "category": "input", "message": f"missing file: {exc}"}}, ensure_ascii=False))
        return 3
    except (json.JSONDecodeError, yaml.YAMLError, ContractError) as exc:
        errors = exc.errors if isinstance(exc, ContractError) else [{"path": "$", "code": "invalid_json", "message": str(exc)}]
        print(json.dumps({"status": "error", "component": "validate_spec", "error": {"exit_code": 4, "category": "contract", "message": "contract validation failed", "details": errors}}, ensure_ascii=False))
        return 4
    except Exception as exc:
        print(json.dumps({"status": "error", "component": "validate_spec", "error": {"exit_code": 70, "category": "internal", "message": str(exc)}}, ensure_ascii=False))
        return 70


if __name__ == "__main__":
    raise SystemExit(main())
