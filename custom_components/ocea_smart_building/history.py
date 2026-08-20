"""One-time import of Ocea daily history into Home Assistant Recorder."""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

import homeassistant.util.dt as dt_util
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import async_import_statistics
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .api import OceaApiClient
from .const import CONF_LOCAL_ID, DOMAIN, HISTORY_START

_LOGGER = logging.getLogger(__name__)
_TIME_ZONE = ZoneInfo("Europe/Paris")

# API fluid, precise sensor key, Recorder unit and converter class.
_HISTORY_SENSORS = (
    ("EauFroide", "eau_froide_precise", "m³", "volume"),
    ("EauChaude", "eau_chaude_precise", "m³", "volume"),
    ("Cetc", "cetc_precise", "kWh", "energy"),
)


def _day_start_utc(day_iso: str) -> datetime:
    """Convert an Ocea calendar day to its local midnight in UTC."""
    year, month, day = (int(part) for part in day_iso.split("-"))
    return datetime(year, month, day, tzinfo=_TIME_ZONE).astimezone(dt_util.UTC)


async def async_import_history(
    hass: HomeAssistant, entry: ConfigEntry, client: OceaApiClient
) -> int:
    """Import all available daily values and return the inserted row count.

    Each sensor state is the running total for its calendar month, matching the
    live precise sensor. Recorder's sum remains continuous across month resets.
    Existing rows are replaced by statistic ID, so a retry is idempotent.
    """
    history = await hass.async_add_executor_job(
        client.get_daily_history, HISTORY_START
    )
    registry = er.async_get(hass)
    local_id = entry.data[CONF_LOCAL_ID]
    total_rows = 0

    for fluid, key, unit, unit_class in _HISTORY_SENSORS:
        entity_id = registry.async_get_entity_id(
            "sensor", DOMAIN, f"{DOMAIN}_{local_id}_{key}"
        )
        if entity_id is None:
            _LOGGER.warning("History sensor %s is not registered", key)
            continue

        rows: list[StatisticData] = []
        cumulative = Decimal(0)
        month_total = Decimal(0)
        current_month = ""
        for day, raw_value in history.get(fluid, []):
            try:
                value = Decimal(raw_value)
            except (InvalidOperation, TypeError, ValueError):
                continue
            if day[:7] != current_month:
                current_month = day[:7]
                month_total = Decimal(0)
            month_total += value
            cumulative += value
            rows.append(
                StatisticData(
                    start=_day_start_utc(day),
                    state=float(month_total),
                    sum=float(cumulative),
                )
            )

        if not rows:
            continue
        metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=None,
            source="recorder",
            statistic_id=entity_id,
            unit_of_measurement=unit,
            unit_class=unit_class,
        )
        async_import_statistics(hass, metadata, rows)
        total_rows += len(rows)

    return total_rows
