"""Tests for the Ocea Smart Building options flow (scan interval)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ocea_smart_building.const import CONF_LOCAL_ID, DOMAIN

EMAIL = "resident@example.com"
PASSWORD = "stored-secret"
LOCAL_ID = "local-123"


@pytest.fixture
def config_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Create an existing config entry without options."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Ocea - Alice",
        unique_id=EMAIL,
        data={
            CONF_EMAIL: EMAIL,
            CONF_PASSWORD: PASSWORD,
            CONF_LOCAL_ID: LOCAL_ID,
        },
    )
    entry.add_to_hass(hass)
    return entry


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_options_form_shows_default_five_hours(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """Test the default scan interval (5h) is suggested when no option is set."""
    result = await hass.config_entries.options.async_init(config_entry.entry_id)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    assert result["data_schema"] is not None
    defaults = result["data_schema"]({})
    assert defaults == {CONF_SCAN_INTERVAL: "5"}


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_options_form_shows_current_value(hass: HomeAssistant) -> None:
    """Test the currently stored scan interval is suggested."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Ocea - Alice",
        unique_id=EMAIL,
        data={
            CONF_EMAIL: EMAIL,
            CONF_PASSWORD: PASSWORD,
            CONF_LOCAL_ID: LOCAL_ID,
        },
        options={CONF_SCAN_INTERVAL: 3600},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)

    defaults = result["data_schema"]({})
    assert defaults == {CONF_SCAN_INTERVAL: "1"}


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_options_update_stores_seconds_and_reloads(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """Test submitting the form stores seconds and reloads the entry."""
    client = MagicMock()
    client.get_consumptions.return_value = []
    with patch(
        "custom_components.ocea_smart_building.OceaApiClient", return_value=client
    ):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    with patch.object(
        hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
    ) as async_reload:
        result = await hass.config_entries.options.async_init(config_entry.entry_id)
        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {CONF_SCAN_INTERVAL: "12"},
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options == {CONF_SCAN_INTERVAL: 43200}
    async_reload.assert_called_once_with(config_entry.entry_id)


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_options_flow_does_not_change_credentials(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """Test the options flow never touches stored credentials."""
    with patch.object(hass.config_entries, "async_reload"):
        result = await hass.config_entries.options.async_init(config_entry.entry_id)
        await hass.config_entries.options.async_configure(
            result["flow_id"],
            {CONF_SCAN_INTERVAL: "24"},
        )
        await hass.async_block_till_done()

    assert config_entry.data == {
        CONF_EMAIL: EMAIL,
        CONF_PASSWORD: PASSWORD,
        CONF_LOCAL_ID: LOCAL_ID,
    }


def _translation_data(language: str) -> dict[str, Any]:
    """Load a translation file."""
    path = (
        Path(__file__).parents[1]
        / "custom_components"
        / DOMAIN
        / "translations"
        / f"{language}.json"
    )
    with path.open(encoding="utf-8") as translation_file:
        return json.load(translation_file)


def test_options_source_strings_are_complete() -> None:
    """Test source strings contain the options step and selector labels."""
    path = (
        Path(__file__).parents[1] / "custom_components" / DOMAIN / "strings.json"
    )
    with path.open(encoding="utf-8") as strings_file:
        strings = json.load(strings_file)

    step = strings["options"]["step"]["init"]
    assert set(step["data"]) == {CONF_SCAN_INTERVAL}
    options = strings["selector"]["scan_interval_hours"]["options"]
    assert set(options) == {"1", "2", "5", "12", "24"}


@pytest.mark.parametrize("language", ["fr", "en"])
def test_options_translations_are_complete(language: str) -> None:
    """Test French and English options translations are complete."""
    config = _translation_data(language)

    step = config["options"]["step"]["init"]
    assert set(step["data"]) == {CONF_SCAN_INTERVAL}
    options = config["selector"]["scan_interval_hours"]["options"]
    assert set(options) == {"1", "2", "5", "12", "24"}
