"""Tests for the Ocea Smart Building diagnostics platform."""

from __future__ import annotations

from unittest.mock import Mock

from homeassistant.components.diagnostics import REDACTED
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ocea_smart_building.const import CONF_LOCAL_ID, DOMAIN
from custom_components.ocea_smart_building.coordinator import (
    OceaData,
    OceaMeterReading,
)
from custom_components.ocea_smart_building.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.ocea_smart_building.ocea import OceaMeter

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
            "future_token": "private-value",
        },
    )
    entry.add_to_hass(hass)
    cold_meter = OceaMeter.from_api(
        {
            "id": "pds-cold-1",
            "fluide": "EauFroide",
            "code": "cold-code",
            "emplacement": "Cuisine",
            "typeReleve": "Tele",
            "fce": 1.0,
        },
        {
            "id": "device-cold-1",
            "numeroSerie": "serial-cold-1",
            "datePose": "2023-07-07T00:00:00Z",
            "fc": 1.25,
        },
    )
    hot_meter = OceaMeter.from_api(
        {"id": "pds-hot-1", "fluide": "EauChaude"},
        {"id": "device-hot-1", "numeroSerie": "serial-hot-1"},
    )
    coordinator = Mock(
        data=OceaData(
            totals={"eau_froide": 15.0, "eau_chaude": 7.5, "unexpected": 3.0},
            meters={
                cold_meter.identifier: OceaMeterReading(
                    meter=cold_meter,
                    value=1.23,
                    latest_date="2026-09-15",
                    unit="m3",
                    estimated_leak=0.02,
                    consent=True,
                    response_type="ConsommationEauResponse",
                ),
                hot_meter.identifier: OceaMeterReading(
                    meter=hot_meter,
                    value=0.45,
                    latest_date="2026-09-14",
                    unit="m3",
                ),
            },
        ),
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
    assert diagnostics["data"] == {
        "totals": {"eau_froide": 15.0, "eau_chaude": 7.5},
        "meters": [
            {
                "meter": {
                    "pds_id": REDACTED,
                    "fluid": "EauFroide",
                    "code": REDACTED,
                    "location": REDACTED,
                    "reading_type": "Tele",
                    "fce": 1.0,
                    "device_id": REDACTED,
                    "serial_number": REDACTED,
                    "installation_date": "2023-07-07T00:00:00Z",
                    "correction_factor": 1.25,
                },
                "value": 1.23,
                "latest_date": "2026-09-15",
                "unit": "m3",
                "estimated_leak": 0.02,
                "consent": True,
                "response_type": "ConsommationEauResponse",
            },
            {
                "meter": {
                    "pds_id": REDACTED,
                    "fluid": "EauChaude",
                    "code": None,
                    "location": None,
                    "reading_type": None,
                    "fce": None,
                    "device_id": REDACTED,
                    "serial_number": REDACTED,
                    "installation_date": None,
                    "correction_factor": None,
                },
                "value": 0.45,
                "latest_date": "2026-09-14",
                "unit": "m3",
                "estimated_leak": None,
                "consent": None,
                "response_type": None,
            },
        ],
    }


async def test_diagnostics_without_coordinator_data(hass: HomeAssistant) -> None:
    """Diagnostics remain available before the first successful update."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_EMAIL: EMAIL})
    entry.add_to_hass(hass)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = Mock(
        data=None, last_update_success=False
    )

    assert await async_get_config_entry_diagnostics(hass, entry) == {
        "entry_data": {CONF_EMAIL: REDACTED},
        "last_update_success": False,
        "data": None,
    }
