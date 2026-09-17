"""Tests for the Ocea Smart Building diagnostics platform."""

from __future__ import annotations

from unittest.mock import Mock

from homeassistant.components.diagnostics import REDACTED
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ocea_smart_building.const import CONF_LOCAL_ID, DOMAIN
from custom_components.ocea_smart_building.diagnostics import (
    async_get_config_entry_diagnostics,
)

EMAIL = "resident@example.com"
PASSWORD = "stored-secret"
LOCAL_ID = "local-123"


async def test_diagnostics_redacts_credentials_and_dwelling_id(
    hass: HomeAssistant,
) -> None:
    """Test email, password, and dwelling identifier are redacted."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_EMAIL: EMAIL,
            CONF_PASSWORD: PASSWORD,
            CONF_LOCAL_ID: LOCAL_ID,
        },
    )
    entry.add_to_hass(hass)
    coordinator = Mock(
        data={"eau_froide": 12.34, "eau_chaude": 5.67},
        last_update_success=True,
    )
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    assert diagnostics["entry_data"] == {
        CONF_EMAIL: REDACTED,
        CONF_PASSWORD: REDACTED,
        CONF_LOCAL_ID: REDACTED,
    }
    assert diagnostics["last_update_success"] is True
    assert diagnostics["data"] == {"eau_froide": 12.34, "eau_chaude": 5.67}
