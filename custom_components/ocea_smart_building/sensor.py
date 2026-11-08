"""Sensor platform for OCEA Smart Building water consumption."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_LOCAL_ID, PDS_FLUID_METADATA
from .coordinator import OCEAConfigEntry, OCEADataUpdateCoordinator
from .entity import (
    OCEAEntity,
    meter_device_info,
    meter_entity_unique_id,
)
from .ocea import OCEAMeter

PARALLEL_UPDATES = 0


SENSOR_TYPES: tuple[SensorEntityDescription, ...] = (
    SensorEntityDescription(
        key="fuite",
        translation_key="fuite",
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        device_class=SensorDeviceClass.WATER,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:pipe-leak",
        suggested_display_precision=3,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: OCEAConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up OCEA Smart Building sensors from a config entry."""
    coordinator = config_entry.runtime_data
    local_id = config_entry.data[CONF_LOCAL_ID]
    descriptions = {description.key: description for description in SENSOR_TYPES}

    entities: list[OCEAMeterSensor] = []
    if coordinator.data:
        for meter in coordinator.data.meters.values():
            if (
                PDS_FLUID_METADATA[meter.meter.pds.fluid].supports_water_leak_sensors
                and meter.estimated_leak is not None
            ):
                entities.append(
                    OCEAMeterSensor(
                        coordinator=coordinator,
                        description=descriptions["fuite"],
                        local_id=local_id,
                        meter=meter.meter,
                    )
                )

    async_add_entities(entities, update_before_add=True)

    leak_entity_keys = {
        reading.meter.identifier
        for reading in (coordinator.data.meters.values() if coordinator.data else ())
        if PDS_FLUID_METADATA[reading.meter.pds.fluid].supports_water_leak_sensors
        and reading.estimated_leak is not None
    }

    @callback
    def _async_add_new_leak_entities() -> None:
        """Add leak entities when OCEA starts reporting an estimate."""
        new_entities: list[OCEAMeterSensor] = []
        if coordinator.data is None:
            return
        for reading in coordinator.data.meters.values():
            if (
                not PDS_FLUID_METADATA[
                    reading.meter.pds.fluid
                ].supports_water_leak_sensors
                or reading.estimated_leak is None
            ):
                continue
            meter = reading.meter
            if meter.identifier in leak_entity_keys:
                continue
            leak_entity_keys.add(meter.identifier)
            new_entities.append(
                OCEAMeterSensor(
                    coordinator=coordinator,
                    description=descriptions["fuite"],
                    local_id=local_id,
                    meter=meter,
                )
            )
        if new_entities:
            async_add_entities(new_entities, update_before_add=True)

    config_entry.async_on_unload(
        coordinator.async_add_listener(_async_add_new_leak_entities)
    )


class OCEAMeterSensor(OCEAEntity, SensorEntity):
    """Representation of an estimated leak for one water meter."""

    entity_description: SensorEntityDescription

    def __init__(
        self,
        coordinator: OCEADataUpdateCoordinator,
        description: SensorEntityDescription,
        local_id: str,
        meter: OCEAMeter,
    ) -> None:
        """Initialize an individual meter sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._meter = meter
        self._attr_unique_id = meter_entity_unique_id(
            local_id, meter, description.key
        )
        self._attr_device_info = meter_device_info(local_id, meter)

    @property
    def native_value(self) -> float | None:
        """Return this meter's estimated leak."""
        if self.coordinator.data is None:
            return None
        reading = self.coordinator.data.meters.get(self._meter.identifier)
        if reading is None:
            return None
        value = reading.estimated_leak
        return float(value) if value is not None else None
