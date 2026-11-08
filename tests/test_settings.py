"""Tests for integration release settings."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

INTEGRATION_PATH = (
    Path(__file__).parents[1] / "custom_components" / "ocea_smart_building"
)
CI_WORKFLOW_PATH = Path(__file__).parents[1] / ".github" / "workflows" / "ci.yml"
RELEASE_WORKFLOW_PATH = (
    Path(__file__).parents[1] / ".github" / "workflows" / "release.yml"
)


def _assert_ci_quality_gate(workflow: str) -> None:
    """Assert the CI workflow keeps every required validation."""
    assert "uses: actions/checkout@v6" in workflow
    assert "fetch-depth: 2" in workflow
    assert (
        "uses: astral-sh/setup-uv@08807647e7069bb48b6ef5acd8ec9567f424441b"
        in workflow
    )
    assert 'python-version: "3.14.2"' in workflow
    assert "--with homeassistant==2026.7.3" in workflow
    assert "--with pytest-homeassistant-custom-component" in workflow
    assert "python -m pytest -q" in workflow
    assert 'uvx --from "ruff==0.16.1"' in workflow
    assert "ruff check custom_components tests" in workflow
    assert "python -m compileall -q custom_components tests ocea_cli.py" in workflow
    assert "python3 -m json.tool" in workflow
    assert "custom_components/ocea_smart_building/manifest.json" in workflow
    assert "custom_components/ocea_smart_building/strings.json" in workflow
    assert "custom_components/ocea_smart_building/translations/en.json" in workflow
    assert "custom_components/ocea_smart_building/translations/fr.json" in workflow
    assert "git diff --check HEAD^ HEAD" in workflow


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
    assert manifest["name"] == "OCEA Smart Building"
    assert f"Home-Assistant-OCEA-Smart-Building/{manifest['version']}" in constants.UA


def test_release_workflow_is_tag_only() -> None:
    """Test normal branch pushes cannot publish a GitHub release."""
    workflow = RELEASE_WORKFLOW_PATH.read_text(encoding="utf-8")

    assert 'tags:\n      - "v*.*.*"' in workflow
    assert "branches:" not in workflow
    assert "permissions:\n  contents: write" in workflow
    assert "uses: actions/checkout@v4" in workflow
    assert "fetch-depth: 0" in workflow
    assert 're.fullmatch(r"v\\d+\\.\\d+\\.\\d+", tag)' in workflow
    assert 'manifest["version"] != version' in workflow
    assert "constants.INTEGRATION_VERSION != version" in workflow
    assert "expected_user_agent not in constants.UA" in workflow
    assert '--title "OCEA Smart Building $GITHUB_REF_NAME"' in workflow
    assert 'gh release create "$GITHUB_REF_NAME"' in workflow
    assert '--verify-tag' in workflow
    assert '--latest' in workflow
    assert "previous_tag=$(git describe --tags" in workflow
    assert "/compare/$previous_tag...$GITHUB_REF_NAME" in workflow


def test_ci_workflow_validates_main_and_pull_requests() -> None:
    """Test CI runs the required checks without publishing releases."""
    workflow = CI_WORKFLOW_PATH.read_text(encoding="utf-8")

    assert "push:\n    branches:\n      - main" in workflow
    assert "pull_request:\n    branches:\n      - main" in workflow
    assert "workflow_dispatch:" in workflow
    assert "cancel-in-progress: true" in workflow
    assert "permissions:\n  contents: read" in workflow
    _assert_ci_quality_gate(workflow)
    assert "gh release create" not in workflow
