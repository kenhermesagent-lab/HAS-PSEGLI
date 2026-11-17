"""The PSEG Long Island integration."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.event import async_track_time_interval

from .api import PsegliClient
from .const import (
    ATTR_DAYS,
    CONF_BACKFILL_DAYS,
    CONF_COOKIE,
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_TIMESTAMP_OFFSET,
    DEFAULT_BACKFILL_DAYS,
    DOMAIN,
    KEEP_ALIVE_MINUTES,
    MAX_BACKFILL_DAYS,
    PLATFORMS,
    SERVICE_BACKFILL,
    USAGE_STATISTIC_ID,
)
from .coordinator import PsegliCoordinator
from .exceptions import PsegliError
from .statistics import async_has_statistics

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register the backfill service once per Home Assistant instance."""
    hass.data.setdefault(DOMAIN, {})

    async def _async_handle_backfill(call: ServiceCall) -> None:
        coordinators = list(hass.data.get(DOMAIN, {}).values())
        if not coordinators:
            raise HomeAssistantError("PSEG Long Island is not configured")

        try:
            days = int(call.data.get(ATTR_DAYS, DEFAULT_BACKFILL_DAYS))
        except (TypeError, ValueError):
            days = DEFAULT_BACKFILL_DAYS
        days = max(1, min(days, MAX_BACKFILL_DAYS))

        for coordinator in coordinators:
            await coordinator.async_backfill(days)

    if not hass.services.has_service(DOMAIN, SERVICE_BACKFILL):
        hass.services.async_register(
            DOMAIN, SERVICE_BACKFILL, _async_handle_backfill
        )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up PSEG Long Island from a config entry."""
    try:
        offset = int(entry.options.get(CONF_TIMESTAMP_OFFSET, 0))
    except (TypeError, ValueError):
        offset = 0

    # A private session with its own cookie jar: the portal rotates its session
    # cookie, and those rotated values must not leak into other integrations
    # (nor be overwritten by them).
    # auto_cleanup closes this session when the config entry unloads.
    session = async_create_clientsession(
        hass, cookie_jar=aiohttp.CookieJar(unsafe=False)
    )

    def _async_store_cookie(cookie: str) -> None:
        """Persist cookies PSEG handed back during a request."""
        if not cookie or entry.data.get(CONF_COOKIE) == cookie:
            return
        _LOGGER.debug("Storing refreshed PSEG session cookies")
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_COOKIE: cookie}
        )

    client = PsegliClient(
        session,
        entry.data.get(CONF_COOKIE, ""),
        email=entry.data.get(CONF_EMAIL),
        password=entry.data.get(CONF_PASSWORD),
        timestamp_offset_minutes=offset,
        on_cookie_refresh=_async_store_cookie,
    )
    coordinator = PsegliCoordinator(hass, entry, client)
    # Cookie refreshes update the entry's data, which also notifies update
    # listeners; remember the options so those notifications do not turn into
    # an endless refresh loop.
    options_snapshot = dict(entry.options)

    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    async def _async_entry_updated(
        hass: HomeAssistant, entry: ConfigEntry
    ) -> None:
        if dict(entry.options) == options_snapshot:
            return
        options_snapshot.clear()
        options_snapshot.update(entry.options)
        await coordinator.async_request_refresh()

    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))

    async def _async_keep_alive(now: datetime) -> None:
        """Keep the PSEG session from idling out between polls.

        The portal expires sessions that stop being used, which is what makes a
        pasted cookie die once no browser is talking to it.  A failed touch is
        only logged: the coordinator decides when authentication is broken.
        """
        try:
            await client.async_keep_alive()
        except PsegliError as err:
            _LOGGER.debug("PSEG session keep-alive failed: %s", err)

    entry.async_on_unload(
        async_track_time_interval(
            hass, _async_keep_alive, timedelta(minutes=KEEP_ALIVE_MINUTES)
        )
    )

    backfill_days = int(
        entry.options.get(CONF_BACKFILL_DAYS, DEFAULT_BACKFILL_DAYS)
    )
    if backfill_days > 0 and not await async_has_statistics(
        hass, USAGE_STATISTIC_ID
    ):
        entry.async_create_background_task(
            hass,
            _async_initial_backfill(coordinator, backfill_days),
            "psegli initial backfill",
        )

    return True


async def _async_initial_backfill(
    coordinator: PsegliCoordinator, days: int
) -> None:
    try:
        await coordinator.async_backfill(days)
    except Exception:  # noqa: BLE001 - a failed backfill must not break setup
                _LOGGER.warning("Initial PSEG backfill failed", exc_info=True)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return True
