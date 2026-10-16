"""Tests for Ocea consumption sensors."""

from __future__ import annotations

import logging
from datetime import date, datetime
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.typing import UNDEFINED
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ocea_smart_building.api import OceaApiError, OceaAuthError
from custom_components.ocea_smart_building.const import DOMAIN
from custom_components.ocea_smart_building.coordinator import (
    OceaDataUpdateCoordinator,
    _build_meter_data,
    _current_month_payload,
)
from custom_components.ocea_smart_building.models import OceaMeter
from custom_components.ocea_smart_building.sensor import SENSOR_TYPES, OceaMeterSensor


def _coordinator(hass: HomeAssistant, client: Mock) -> OceaDataUpdateCoordinator:
    """Create a coordinator bound to an actual config entry."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    coordinator = OceaDataUpdateCoordinator(hass, client, entry)
    assert coordinator.config_entry is entry
    return coordinator


def test_sensor_types_include_cetc_heating_energy_sensor() -> None:
    """Test CETC heating consumption is exposed as an energy sensor."""
    sensors = {description.key: description for description in SENSOR_TYPES}

    assert {"eau_froide", "eau_chaude", "cetc"} <= set(sensors)
    assert sensors["eau_froide"].native_unit_of_measurement == UnitOfVolume.CUBIC_METERS
    assert sensors["eau_chaude"].native_unit_of_measurement == UnitOfVolume.CUBIC_METERS

    cetc = sensors["cetc"]
    assert cetc.data_key == "cetc"
    assert cetc.translation_key == "cetc"
    assert cetc.name is UNDEFINED
    assert cetc.native_unit_of_measurement == UnitOfEnergy.KILO_WATT_HOUR
    assert cetc.device_class is SensorDeviceClass.ENERGY
    assert cetc.state_class is SensorStateClass.TOTAL_INCREASING


def test_leak_sensor_description_is_water_measurement() -> None:
    """Test estimated leaks use the standard water measurement contract."""
    leak = next(description for description in SENSOR_TYPES if description.key == "fuite")

    assert leak.translation_key == "fuite"
    assert leak.native_unit_of_measurement == UnitOfVolume.CUBIC_METERS
    assert leak.device_class is SensorDeviceClass.WATER
    assert leak.state_class is SensorStateClass.MEASUREMENT


def test_meter_data_builder_aggregates_one_meter_response() -> None:
    """Test meter response parsing is isolated from coordinator I/O."""
    meter = OceaMeter.from_api(
        {"id": "pds-cold-1", "fluide": "EauFroide"},
        {"pdsId": "pds-cold-1", "numeroSerie": "SERIAL-1"},
    )

    assert _build_meter_data(
        meter,
        {
            "consommations": [
                {"date": "2026-09-01", "valeur": "0,321"},
                {"date": "2026-09-15", "valeur": 0.654},
            ]
        },
    ).value == 0.975
    assert _build_meter_data(
        meter,
        {"consommations": [{"date": "2026-09-15", "valeur": 0.654}]},
    ).latest_date == "2026-09-15"


def test_current_month_payload_uses_requested_calendar_date() -> None:
    """Test the API payload represents the local calendar month."""
    assert _current_month_payload(date(2026, 9, 16)) == {
        "debut": "2026-09-01T00:00:00.000Z",
        "fin": "2026-09-16T23:59:59.999Z",
        "granularity": "Day",
    }


async def test_coordinator_uses_home_assistant_timezone(
    hass: HomeAssistant,
) -> None:
    """Test current-month boundaries follow Home Assistant's timezone."""
    client = Mock()
    client.get_consumptions.return_value = []
    client.get_pds.return_value = [
        {"id": "pds-cold-1", "fluide": "EauFroide"},
    ]
    client.get_appareils.return_value = []
    client.get_pds_consumption.return_value = {"consommations": []}
    coordinator = _coordinator(hass, client)

    with patch(
        "custom_components.ocea_smart_building.coordinator.dt_util.now",
        return_value=datetime(2026, 9, 16, tzinfo=ZoneInfo("America/Montreal")),
    ):
        await coordinator._async_setup()
        await coordinator._async_update_data()

    payload = client.get_pds_consumption.call_args.args[1]
    assert payload["debut"] == "2026-09-01T00:00:00.000Z"


async def test_coordinator_normalizes_cetc_consumption(hass: HomeAssistant) -> None:
    """Test raw CETC consumption is normalized for the heating sensor."""
    client = Mock()
    client.get_consumptions.return_value = [
        {"fluide": "EauFroide", "valeur": "12,34"},
        {"fluide": "EauChaude", "valeur": "5,67"},
        {"fluide": "CETC", "valeur": "89,01"},
    ]
    client.get_pds.return_value = []
    client.get_appareils.return_value = []
    coordinator = _coordinator(hass, client)

    await coordinator._async_setup()
    data = await coordinator._async_update_data()

    assert data.totals == {
        "eau_froide": 12.34,
        "eau_chaude": 5.67,
        "cetc": 89.01,
    }
    assert data.meters == {}


async def test_coordinator_keeps_consumption_separate_per_pds(
    hass: HomeAssistant,
) -> None:
    """Test two points of the same fluid produce independent meter data."""
    client = Mock()
    client.get_consumptions.return_value = [
        {"fluide": "EauFroide", "valeur": "1,14"},
    ]
    client.get_pds.return_value = [
        {"id": "pds-cold-1", "fluide": "EauFroide", "emplacement": "Cuisine"},
        {"id": "pds-cold-2", "fluide": "EauFroide", "emplacement": "Salle de bain"},
    ]
    client.get_appareils.return_value = [
        {"pdsId": "pds-cold-1", "numeroSerie": "SERIAL-1"},
        {"pdsId": "pds-cold-2", "numeroSerie": "SERIAL-2"},
    ]
    client.get_pds_consumption.side_effect = [
        {"unite": "m3", "consommations": [{"date": "2026-09-15", "valeur": 0.321}]},
        {"unite": "m3", "consommations": [{"date": "2026-09-15", "valeur": 0.654}]},
    ]
    coordinator = _coordinator(hass, client)

    await coordinator._async_setup()
    data = await coordinator._async_update_data()

    assert data.totals["eau_froide"] == 1.14
    assert data.meters["pds-cold-1"].meter.pds.location == "Cuisine"
    assert data.meters["pds-cold-1"].meter.serial_number == "SERIAL-1"
    assert data.meters["pds-cold-1"].value == 0.321
    assert data.meters["pds-cold-2"].meter.pds.location == "Salle de bain"
    assert data.meters["pds-cold-2"].meter.serial_number == "SERIAL-2"
    assert data.meters["pds-cold-2"].value == 0.654


def test_meter_data_builder_keeps_latest_leak_estimate() -> None:
    """Test the latest leak estimate is retained from the daily response."""
    meter = OceaMeter.from_api(
        {"id": "pds-cold-1", "fluide": "EauFroide"},
        None,
    )

    reading = _build_meter_data(
        meter,
        {
            "unite": "m3",
            "consommations": [
                {
                    "date": "2026-09-01",
                    "valeur": 0.1,
                    "fuiteEstimee": 0.0,
                    "consentement": True,
                    "type": "ConsommationEauResponse",
                },
                {
                    "date": "2026-09-15",
                    "valeur": 0.2,
                    "fuiteEstimee": 0.015,
                    "consentement": True,
                    "type": "ConsommationEauResponse",
                },
            ],
        },
    )

    assert reading.value == 0.3
    assert reading.estimated_leak == 0.015
    assert reading.unit == "m3"
    assert reading.latest_date == "2026-09-15"


def test_meter_data_builder_normalizes_string_leak_estimate() -> None:
    """Test string estimates and comma decimals are exposed as numbers."""
    meter = OceaMeter.from_api(
        {"id": "pds-cold-1", "fluide": "EauFroide"},
        None,
    )

    reading = _build_meter_data(
        meter,
        {
            "consommations": [
                {"date": "2026-09-15", "valeur": "0,321", "fuiteEstimee": "0,015"}
            ],
        },
    )

    assert reading.value == 0.321
    assert reading.estimated_leak == 0.015


async def test_coordinator_isolates_malformed_meter_response(
    hass: HomeAssistant,
) -> None:
    """Test malformed meter data does not discard other meter readings or totals."""
    client = Mock()
    client.get_consumptions.return_value = [
        {"fluide": "EauFroide", "valeur": "1,14"},
    ]
    client.get_pds.return_value = [
        {"id": "pds-cold-1", "fluide": "EauFroide"},
        {"id": "pds-cold-2", "fluide": "EauFroide"},
    ]
    client.get_appareils.return_value = []
    client.get_pds_consumption.side_effect = [
        {"consommations": [{"date": "2026-09-15", "valeur": 0.321}]},
        {"consommations": [{"date": "2026-09-15", "valeur": "invalid"}]},
    ]
    coordinator = _coordinator(hass, client)

    await coordinator._async_setup()
    data = await coordinator._async_update_data()

    assert data.totals["eau_froide"] == 1.14
    assert data.meters["pds-cold-1"].value == 0.321
    assert data.meters["pds-cold-2"].value is None


async def test_coordinator_discovers_meters_only_once(
    hass: HomeAssistant,
) -> None:
    """Test PDS metadata is cached while values continue to refresh."""
    client = Mock()
    client.get_consumptions.return_value = []
    client.get_pds.return_value = [
        {"id": "pds-cold-1", "fluide": "EauFroide"},
    ]
    client.get_appareils.return_value = [
        {"pdsId": "pds-cold-1", "numeroSerie": "SERIAL-1"},
    ]
    client.get_pds_consumption.return_value = {
        "consommations": [{"date": "2026-09-15", "valeur": 0.321}],
    }
    coordinator = _coordinator(hass, client)

    await coordinator._async_setup()
    await coordinator._async_update_data()
    await coordinator._async_update_data()

    client.get_pds.assert_called_once_with()
    client.get_appareils.assert_called_once_with(["pds-cold-1"])
    assert client.get_pds_consumption.call_count == 2


async def test_coordinator_keeps_other_meters_when_one_fails(
    hass: HomeAssistant,
) -> None:
    """Test one PDS failure does not discard other meter data or totals."""
    client = Mock()
    client.get_consumptions.return_value = [
        {"fluide": "EauFroide", "valeur": "1,14"},
    ]
    client.get_pds.return_value = [
        {"id": "pds-cold-1", "fluide": "EauFroide"},
        {"id": "pds-cold-2", "fluide": "EauFroide"},
    ]
    client.get_appareils.return_value = []
    client.get_pds_consumption.side_effect = [
        {"consommations": [{"date": "2026-09-15", "valeur": 0.321}]},
        OceaApiError("meter unavailable"),
    ]
    coordinator = _coordinator(hass, client)

    await coordinator._async_setup()
    data = await coordinator._async_update_data()

    assert data.totals["eau_froide"] == 1.14
    assert set(data.meters) == {"pds-cold-1", "pds-cold-2"}
    assert data.meters["pds-cold-2"].value is None


async def test_coordinator_does_not_log_meter_details(
    hass: HomeAssistant,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test coordinator logs do not contain PDS or serial identifiers."""
    client = Mock()
    client.get_consumptions.return_value = []
    client.get_pds.return_value = [
        {"id": "pds-secret", "fluide": "EauFroide"},
    ]
    client.get_appareils.return_value = [
        {"pdsId": "pds-secret", "numeroSerie": "serial-secret"},
    ]
    client.get_pds_consumption.return_value = {"consommations": []}
    coordinator = _coordinator(hass, client)

    with caplog.at_level(logging.DEBUG):
        await coordinator._async_setup()
        await coordinator._async_update_data()

    assert "pds-secret" not in caplog.text
    assert "serial-secret" not in caplog.text


def test_meter_sensor_uses_pds_id_and_serial_for_identity() -> None:
    """Test individual meters get stable identities and readable names."""
    coordinator = Mock()
    description = next(
        description
        for description in SENSOR_TYPES
        if description.key == "eau_froide"
    )
    sensor = OceaMeterSensor(
        coordinator=coordinator,
        description=description,
        local_id="local-123",
        meter=OceaMeter.from_api(
            {
                "id": "pds-cold-1",
                "fluide": "EauFroide",
                "emplacement": "Cuisine",
            },
            {"pdsId": "pds-cold-1", "numeroSerie": "SERIAL-1"},
        ),
    )

    assert sensor.unique_id == (
        "ocea_smart_building_local-123_pds-cold-1_eau_froide"
    )
    assert sensor.has_entity_name is True
    assert sensor.entity_description.translation_key == "eau_froide"
    assert sensor.entity_description.name is UNDEFINED
    assert not hasattr(sensor, "_attr_name")
    assert sensor.device_info["identifiers"] == {
        ("ocea_smart_building", "local-123_pds-cold-1")
    }
    assert sensor.device_info["translation_key"] == "meter"
    assert sensor.device_info["translation_placeholders"] == {
        "meter_identity": "SERIAL-1 (PDS pds-cold-1)"
    }
    assert sensor.device_info["suggested_area"] == "Cuisine"


async def test_coordinator_setup_maps_auth_error_to_reauth(
    hass: HomeAssistant,
) -> None:
    """Test a rejected discovery requests reauthentication."""
    client = Mock()
    client.get_pds.side_effect = OceaAuthError("rejected")
    coordinator = _coordinator(hass, client)

    with pytest.raises(ConfigEntryAuthFailed):
        await coordinator._async_setup()
