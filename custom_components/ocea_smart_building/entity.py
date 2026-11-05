"""Shared entity helpers for Ocea Smart Building."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import OceaDataUpdateCoordinator
from .models import OceaMeter


def local_device_info(local_id: str) -> DeviceInfo:
    """Build the Home Assistant device descriptor for dwelling totals."""
    return DeviceInfo(
        identifiers={(DOMAIN, local_id)},
        manufacturer="Ocea Smart Building",
        translation_key="local",
        translation_placeholders={"local_id": local_id},
    )


def meter_device_info(local_id: str, meter: OceaMeter) -> DeviceInfo:
    """Build the Home Assistant device descriptor for one meter."""
    return DeviceInfo(
        identifiers={(DOMAIN, f"{local_id}_{meter.identifier}")},
        manufacturer="Ocea Smart Building",
        serial_number=meter.serial_number,
        suggested_area=meter.pds.location,
        translation_key="meter",
        translation_placeholders={"meter_identity": meter.display_name},
    )


def local_entity_unique_id(local_id: str, key: str) -> str:
    """Build the established unique ID for a dwelling-level sensor."""
    return f"{DOMAIN}_{local_id}_{key}"


def meter_entity_unique_id(local_id: str, meter: OceaMeter, key: str) -> str:
    """Build the established unique ID for an individual meter entity."""
    return f"{DOMAIN}_{local_id}_{meter.identifier}_{key}"


class OceaEntity(CoordinatorEntity[OceaDataUpdateCoordinator]):
    """Base class for Ocea coordinator entities."""

    _attr_has_entity_name = True
