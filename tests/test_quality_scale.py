"""Tests for the quality scale tracking file."""

from __future__ import annotations

from pathlib import Path

import yaml

QUALITY_SCALE = (
    Path(__file__).parents[1]
    / "custom_components"
    / "ocea_smart_building"
    / "quality_scale.yaml"
)
BRONZE_RULES = {
    "action-setup",
    "appropriate-polling",
    "brands",
    "common-modules",
    "config-flow-test-coverage",
    "config-flow",
    "dependency-transparency",
    "docs-actions",
    "docs-triggers",
    "docs-conditions",
    "docs-high-level-description",
    "docs-installation-instructions",
    "docs-removal-instructions",
    "entity-event-setup",
    "entity-unique-id",
    "has-entity-name",
    "runtime-data",
    "test-before-configure",
    "test-before-setup",
    "unique-config-entry",
}
SILVER_RULES = {
    "action-exceptions",
    "config-entry-unloading",
    "docs-configuration-parameters",
    "docs-installation-parameters",
    "entity-unavailable",
    "integration-owner",
    "log-when-unavailable",
    "parallel-updates",
    "reauthentication-flow",
    "test-coverage",
}
GOLD_RULES = {
    "devices",
    "diagnostics",
    "discovery-update-info",
    "discovery",
    "docs-data-update",
    "docs-examples",
    "docs-known-limitations",
    "docs-supported-devices",
    "docs-supported-functions",
    "docs-troubleshooting",
    "docs-use-cases",
    "dynamic-devices",
    "entity-category",
    "entity-device-class",
    "entity-disabled-by-default",
    "entity-translations",
    "exception-translations",
    "icon-translations",
    "reconfiguration-flow",
    "repair-issues",
    "stale-devices",
}
PLATINUM_RULES = {"async-dependency", "inject-websession", "strict-typing"}
ALL_RULES = BRONZE_RULES | SILVER_RULES | GOLD_RULES | PLATINUM_RULES


def _rules() -> dict:
    return yaml.safe_load(QUALITY_SCALE.read_text(encoding="utf-8"))["rules"]


def test_quality_scale_lists_every_rule() -> None:
    """Test no rule from bronze to platinum is missing from the file."""
    assert set(_rules()) == ALL_RULES


def test_quality_scale_statuses_are_valid() -> None:
    """Test each rule is done, todo, or an exemption with a reason."""
    for name, value in _rules().items():
        if isinstance(value, dict):
            assert value.get("status") == "exempt", name
            assert value.get("comment"), name
        else:
            assert value in {"done", "todo"}, name
