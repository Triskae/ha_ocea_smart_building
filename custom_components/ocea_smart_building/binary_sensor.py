"""Binary sensor platform for OCEA Smart Building leak detection."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_LOCAL_ID, PDS_FLUID_METADATA
from .coordinator import OCEAConfigEntry, OCEADataUpdateCoordinator
from .entity import OCEAMeterEntity, meter_device_info, meter_entity_unique_id
from .ocea import OCEAMeter

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: OCEAConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up leak binary sensors for discovered water meters."""
    coordinator = config_entry.runtime_data
    local_id = config_entry.data[CONF_LOCAL_ID]
    readings = coordinator.data.meters.values() if coordinator.data else ()

    async_add_entities(
        OCEALeakBinarySensor(coordinator, local_id, reading.meter)
        for reading in readings
        if PDS_FLUID_METADATA[reading.meter.pds.fluid].supports_water_leak_sensors
    )


class OCEALeakBinarySensor(OCEAMeterEntity, BinarySensorEntity):
    """Represent whether OCEA estimates a leak for one water meter."""

    _attr_translation_key = "fuite"
    _attr_device_class = BinarySensorDeviceClass.MOISTURE

    def __init__(
        self,
        coordinator: OCEADataUpdateCoordinator,
        local_id: str,
        meter: OCEAMeter,
    ) -> None:
        """Initialize a leak binary sensor."""
        super().__init__(coordinator, meter)
        self._attr_unique_id = meter_entity_unique_id(
            local_id, meter, "fuite_detectee"
        )
        self._attr_device_info = meter_device_info(local_id, meter)

    @property
    def is_on(self) -> bool | None:
        """Return whether the meter reports an estimated leak."""
        reading = self._reading
        if reading is None or reading.estimated_leak is None:
            return None
        return reading.estimated_leak > 0
