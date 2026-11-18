"""Tests for the Ocea Smart Building system health platform."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ocea_smart_building.const import CONF_LOCAL_ID, DOMAIN
from custom_components.ocea_smart_building.coordinator import (
    OceaData,
    OceaMeterReading,
)
from custom_components.ocea_smart_building.ocea import OceaMeter, OceaPds
from custom_components.ocea_smart_building.system_health import (
    async_register,
    system_health_info,
)


def test_system_health_info_labels_are_translated() -> None:
    """Test every system health field has source and localized labels."""
    integration_path = Path(__file__).parents[1] / "custom_components" / DOMAIN
    expected_keys = {
        "can_reach_server",
        "last_update_success",
        "meter_count",
        "meter_readings_available",
        "meter_readings_unavailable",
    }

    for path in (
        integration_path / "strings.json",
        integration_path / "translations" / "en.json",
        integration_path / "translations" / "fr.json",
    ):
        with path.open(encoding="utf-8") as translation_file:
            data = json.load(translation_file)
        assert set(data["system_health"]["info"]) == expected_keys


def test_async_register_registers_info_callback() -> None:
    """Test the info callback is registered with system health."""
    register = Mock()

    async_register(Mock(), register)

    register.async_register_info.assert_called_once_with(system_health_info)


async def test_system_health_info_without_loaded_entry(hass: HomeAssistant) -> None:
    """Test the info dict omits update status when no entry is loaded."""
    with (
        patch.object(hass.config_entries, "async_loaded_entries", return_value=[]),
        patch(
            "custom_components.ocea_smart_building.system_health.system_health"
            ".async_check_can_reach_url",
            return_value="ok",
        ) as mock_check,
    ):
        info = await system_health_info(hass)

    assert set(info) == {"can_reach_server"}
    assert await info["can_reach_server"] == "ok"
    mock_check.assert_called_once()


async def test_system_health_info_reports_last_update_success(
    hass: HomeAssistant,
) -> None:
    """Test the info dict reflects the loaded coordinator's update status."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    coordinator = Mock(last_update_success=True, data=None)
    entry.runtime_data = coordinator

    with (
        patch.object(
            hass.config_entries, "async_loaded_entries", return_value=[entry]
        ),
        patch(
            "custom_components.ocea_smart_building.system_health.system_health"
            ".async_check_can_reach_url",
            return_value="ok",
        ),
    ):
        info = await system_health_info(hass)

    assert info["last_update_success"] is True
    assert await info["can_reach_server"] == "ok"


async def test_system_health_info_reports_available_meter_readings(
    hass: HomeAssistant,
) -> None:
    """Test zero-valued meter readings are included as available."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    meter = OceaMeter(pds=OceaPds(id="meter-1", fluid="EauFroide"))
    coordinator = Mock(
        last_update_success=True,
        data=OceaData(
            totals={},
            meters={
                meter.identifier: OceaMeterReading(
                    meter=meter, value=0.0, latest_date="2026-11-08"
                )
            },
        ),
    )
    entry.runtime_data = coordinator

    with (
        patch.object(
            hass.config_entries, "async_loaded_entries", return_value=[entry]
        ),
        patch(
            "custom_components.ocea_smart_building.system_health.system_health"
            ".async_check_can_reach_url",
            return_value="ok",
        ),
    ):
        info = await system_health_info(hass)

    assert info["meter_count"] == 1
    assert info["meter_readings_available"] == 1
    assert info["meter_readings_unavailable"] == 0
    assert await info["can_reach_server"] == "ok"


async def test_system_health_info_reports_partial_meter_readings(
    hass: HomeAssistant,
) -> None:
    """Test unavailable meter readings are counted without exposing identities."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    available_meter = OceaMeter(pds=OceaPds(id="meter-1", fluid="EauFroide"))
    unavailable_meter = OceaMeter(pds=OceaPds(id="meter-2", fluid="EauFroide"))
    coordinator = Mock(
        last_update_success=True,
        data=OceaData(
            totals={},
            meters={
                available_meter.identifier: OceaMeterReading(
                    meter=available_meter, value=0.321, latest_date="2026-11-08"
                ),
                unavailable_meter.identifier: OceaMeterReading(
                    meter=unavailable_meter, value=None, latest_date=None
                ),
            },
        ),
    )
    entry.runtime_data = coordinator

    with (
        patch.object(
            hass.config_entries, "async_loaded_entries", return_value=[entry]
        ),
        patch(
            "custom_components.ocea_smart_building.system_health.system_health"
            ".async_check_can_reach_url",
            return_value="ok",
        ),
    ):
        info = await system_health_info(hass)

    assert info["meter_count"] == 2
    assert info["meter_readings_available"] == 1
    assert info["meter_readings_unavailable"] == 1
    assert not any("meter-" in key for key in info)
    assert await info["can_reach_server"] == "ok"


async def test_system_health_info_aggregates_all_loaded_entries(
    hass: HomeAssistant,
) -> None:
    """Test health status and meter readings include each loaded entry."""
    first_entry = MockConfigEntry(domain=DOMAIN)
    first_entry.add_to_hass(hass)
    second_entry = MockConfigEntry(domain=DOMAIN)
    second_entry.add_to_hass(hass)

    first_meter = OceaMeter(pds=OceaPds(id="meter-1", fluid="EauFroide"))
    second_meter = OceaMeter(pds=OceaPds(id="meter-2", fluid="EauFroide"))
    first_entry.runtime_data = Mock(
        last_update_success=True,
        data=OceaData(
            totals={},
            meters={
                first_meter.identifier: OceaMeterReading(
                    meter=first_meter, value=0.321, latest_date="2026-11-08"
                )
            },
        ),
    )
    second_entry.runtime_data = Mock(
        last_update_success=False,
        data=OceaData(
            totals={},
            meters={
                second_meter.identifier: OceaMeterReading(
                    meter=second_meter, value=0.25, latest_date="2026-11-08"
                )
            },
        ),
    )

    with (
        patch.object(
            hass.config_entries,
            "async_loaded_entries",
            return_value=[first_entry, second_entry],
        ),
        patch(
            "custom_components.ocea_smart_building.system_health.system_health"
            ".async_check_can_reach_url",
            return_value="ok",
        ),
    ):
        info = await system_health_info(hass)

    assert info["last_update_success"] is False
    assert info["meter_count"] == 2
    assert info["meter_readings_available"] == 1
    assert info["meter_readings_unavailable"] == 1
    assert await info["can_reach_server"] == "ok"


@pytest.mark.usefixtures("enable_custom_integrations")
async def test_system_health_tracks_real_entry_lifecycle(hass: HomeAssistant) -> None:
    """Health metrics follow loaded entries and exclude them after unloading."""
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
        patch("custom_components.ocea_smart_building.OceaApiClient", autospec=True) as client,
        patch(
            "custom_components.ocea_smart_building.system_health.system_health"
            ".async_check_can_reach_url",
            return_value="ok",
        ),
    ):
        client.return_value.get_pds.return_value = [
            {"id": "pds-1", "fluide": "EauFroide"}
        ]
        client.return_value.get_appareils.return_value = []
        client.return_value.get_consumptions.return_value = []
        client.return_value.get_pds_consumption.return_value = {"consommations": []}

        assert await hass.config_entries.async_setup(entry.entry_id)
        info = await system_health_info(hass)
        assert await info.pop("can_reach_server") == "ok"
        assert info == {
            "last_update_success": True,
            "meter_count": 1,
            "meter_readings_available": 1,
            "meter_readings_unavailable": 0,
        }

        assert await hass.config_entries.async_unload(entry.entry_id)
        info = await system_health_info(hass)
        assert set(info) == {"can_reach_server"}
        assert await info["can_reach_server"] == "ok"
