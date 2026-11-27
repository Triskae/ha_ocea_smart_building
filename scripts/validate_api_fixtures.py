"""Validate offline API fixtures against their OpenAPI JSON schemas."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

API = Path(__file__).resolve().parents[1] / "docs" / "api"
FIXTURES = API / "fixtures"


def dereference(value: dict, spec: dict) -> dict:
    """Resolve local OpenAPI references, rejecting reference cycles."""
    seen: set[str] = set()
    while "$ref" in value:
        ref = value["$ref"]
        if not isinstance(ref, str) or not ref.startswith("#/") or ref in seen:
            raise ValueError(f"Invalid or cyclic reference: {ref}")
        seen.add(ref)
        value = spec
        for part in ref[2:].split("/"):
            value = value[part.replace("~1", "/").replace("~0", "~")]
    return value


def fixture_schema_ref(operation: dict, fixture: dict, spec: dict) -> str:
    """Resolve the schema for the fixture's exact request or response."""
    kind = fixture["kind"]
    if kind == "request":
        if "status" in fixture:
            raise ValueError("A request fixture must not specify a response status")
        body = operation["requestBody"]
    elif kind == "response":
        status = fixture["status"]
        if not isinstance(status, str) or not status.isdigit():
            raise ValueError("A response fixture must specify an HTTP status string")
        body = operation["responses"][status]
    else:
        raise ValueError(f"Invalid fixture kind: {kind}")
    body = dereference(body, spec)
    schema = body["content"]["application/json"]["schema"]
    ref = schema.get("$ref")
    if not isinstance(ref, str) or not ref.startswith("#/components/schemas/"):
        raise ValueError("Fixture content must reference a local component schema")
    if fixture["schema"] != ref:
        raise ValueError("Fixture schema does not match the operation content")
    return ref


def main() -> None:
    """Validate fixture metadata, paths, and content."""
    from jsonschema import Draft202012Validator

    spec = yaml.safe_load((API / "openapi.yaml").read_text(encoding="utf-8"))
    manifest = yaml.safe_load((FIXTURES / "manifest.yaml").read_text(encoding="utf-8"))
    if manifest["version"] != 1:
        raise ValueError("Unsupported fixture manifest version")
    operations = {
        operation["operationId"]: operation
        for path in spec["paths"].values()
        for method, operation in path.items()
        if method in {"get", "post"} and isinstance(operation, dict)
    }
    seen = set()
    for fixture in manifest["fixtures"]:
        relative = Path(fixture["file"])
        path = (FIXTURES / relative).resolve()
        if relative.is_absolute() or not path.is_relative_to(FIXTURES.resolve()):
            raise ValueError("Fixture path escapes the fixture directory")
        if path in seen:
            raise ValueError(f"Duplicate fixture: {relative}")
        seen.add(path)
        if fixture["operationId"] not in operations:
            raise ValueError(f"Unknown fixture operation: {fixture['operationId']}")
        if fixture["evidence"] not in {"observed", "verified"}:
            raise ValueError(f"Unsupported fixture evidence: {fixture['evidence']}")
        if not fixture["source"] or not fixture["anonymization"]:
            raise ValueError(f"Missing fixture provenance: {relative}")
        ref = fixture_schema_ref(operations[fixture["operationId"]], fixture, spec)
        schema = {**spec, "$ref": ref}
        Draft202012Validator(schema).validate(json.loads(path.read_text(encoding="utf-8")))
    if seen != {path.resolve() for path in FIXTURES.rglob("*.json")}:
        raise ValueError("Every JSON fixture must be listed in the manifest")
    print(f"Validated {len(seen)} API fixture(s)")


if __name__ == "__main__":
    main()
