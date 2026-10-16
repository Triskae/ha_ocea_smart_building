"""Tests for Ocea leak binary sensors."""

from __future__ import annotations

from unittest.mock import Mock

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ocea_smart_building.binary_sensor import OceaLeakBinarySensor
from custom_components.ocea_smart_building.const import DOMAIN
from custom_components.ocea_smart_building.coordinator import OceaDataUpdateCoordinator
from custom_components.ocea_smart_building.models import (
    OceaData,
    OceaMeter,
    OceaMeterReading,
)


def _sensor(
    hass: HomeAssistant,
    estimated_leak: float | None,
) -> OceaLeakBinarySensor:
    """Create a leak sensor with one meter reading."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    coordinator = OceaDataUpdateCoordinator(hass, Mock(), entry)
    meter = OceaMeter.from_api(
        {"id": "pds-cold-1", "fluide": "EauFroide"},
        {"pdsId": "pds-cold-1", "numeroSerie": "SERIAL-1"},
    )
    coordinator.data = OceaData(
        totals={},
        meters={
            meter.identifier: OceaMeterReading(
                meter=meter,
                value=1.0,
                latest_date="2026-09-15",
                estimated_leak=estimated_leak,
            )
        },
    )
    return OceaLeakBinarySensor(coordinator, "local-123", meter)


async def test_leak_binary_sensor_is_on_for_positive_estimate(
    hass: HomeAssistant,
) -> None:
    """Test a positive estimate turns the leak sensor on."""
    sensor = _sensor(hass, 0.015)

    assert sensor.device_class is BinarySensorDeviceClass.MOISTURE
    assert sensor.is_on is True
    assert sensor.available is True
    assert sensor.unique_id == (
        "ocea_smart_building_local-123_pds-cold-1_fuite_detectee"
    )


async def test_leak_binary_sensor_is_off_for_zero_estimate(
    hass: HomeAssistant,
) -> None:
    """Test the nominal zero estimate turns the leak sensor off."""
    sensor = _sensor(hass, 0.0)

    assert sensor.is_on is False
    assert sensor.available is True


async def test_leak_binary_sensor_is_unavailable_without_estimate(
    hass: HomeAssistant,
) -> None:
    """Test a missing estimate does not falsely report no leak."""
    sensor = _sensor(hass, None)

    assert sensor.is_on is None
    assert sensor.available is False
