"""Shared entity helpers for Ocea Smart Building."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import OceaDataUpdateCoordinator
from .ocea import OceaMeter


def meter_device_info(local_id: str, meter: OceaMeter) -> DeviceInfo:
    """Build the Home Assistant device descriptor for one meter."""
    identity = f"PDS {meter.identifier}"
    if meter.serial_number:
        identity = f"{meter.serial_number} ({identity})"
    return DeviceInfo(
        identifiers={(DOMAIN, f"{local_id}_{meter.identifier}")},
        manufacturer="Ocea Smart Building",
        serial_number=meter.serial_number,
        suggested_area=meter.pds.location,
        translation_key="meter",
        translation_placeholders={"meter_identity": identity},
    )


def meter_entity_unique_id(local_id: str, meter: OceaMeter, key: str) -> str:
    """Build the established unique ID for an individual meter entity."""
    return f"{DOMAIN}_{local_id}_{meter.identifier}_{key}"


class OceaEntity(CoordinatorEntity[OceaDataUpdateCoordinator]):
    """Base class for Ocea coordinator entities."""

    _attr_has_entity_name = True
