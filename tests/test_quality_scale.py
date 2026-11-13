"""Tests for the quality scale tracking file."""

from __future__ import annotations

import inspect
from importlib import import_module
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
import yaml
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

from custom_components import ocea_smart_building as integration
from custom_components.ocea_smart_building import (
    PLATFORMS,
    binary_sensor,
    coordinator,
    entity,
    sensor,
)

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
PROTECTED_RULES = {"brands", "common-modules", "parallel-updates"}


def _rules() -> dict[str, Any]:
    return yaml.safe_load(QUALITY_SCALE.read_text(encoding="utf-8"))["rules"]


def _assert_valid_statuses(rules: dict[str, Any]) -> None:
    for name, value in rules.items():
        if isinstance(value, dict):
            assert value.get("status") == "exempt", name
            comment = value.get("comment")
            assert isinstance(comment, str) and comment.strip(), name
        else:
            assert isinstance(value, str) and value in {"done", "todo"}, name


def _assert_protected_rules(rules: dict[str, Any]) -> None:
    for name in PROTECTED_RULES:
        assert rules.get(name) == "done", f"{name} must remain done"


def _assert_class_layout(
    cls: type, base: type, expected_module: str
) -> None:
    assert issubclass(cls, base), f"{cls.__name__} must inherit from {base.__name__}"
    assert cls.__module__ == expected_module, (
        f"{cls.__name__} must be defined in {expected_module}"
    )


def _assert_parallel_updates(platform: ModuleType | SimpleNamespace) -> None:
    value = vars(platform).get("PARALLEL_UPDATES")
    assert type(value) is int and value == 0, (
        "Entity platforms must explicitly declare PARALLEL_UPDATES = 0"
    )


def test_quality_scale_lists_every_rule() -> None:
    """Test no rule from bronze to platinum is missing from the file."""
    assert set(_rules()) == ALL_RULES


def test_quality_scale_statuses_are_valid() -> None:
    """Test each rule is done, todo, or an exemption with a reason."""
    _assert_valid_statuses(_rules())


def test_quality_scale_protected_rules_remain_done() -> None:
    """Make changes to guarded claims an explicit review decision."""
    _assert_protected_rules(_rules())


@pytest.mark.parametrize("comment", [None, "", "   ", 1])
def test_exempt_rule_requires_a_nonempty_text_reason(comment: object) -> None:
    """Reject missing, blank, and non-text exemption reasons."""
    with pytest.raises(AssertionError):
        _assert_valid_statuses({"example": {"status": "exempt", "comment": comment}})


def test_valid_unprotected_statuses() -> None:
    """Allow pending rules and justified exemptions."""
    _assert_valid_statuses(
        {"pending": "todo", "exempt": {"status": "exempt", "comment": "No actions"}}
    )


@pytest.mark.parametrize(
    "value", ["todo", {"status": "exempt", "comment": "No longer applicable"}, None]
)
def test_protected_rule_status_regression_is_detected(value: object) -> None:
    """Require a deliberate change to the protected rule set."""
    rules = dict.fromkeys(PROTECTED_RULES, "done")
    rules["common-modules"] = value
    with pytest.raises(AssertionError, match="common-modules"):
        _assert_protected_rules(rules)


def test_common_modules_for_active_coordinator_and_entity_classes() -> None:
    """Guard the integration's shared classes, not the whole HA rule."""
    _assert_class_layout(
        coordinator.OceaDataUpdateCoordinator,
        DataUpdateCoordinator,
        "custom_components.ocea_smart_building.coordinator",
    )
    _assert_class_layout(
        entity.OceaEntity,
        CoordinatorEntity,
        "custom_components.ocea_smart_building.entity",
    )
    assert integration.OceaDataUpdateCoordinator is coordinator.OceaDataUpdateCoordinator
    assert sensor.OceaDataUpdateCoordinator is coordinator.OceaDataUpdateCoordinator
    assert binary_sensor.OceaDataUpdateCoordinator is coordinator.OceaDataUpdateCoordinator
    for platform in (sensor, binary_sensor):
        entity_classes = [
            cls
            for _, cls in inspect.getmembers(platform, inspect.isclass)
            if cls.__module__ == platform.__name__ and issubclass(cls, Entity)
        ]
        assert entity_classes, platform.__name__
        assert all(issubclass(cls, entity.OceaEntity) for cls in entity_classes)


@pytest.mark.parametrize("invalid", ["module", "base"])
def test_common_module_guard_detects_wrong_module_or_base(invalid: str) -> None:
    """Exercise both parts of the shared class guard."""
    class Base:
        pass

    class Correct(Base):
        pass

    cls = Correct if invalid == "module" else object
    expected_module = "wrong.module" if invalid == "module" else object.__module__
    with pytest.raises(AssertionError):
        _assert_class_layout(cls, Base, expected_module)


def test_all_configured_platforms_declare_parallel_updates() -> None:
    """Keep coordinator-backed entity platforms on the shared update schedule."""
    assert PLATFORMS
    for platform in PLATFORMS:
        module = import_module(f"custom_components.ocea_smart_building.{platform}")
        _assert_parallel_updates(module)


@pytest.mark.parametrize("value", [None, 1, False, "0"])
def test_parallel_updates_guard_detects_missing_or_invalid_value(value: object) -> None:
    """Reject absent, incorrect, or incorrectly typed platform settings."""
    platform = SimpleNamespace() if value is None else SimpleNamespace(PARALLEL_UPDATES=value)
    with pytest.raises(AssertionError, match="PARALLEL_UPDATES"):
        _assert_parallel_updates(platform)
