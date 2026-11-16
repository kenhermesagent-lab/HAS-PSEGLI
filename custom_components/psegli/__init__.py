"""The PSEG Long Island integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import PsegliClient
from .const import (
    ATTR_DAYS,
    CONF_BACKFILL_DAYS,
    CONF_COOKIE,
    CONF_TIMESTAMP_OFFSET,
    DEFAULT_BACKFILL_DAYS,
    DOMAIN,
    MAX_BACKFILL_DAYS,
    PLATFORMS,
    SERVICE_BACKFILL,
    USAGE_STATISTIC_ID,
)
from .coordinator import PsegliCoordinator
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

    client = PsegliClient(
        async_get_clientsession(hass), entry.data[CONF_COOKIE], offset
    )
    coordinator = PsegliCoordinator(hass, entry, client)

    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

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


async def _async_options_updated(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Apply tariff changes without reloading the integration."""
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator is not None:
        await coordinator.async_request_refresh()


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return True
