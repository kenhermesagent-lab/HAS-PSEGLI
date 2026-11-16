"""Import PSEG usage and cost into Home Assistant long-term statistics."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    statistics_during_period,
)
from homeassistant.core import HomeAssistant

from .const import COST_STATISTIC_ID, DOMAIN, NAME, USAGE_STATISTIC_ID
from .parser import Tariff, UsagePoint

_LOGGER = logging.getLogger(__name__)

_LOOKBACK = timedelta(days=45)


async def _async_last_state(
    hass: HomeAssistant, statistic_id: str, before: datetime
) -> tuple[datetime | None, float]:
    """Return the last recorded (start, cumulative sum) before a timestamp."""
    try:
        instance = get_instance(hass)
    except KeyError:
        _LOGGER.debug("Recorder is not available yet")
        return None, 0.0

    try:
        rows = await instance.async_add_executor_job(
            statistics_during_period,
            hass,
            before - _LOOKBACK,
            before,
            [statistic_id],
            "hour",
            None,
            {"start", "sum"},
        )
    except Exception:  # noqa: BLE001 - recorder internals vary between versions
        _LOGGER.debug("Could not read statistics for %s", statistic_id, exc_info=True)
        return None, 0.0

    entries = rows.get(statistic_id) or []
    for entry in reversed(entries):
        if entry.get("sum") is not None:
            start = entry["start"]
            if isinstance(start, datetime) and start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            return start, float(entry["sum"])
    return None, 0.0


async def async_has_statistics(hass: HomeAssistant, statistic_id: str) -> bool:
    """Return True when the statistic already has rows."""
    start, _ = await _async_last_state(
        hass, statistic_id, datetime.now(timezone.utc)
    )
    return start is not None


def _metadata(statistic_id: str, name: str, unit: str) -> dict[str, Any]:
    return {
        "statistic_id": statistic_id,
        "source": DOMAIN,
        "name": name,
        "unit_of_measurement": unit,
        "has_mean": False,
        "has_sum": True,
    }


async def async_import_usage(
    hass: HomeAssistant,
    points: list[UsagePoint] | tuple[UsagePoint, ...],
    tariff: Tariff,
    tz: tzinfo,
) -> None:
    """Store new hourly points as cumulative usage and cost statistics.

    Only points newer than the last stored statistic are written, so repeated
    polls and backfills never double count.
    """
    ordered = sorted((point for point in points if point.kwh > 0), key=lambda p: p.start)
    if not ordered:
        return

    first_start = ordered[0].start
    last_start, usage_sum = await _async_last_state(hass, USAGE_STATISTIC_ID, first_start)
    _, cost_sum = await _async_last_state(hass, COST_STATISTIC_ID, first_start)

    usage_rows: list[dict[str, Any]] = []
    cost_rows: list[dict[str, Any]] = []

    for point in ordered:
        if last_start is not None and point.start <= last_start:
            continue
        rate = tariff.rate_at(point.start.astimezone(tz))
        usage_sum += point.kwh
        cost_sum += point.kwh * rate
        start = point.start.astimezone(timezone.utc).replace(
            minute=0, second=0, microsecond=0
        )
        usage_rows.append({"start": start, "sum": round(usage_sum, 6)})
        cost_rows.append({"start": start, "sum": round(cost_sum, 6)})

    if usage_rows:
        async_add_external_statistics(
            hass,
            _metadata(USAGE_STATISTIC_ID, f"{NAME} usage", "kWh"),
            usage_rows,
        )
    if cost_rows:
        async_add_external_statistics(
            hass,
            _metadata(COST_STATISTIC_ID, f"{NAME} cost", "USD"),
            cost_rows,
        )
