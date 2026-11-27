"""Offline consistency checks for the OCEA API reference."""

from __future__ import annotations

import ast
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from scripts.validate_api_fixtures import fixture_schema_ref

ROOT = Path(__file__).parents[1]
API = ROOT / "docs" / "api"
CLIENT = ROOT / "custom_components" / "ocea_smart_building" / "ocea" / "client.py"
METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}


class UniqueKeyLoader(yaml.SafeLoader):
    """Reject duplicate mapping keys instead of silently replacing operations."""

    def construct_mapping(self, node, deep=False):
        """Construct a mapping with unique keys."""
        self.flatten_mapping(node)
        mapping = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in mapping:
                raise ValueError(f"Duplicate YAML key: {key}")
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


def load_yaml(path: Path) -> dict:
    """Load a reference document without accepting duplicate keys."""
    data = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueKeyLoader)
    assert isinstance(data, dict)
    return data


def validate_references(document: dict) -> None:
    """Resolve every local JSON pointer, rejecting external references."""
    def walk(value):
        if isinstance(value, dict):
            if "$ref" in value:
                ref = value["$ref"]
                assert isinstance(ref, str) and ref.startswith("#/"), f"Invalid ref: {ref}"
                target = document
                for part in ref[2:].split("/"):
                    part = part.replace("~1", "/").replace("~0", "~")
                    assert isinstance(target, dict) and part in target, f"Broken ref: {ref}"
                    target = target[part]
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(document)


def validate_inventory(inventory: dict, coverage: dict, spec: dict) -> None:
    """Check inventory, client coverage and supported OpenAPI operations agree."""
    assert inventory["version"] == coverage["version"] == 1
    entries = inventory["operations"]
    assert isinstance(entries, list) and entries
    known = {}
    supported = {}
    for entry in entries:
        assert isinstance(entry, dict)
        op_id = entry["id"]
        assert isinstance(op_id, str) and op_id and op_id not in known
        known[op_id] = entry
        assert isinstance(entry["source"], str) and entry["source"]
        assert isinstance(entry["domain"], str) and entry["domain"]
        evidence = entry["evidence"]
        assert isinstance(evidence, list) and evidence
        assert len(evidence) == len(set(evidence))
        assert set(evidence) <= {"discovered", "observed", "verified", "implemented"}
        observations = entry["observations"]
        assert isinstance(observations, list)
        assert len(observations) == len(set(observations))
        assert set(observations) <= {"request", "response"}
        assert ("observed" in evidence) == bool(observations)
        if "verified" in evidence:
            assert {"discovered", "observed"} <= set(evidence)
        assert isinstance(entry["note"], str) and entry["note"]
        assert entry["access"] == "read_only"
        assert entry["method"] in {"GET", "POST"}
        assert isinstance(entry["path"], str) and entry["path"].startswith("/api/v1/")
        key = (entry["path"], entry["method"].lower())
        assert key not in supported
        supported[key] = op_id

    assert spec["openapi"].startswith("3.1.")
    found = {}
    for path, path_item in spec["paths"].items():
        for method, operation in path_item.items():
            if method not in METHODS:
                continue
            key = (path, method)
            assert key in supported, f"Unsupported operation: {method} {path}"
            assert operation["operationId"] == supported[key]
            assert operation["x-ocea-access-mode"] == "read-only"
            if method == "post":
                assert operation.get("description"), "POST needs an evidence explanation"
            assert operation["operationId"] not in found
            found[operation["operationId"]] = key
    assert set(found) == set(supported.values())
    validate_references(spec)

    class_node = next(
        node for node in ast.parse(CLIENT.read_text(encoding="utf-8")).body
        if isinstance(node, ast.ClassDef) and node.name == "OCEAApiClient"
    )
    client_methods = {
        node.name for node in class_node.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    covered = set()
    for entry in coverage["operations"]:
        op_id = entry["id"]
        assert op_id in found and op_id not in covered
        covered.add(op_id)
        status = entry["status"]
        assert status in {"implemented", "partially_implemented", "not_implemented", "unclear"}
        methods = entry.get("methods", [])
        assert isinstance(methods, list) and set(methods) <= client_methods
        if status in {"implemented", "partially_implemented"}:
            assert methods and "implemented" in known[op_id]["evidence"]
        else:
            assert not methods and "implemented" not in known[op_id]["evidence"]
    assert covered == set(found)


def validate_fixture_observations(inventory: dict, manifest: dict) -> None:
    """Require each fixture's direction to be recorded in the inventory."""
    operations = {entry["id"]: entry for entry in inventory["operations"]}
    for fixture in manifest["fixtures"]:
        assert fixture["operationId"] in operations
        assert fixture["kind"] in operations[fixture["operationId"]]["observations"]


@pytest.fixture
def reference():
    """Read the three reference documents."""
    return (
        load_yaml(API / "inventory" / "endpoints.yaml"),
        load_yaml(API / "inventory" / "client_coverage.yaml"),
        load_yaml(API / "openapi.yaml"),
    )


def test_reference_is_consistent(reference):
    """All supported operations and client methods are cross-referenced."""
    validate_inventory(*reference)


@pytest.mark.parametrize("direction", ["request", "response"])
def test_observation_can_be_one_direction_without_a_fixture(reference, direction):
    """An observation need not cover both directions or have a fixture."""
    inventory, coverage, spec = deepcopy(reference)
    entry = inventory["operations"][0]
    entry["evidence"].append("observed")
    entry["observations"] = [direction]
    validate_inventory(inventory, coverage, spec)
    validate_fixture_observations(inventory, {"fixtures": []})


@pytest.mark.parametrize(
    ("evidence", "observations"),
    [
        (["discovered", "observed"], []),
        (["discovered"], ["request"]),
        (["discovered", "observed"], ["unknown"]),
        (["discovered", "observed"], ["request", "request"]),
        (["discovered", "verified"], []),
    ],
)
def test_inconsistent_observation_is_rejected(reference, evidence, observations):
    """Evidence labels and valid, unique observation directions must agree."""
    inventory, coverage, spec = deepcopy(reference)
    inventory["operations"][0]["evidence"] = evidence
    inventory["operations"][0]["observations"] = observations
    with pytest.raises(AssertionError):
        validate_inventory(inventory, coverage, spec)


def test_fixture_directions_match_inventory(reference):
    """A fixture must not imply an unrecorded observation."""
    inventory, _, _ = reference
    manifest = load_yaml(API / "fixtures" / "manifest.yaml")
    validate_fixture_observations(inventory, manifest)
    inventory = deepcopy(inventory)
    operation = next(
        entry for entry in inventory["operations"]
        if entry["id"] == manifest["fixtures"][0]["operationId"]
    )
    operation["observations"].remove(manifest["fixtures"][0]["kind"])
    with pytest.raises(AssertionError):
        validate_fixture_observations(inventory, manifest)


def test_client_coverage_is_sorted_by_operation_id(reference):
    """Keep the coverage inventory easy to scan."""
    _, coverage, _ = reference
    operation_ids = [entry["id"] for entry in coverage["operations"]]
    assert operation_ids == sorted(operation_ids)


def test_inventory_excludes_file_operations(reference):
    """Keep the reference focused on the integration's data APIs."""
    inventory, _, _ = reference
    for operation in inventory["operations"]:
        assert operation["domain"] != "documents"
        assert not operation["id"].lower().startswith("export")


def test_broken_schema_reference_is_rejected(reference):
    """A missing schema must not silently pass validation."""
    inventory, coverage, spec = deepcopy(reference)
    spec["paths"]["/api/v1/resident"]["get"]["responses"]["200"]["content"][
        "application/json"
    ]["schema"]["$ref"] = "#/components/schemas/Missing"
    with pytest.raises(AssertionError, match="Broken ref"):
        validate_inventory(inventory, coverage, spec)


@pytest.mark.parametrize("classification", ["unknown", "state_changing"])
def test_other_inventory_classifications_are_rejected(reference, classification):
    """Only evidenced consultation operations belong in the inventory."""
    inventory, coverage, spec = deepcopy(reference)
    inventory["operations"][0]["access"] = classification
    with pytest.raises(AssertionError):
        validate_inventory(inventory, coverage, spec)


def test_documented_values_remain_open(reference):
    """Known fluid, unit and granularity examples are not closed lists."""
    _, _, spec = reference
    schemas = spec["components"]["schemas"]
    cases = (
        (schemas["Fluid"], {"EauFroide", "Temperature", "Hygrometrie"}),
        (schemas["ConsumptionUnit"], {"m3", "kWh"}),
        (schemas["ConsumptionQuery"]["properties"]["granularity"], {"Day", "Month", "Hour"}),
    )
    for schema, expected in cases:
        assert expected <= set(schema["examples"])
        assert not {"enum", "const", "pattern", "not", "oneOf", "anyOf", "allOf"} & schema.keys()


def test_partial_response_types_preserve_client_variants(reference):
    """Document accepted types without making optional fields mandatory."""
    _, _, spec = reference
    schemas = spec["components"]["schemas"]
    expected = {
        "Pds": {
            "id": {"string", "integer", "null"},
            "fce": {"number", "null"},
        },
        "Device": {
            "pdsId": {"string", "integer", "null"},
            "datePose": {"string", "null"},
        },
        "ConsumptionItem": {
            "date": {"string", "null"},
            "valeur": {"number", "string", "null"},
            "consentement": {"boolean", "null"},
            "fuiteEstimee": {"number", "string", "null"},
        },
    }
    for name, fields in expected.items():
        schema = schemas[name]
        assert not schema.get("required")
        for field, types in fields.items():
            assert set(schema["properties"][field]["type"]) == types
    assert "format" not in schemas["ConsumptionItem"]["properties"]["date"]
    assert "type" not in schemas["ConsumptionItem"]["properties"]["isEstimated"]
    assert schemas["Resident"]["properties"]["consentement"]["$ref"].endswith("/Consent")
    assert schemas["Dwelling"]["properties"]["consentement"]["$ref"].endswith("/Consent")
    assert set(schemas["Consent"]["type"]) == {"object", "null"}


def test_observed_nested_resident_and_dwelling_shapes(reference):
    """Keep the observed structures distinct and their fields optional."""
    _, _, spec = reference
    schemas = spec["components"]["schemas"]
    consent = schemas["Consent"]
    assert set(consent["properties"]["isValid"]["type"]) == {"boolean", "null"}
    assert set(consent["properties"]["lastUpdateConsentement"]["type"]) == {"string", "null"}
    assert not consent.get("required")
    resident = schemas["Resident"]["properties"]
    notification = resident["notifications"]["items"]
    assert set(notification["properties"]["configurationNotification"]["properties"][
        "notifMail"
    ]["type"]) == {"boolean", "null"}
    address = schemas["Dwelling"]["properties"]["local"]["properties"]["adresse"]
    assert set(address["properties"]["codePostal"]["type"]) == {"string", "integer", "null"}
    assert not address.get("required")
    assert "format" not in consent["properties"]["lastUpdateConsentement"]


def test_missing_or_unclassified_coverage_is_rejected(reference):
    """Coverage must point to a known supported operation."""
    inventory, coverage, spec = deepcopy(reference)
    coverage["operations"][0]["id"] = "nonexistent"
    with pytest.raises(AssertionError):
        validate_inventory(inventory, coverage, spec)


def test_duplicate_yaml_keys_are_rejected():
    """A duplicate path or operation cannot be hidden by YAML parsing."""
    with pytest.raises(ValueError, match="Duplicate YAML key"):
        yaml.load("key: first\nkey: second\n", Loader=UniqueKeyLoader)


def test_fixture_schema_matches_request_or_response(reference):
    """The right schema must be selected for each direction."""
    _, _, spec = reference
    operation = spec["paths"]["/api/v1/local/appareils"]["post"]
    request = {
        "kind": "request",
        "schema": "#/components/schemas/DevicesRequest",
    }
    response = {
        "kind": "response",
        "status": "200",
        "schema": "#/components/schemas/DeviceList",
    }
    assert fixture_schema_ref(operation, request, spec) == request["schema"]
    assert fixture_schema_ref(operation, response, spec) == response["schema"]
    response["schema"] = request["schema"]
    with pytest.raises(ValueError, match="does not match"):
        fixture_schema_ref(operation, response, spec)
