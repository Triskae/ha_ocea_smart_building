"""Tests for OCEA leak binary sensors."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import Mock

from homeassistant.components.binary_sensor import BinarySensorDeviceClass
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.ocea_smart_building.binary_sensor import OCEALeakBinarySensor
from custom_components.ocea_smart_building.const import CONF_LOCAL_ID, DOMAIN
from custom_components.ocea_smart_building.coordinator import (
    OCEAData,
    OCEADataUpdateCoordinator,
    OCEAMeterReading,
)
from custom_components.ocea_smart_building.ocea import OCEAMeter


def _sensor(
    hass: HomeAssistant,
    estimated_leak: Decimal | None,
) -> OCEALeakBinarySensor:
    """Create a leak sensor with one meter reading."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCAL_ID: "local-123"})
    entry.add_to_hass(hass)
    coordinator = OCEADataUpdateCoordinator(hass, Mock(), entry)
    meter = OCEAMeter.from_api(
        {"id": "pds-cold-1", "fluide": "EauFroide"},
        {"pdsId": "pds-cold-1", "numeroSerie": "SERIAL-1"},
    )
    coordinator.data = OCEAData(
        totals={},
        meters={
            meter.identifier: OCEAMeterReading(
                meter=meter,
                value=Decimal(1),
                latest_date=date(2026, 9, 15),
                estimated_leak=estimated_leak,
            )
        },
    )
    return OCEALeakBinarySensor(coordinator, "local-123", meter)


async def test_leak_binary_sensor_is_on_for_positive_estimate(
    hass: HomeAssistant,
) -> None:
    """Test a positive estimate turns the leak sensor on."""
    sensor = _sensor(hass, Decimal("0.015"))

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
    sensor = _sensor(hass, Decimal(0))

    assert sensor.is_on is False
    assert sensor.available is True


async def test_leak_binary_sensor_is_unknown_without_estimate(
    hass: HomeAssistant,
) -> None:
    """Test a missing estimate does not falsely report no leak."""
    sensor = _sensor(hass, None)

    assert sensor.is_on is None
    assert sensor.available is True
