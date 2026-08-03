"""Tests for integration release settings."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

INTEGRATION_PATH = (
    Path(__file__).parents[1] / "custom_components" / "ocea_smart_building"
)
RELEASE_WORKFLOW_PATH = (
    Path(__file__).parents[1] / ".github" / "workflows" / "release.yml"
)


def _load_constants() -> ModuleType:
    """Load integration constants without importing Home Assistant."""
    spec = importlib.util.spec_from_file_location(
        "ocea_smart_building_const",
        INTEGRATION_PATH / "const.py",
    )
    assert spec is not None
    assert spec.loader is not None
    constants = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(constants)
    return constants


def test_manifest_and_user_agent_versions_are_aligned() -> None:
    """Test manifest, integration, and User-Agent versions match."""
    manifest = json.loads(
        (INTEGRATION_PATH / "manifest.json").read_text(encoding="utf-8")
    )
    constants = _load_constants()

    assert constants.INTEGRATION_VERSION == manifest["version"]
    assert f"Home-Assistant-Ocea-Smart-Building/{manifest['version']}" in constants.UA


def test_release_workflow_is_tag_only() -> None:
    """Test normal branch pushes cannot publish a GitHub release."""
    workflow = RELEASE_WORKFLOW_PATH.read_text(encoding="utf-8")

    assert 'tags:\n      - "v*.*.*"' in workflow
    assert "branches:" not in workflow
    assert 'gh release create "$GITHUB_REF_NAME"' in workflow
    assert "previous_tag=$(git describe --tags" in workflow
    assert "/compare/$previous_tag...$GITHUB_REF_NAME" in workflow
