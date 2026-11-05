"""Tests for integration setup lifecycle."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ocea_smart_building import async_setup_entry
from custom_components.ocea_smart_building.const import CONF_LOCAL_ID, DOMAIN


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_setup_closes_client_when_first_refresh_fails(
    hass: HomeAssistant,
) -> None:
    """Test a failed initial refresh does not leak the HTTP session."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_EMAIL: "resident@example.com",
            CONF_PASSWORD: "secret",
            CONF_LOCAL_ID: "local-123",
        },
    )
    entry.add_to_hass(hass)

    with (
        patch("custom_components.ocea_smart_building.OceaApiClient") as client_class,
        patch(
            "custom_components.ocea_smart_building.OceaDataUpdateCoordinator"
        ) as coordinator_class,
    ):
        coordinator_class.return_value.async_config_entry_first_refresh.side_effect = (
            RuntimeError("refresh failed")
        )

        with pytest.raises(RuntimeError, match="refresh failed"):
            await async_setup_entry(hass, entry)

    client_class.return_value.close.assert_called_once_with()
