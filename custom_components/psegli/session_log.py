"""A persisted timeline of PSEG session rejections and recoveries.

A session that keeps dying is only diagnosable if the failures survive a Home
Assistant restart: the interesting pattern usually spans days.  This module
keeps the last week of events (bounded to a couple of hundred) in Home
Assistant's own storage, per config entry.

Only what the portal answered is stored - the time, which request, the reason
and the HTTP status.  Cookie values, passwords and the email address never
reach the file.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.session_log"
#: A week is enough to see a daily or half-hourly pattern, and small enough to
#: keep the file trivial.
MAX_AGE = timedelta(days=7)
MAX_EVENTS = 200


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse the ISO timestamp stored with an event."""
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def prune_events(
    events: Iterable[Any], *, now: datetime | None = None
) -> list[dict[str, Any]]:
    """Drop malformed and stale events, keeping the newest ones."""
    cutoff = (now or datetime.now(timezone.utc)) - MAX_AGE
    kept: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, Mapping):
            continue
        stamp = _parse_timestamp(event.get("at"))
        if stamp is None or stamp < cutoff:
            continue
        kept.append(dict(event))
    return kept[-MAX_EVENTS:]


class SessionLog:
    """Store the recent PSEG session events for one config entry."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{STORAGE_KEY}.{entry_id}"
        )
        self._lock = asyncio.Lock()
        self._events: list[dict[str, Any]] | None = None

    async def async_events(self) -> list[dict[str, Any]]:
        """Return the stored timeline, newest last."""
        async with self._lock:
            return list(await self._async_loaded())

    async def async_add(self, event: Mapping[str, Any]) -> None:
        """Append one event and write the trimmed timeline back."""
        async with self._lock:
            events = list(await self._async_loaded())
            events.append(dict(event))
            self._events = prune_events(events)
            try:
                await self._store.async_save({"events": self._events})
            except Exception:  # noqa: BLE001 - the log must not break polling
                _LOGGER.debug("Could not store the PSEG session timeline", exc_info=True)

    async def async_remove(self) -> None:
        """Delete the stored timeline."""
        async with self._lock:
            self._events = []
            try:
                await self._store.async_remove()
            except Exception:  # noqa: BLE001 - deletion already happened
                _LOGGER.debug("Could not delete the PSEG session timeline", exc_info=True)

    async def _async_loaded(self) -> list[dict[str, Any]]:
        if self._events is None:
            try:
                data = await self._store.async_load()
            except Exception:  # noqa: BLE001 - a broken file is not fatal
                _LOGGER.debug("Could not read the PSEG session timeline", exc_info=True)
                data = None
            events = data.get("events") if isinstance(data, Mapping) else None
            self._events = prune_events(events or [])
        return self._events
