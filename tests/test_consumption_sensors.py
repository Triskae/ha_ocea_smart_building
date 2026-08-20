"""Tests for Ocea consumption sensors."""

from __future__ import annotations

from unittest.mock import Mock

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import UnitOfEnergy, UnitOfVolume
from homeassistant.core import HomeAssistant

from custom_components.ocea_smart_building.coordinator import OceaDataUpdateCoordinator
from custom_components.ocea_smart_building.sensor import SENSOR_TYPES


def test_sensor_types_include_cetc_heating_energy_sensor() -> None:
    """Test CETC heating consumption is exposed as an energy sensor."""
    sensors = {description.key: description for description in SENSOR_TYPES}

    assert set(sensors) == {
        "eau_froide",
        "eau_chaude",
        "cetc",
        "eau_froide_precise",
        "eau_chaude_precise",
        "cetc_precise",
        "fuite_eau_froide",
        "fuite_eau_chaude",
    }
    assert sensors["eau_froide"].native_unit_of_measurement == UnitOfVolume.CUBIC_METERS
    assert sensors["eau_chaude"].native_unit_of_measurement == UnitOfVolume.CUBIC_METERS

    cetc = sensors["cetc"]
    assert cetc.data_key == "cetc"
    assert cetc.translation_key == "cetc"
    assert cetc.name == "Chauffage thermique"
    assert cetc.native_unit_of_measurement == UnitOfEnergy.KILO_WATT_HOUR
    assert cetc.device_class is SensorDeviceClass.ENERGY
    assert cetc.state_class is SensorStateClass.TOTAL_INCREASING


async def test_coordinator_normalizes_cetc_consumption(hass: HomeAssistant) -> None:
    """Test raw CETC consumption is normalized for the heating sensor."""
    client = Mock()
    client.get_consumptions.return_value = [
        {"fluide": "EauFroide", "valeur": "12,34"},
        {"fluide": "EauChaude", "valeur": "5,67"},
        {"fluide": "CETC", "valeur": "89,01"},
    ]
    client.get_precise_consumptions.return_value = []
    coordinator = OceaDataUpdateCoordinator(hass, client)

    data = await coordinator._async_update_data()

    assert data == {
        "eau_froide": 12.34,
        "eau_chaude": 5.67,
        "cetc": 89.01,
    }


async def test_coordinator_exposes_precise_heating_and_leaks(
    hass: HomeAssistant,
) -> None:
    """Test detailed Ocea values are normalized for their sensors."""
    client = Mock()
    client.get_consumptions.return_value = []
    client.get_precise_consumptions.return_value = [
        {"fluide": "Cetc", "valeur": "42.125", "precise": True},
        {
            "fluide": "EauFroide",
            "valeur": "0.015",
            "leak_estimate": True,
            "date": "2026-08-19T00:00:00Z",
        },
    ]
    coordinator = OceaDataUpdateCoordinator(hass, client)

    data = await coordinator._async_update_data()

    assert data["cetc_precise"] == 42.125
    assert data["fuite_eau_froide"] == 0.015
    assert data["fuite_eau_froide_date"] == "2026-08-19T00:00:00Z"
