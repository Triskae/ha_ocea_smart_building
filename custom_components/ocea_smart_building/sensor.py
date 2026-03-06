"""Sensor platform for Ocea Smart Building water consumption."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_LOCAL_ID, DOMAIN
from .coordinator import OceaDataUpdateCoordinator


@dataclass(frozen=True, kw_only=True)
class OceaSensorEntityDescription(SensorEntityDescription):
    """Describe an Ocea sensor entity."""

    data_key: str


SENSOR_TYPES: tuple[OceaSensorEntityDescription, ...] = (
    OceaSensorEntityDescription(
        key="eau_froide",
        data_key="eau_froide",
        translation_key="eau_froide",
        name="Eau froide",
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        device_class=SensorDeviceClass.WATER,
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:water",
        suggested_display_precision=2,
    ),
    OceaSensorEntityDescription(
        key="eau_chaude",
        data_key="eau_chaude",
        translation_key="eau_chaude",
        name="Eau chaude",
        native_unit_of_measurement=UnitOfVolume.CUBIC_METERS,
        device_class=SensorDeviceClass.WATER,
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:water-thermometer",
        suggested_display_precision=2,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Ocea Smart Building sensors from a config entry."""
    coordinator: OceaDataUpdateCoordinator = hass.data[DOMAIN][config_entry.entry_id]
    local_id = config_entry.data[CONF_LOCAL_ID]

    entities: list[OceaWaterSensor] = []
    for description in SENSOR_TYPES:
        if coordinator.data and description.data_key in coordinator.data:
            entities.append(
                OceaWaterSensor(
                    coordinator=coordinator,
                    description=description,
                    local_id=local_id,
                )
            )

    async_add_entities(entities, update_before_add=True)


class OceaWaterSensor(
    CoordinatorEntity[OceaDataUpdateCoordinator], SensorEntity
):
    """Representation of an Ocea water consumption sensor."""

    entity_description: OceaSensorEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: OceaDataUpdateCoordinator,
        description: OceaSensorEntityDescription,
        local_id: str,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._local_id = local_id
        self._attr_unique_id = f"{DOMAIN}_{local_id}_{description.key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, local_id)},
            "name": f"Ocea - Local {local_id}",
            "manufacturer": "Ocea Smart Building",
            "model": "Espace Résident",
        }

    @property
    def native_value(self) -> float | None:
        """Return the sensor value."""
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get(self.entity_description.data_key)
