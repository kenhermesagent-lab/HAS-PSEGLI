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

from .const import (
    COST_STATISTIC_ID,
    DOMAIN,
    MAX_BACKFILL_DAYS,
    NAME,
    USAGE_STATISTIC_ID,
)
from .parser import Tariff, UsagePoint, cumulative_hourly

_LOGGER = logging.getLogger(__name__)

#: Cheap window that covers the normal polling overlap.
_LOOKBACK = timedelta(days=45)
#: Used when the narrow window finds nothing, so a long gap (integration
#: disabled for months, Home Assistant offline) continues the stored sum
#: instead of restarting it at zero and making the sensor look reset.
_WIDE_LOOKBACK = timedelta(days=MAX_BACKFILL_DAYS + 60)


async def _async_last_state_within(
    hass: HomeAssistant,
    statistic_id: str,
    before: datetime,
    lookback: timedelta,
) -> tuple[datetime | None, float]:
    """Return the newest stored (start, cumulative sum) in a window."""
    try:
        instance = get_instance(hass)
    except KeyError:
        _LOGGER.debug("Recorder is not available yet")
        return None, 0.0

    try:
        rows = await instance.async_add_executor_job(
            statistics_during_period,
            hass,
            before - lookback,
            before,
            [statistic_id],
            "hour",
            None,
            {"start", "sum"},
        )
    except Exception:  # noqa: BLE001 - recorder internals vary between versions
        _LOGGER.debug("Could not read statistics for %s", statistic_id, exc_info=True)
        return None, 0.0

    for entry in reversed(rows.get(statistic_id) or []):
        if entry.get("sum") is not None:
            start = entry["start"]
            if isinstance(start, datetime) and start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            return start, float(entry["sum"])
    return None, 0.0


async def _async_last_state(
    hass: HomeAssistant, statistic_id: str, before: datetime
) -> tuple[datetime | None, float]:
    """Return the newest stored (start, cumulative sum) at or before a time."""
    for lookback in (_LOOKBACK, _WIDE_LOOKBACK):
        start, total = await _async_last_state_within(
            hass, statistic_id, before, lookback
        )
        if start is not None:
            return start, total
    return None, 0.0


async def async_has_statistics(hass: HomeAssistant, statistic_id: str) -> bool:
    """Return True when the statistic already has rows."""
    start, _ = await _async_last_state(
        hass, statistic_id, datetime.now(timezone.utc)
    )
    return start is not None


def _as_iso(value: Any) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


async def async_statistics_coverage(
    hass: HomeAssistant, statistic_id: str
) -> dict[str, Any]:
    """Summarise the stored rows of one statistic for diagnostics."""
    try:
        instance = get_instance(hass)
    except KeyError:
        return {"available": False, "reason": "recorder not ready"}

    now = datetime.now(timezone.utc)
    try:
        rows = await instance.async_add_executor_job(
            statistics_during_period,
            hass,
            now - _WIDE_LOOKBACK,
            now,
            [statistic_id],
            "hour",
            None,
            {"start", "sum"},
        )
    except Exception:  # noqa: BLE001 - diagnostics must never raise
        _LOGGER.debug("Could not read statistics for %s", statistic_id, exc_info=True)
        return {"available": False, "reason": "query failed"}

    entries = rows.get(statistic_id) or []
    if not entries:
        return {"available": True, "rows": 0}
    return {
        "available": True,
        "rows": len(entries),
        "first": _as_iso(entries[0].get("start")),
        "last": _as_iso(entries[-1].get("start")),
        "last_sum": entries[-1].get("sum"),
    }


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
    """Write hourly points as cumulative usage and cost statistics.

    Rows are recomputed from the last stored sum *before* the imported range,
    which is what makes a backfill safe to repeat: hours already in the
    database are replaced by the recorder (upsert on the start timestamp)
    rather than counted twice, and hours the portal no longer reports as used
    are rewritten to a zero contribution.
    """
    if not points:
        return

    # Compare on the hour boundary: rows are stored per hour, and using a
    # point's exact timestamp could pick up the row of the hour we are about
    # to rewrite.
    first_start = (
        min(point.start for point in points)
        .astimezone(timezone.utc)
        .replace(minute=0, second=0, microsecond=0)
    )
    last_start, usage_base = await _async_last_state(
        hass, USAGE_STATISTIC_ID, first_start
    )
    _, cost_base = await _async_last_state(hass, COST_STATISTIC_ID, first_start)

    rows = cumulative_hourly(
        points,
        tariff,
        tz,
        usage_base=usage_base,
        cost_base=cost_base,
        after=last_start,
    )
    if not rows:
        return

    async_add_external_statistics(
        hass,
        _metadata(USAGE_STATISTIC_ID, f"{NAME} usage", "kWh"),
        [{"start": start, "sum": usage} for start, usage, _cost in rows],
    )
    async_add_external_statistics(
        hass,
        _metadata(COST_STATISTIC_ID, f"{NAME} cost", "USD"),
        [{"start": start, "sum": cost} for start, _usage, cost in rows],
    )
    _LOGGER.debug(
        "Wrote %s PSEG statistics rows from %s to %s",
        len(rows),
        rows[0][0].isoformat(),
        rows[-1][0].isoformat(),
    )
