"""Data coordinator for the PSEG Long Island integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import PsegliClient
from .const import (
    CONF_BACKFILL_DAYS,
    CONF_BILLING_DAY,
    CONF_TIMESTAMP_OFFSET,
    CONF_WEEK_START,
    DEFAULT_BACKFILL_DAYS,
    DEFAULT_LOOKBACK_DAYS,
    DEFAULT_TARIFF,
    DOMAIN,
    MAX_BACKFILL_DAYS,
    NAME,
    UPDATE_INTERVAL_MINUTES,
)
from .exceptions import PsegliAuthError, PsegliError
from .parser import (
    OPT_BILLING_DAY,
    OPT_WEEK_START,
    PeriodTotals,
    Tariff,
    UsagePoint,
    aggregate,
    latest_point,
)
from .statistics import async_import_usage

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class PsegliData:
    """Everything the sensors need for one update."""

    points: tuple[UsagePoint, ...]
    aggregates: dict[str, PeriodTotals]
    latest: UsagePoint | None
    tariff: Tariff


class PsegliCoordinator(DataUpdateCoordinator[PsegliData]):
    """Poll the MySmartEnergy portal and keep statistics in sync."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, client: PsegliClient
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=NAME,
            update_interval=timedelta(minutes=UPDATE_INTERVAL_MINUTES),
        )
        self.entry = entry
        self.client = client
        self.time_zone: tzinfo = (
            dt_util.get_time_zone(hass.config.time_zone) or timezone.utc
        )
        self.tariff = Tariff.from_options(entry.options)

    async def _async_update_data(self) -> PsegliData:
        try:
            offset = int(self.entry.options.get(CONF_TIMESTAMP_OFFSET, 0) or 0)
        except (TypeError, ValueError):
            offset = 0
        self.client.update_timestamp_offset(offset)

        now_local = dt_util.now(self.time_zone)
        start = (now_local - timedelta(days=DEFAULT_LOOKBACK_DAYS)).date()

        try:
            points = await self.client.async_fetch_points(start, now_local.date())
        except PsegliAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except PsegliError as err:
            raise UpdateFailed(str(err)) from err

        self.tariff = Tariff.from_options(self.entry.options)

        if points:
            try:
                await async_import_usage(self.hass, points, self.tariff, self.time_zone)
            except Exception:  # noqa: BLE001 - statistics must not break polling
                _LOGGER.warning("Could not import PSEG statistics", exc_info=True)

        aggregates = aggregate(
            points,
            self.tariff,
            self.time_zone,
            now=now_local,
            week_start=str(
                self.entry.options.get(OPT_WEEK_START, DEFAULT_TARIFF[OPT_WEEK_START])
            ),
            billing_day=int(
                self.entry.options.get(
                    OPT_BILLING_DAY, DEFAULT_TARIFF[OPT_BILLING_DAY]
                )
            ),
        )
        return PsegliData(
            points=tuple(points),
            aggregates=aggregates,
            latest=latest_point(points),
            tariff=self.tariff,
        )

    async def async_backfill(self, days: int | None = None) -> None:
        """Fetch and import up to `days` of history."""
        if days is None:
            days = int(
                self.entry.options.get(CONF_BACKFILL_DAYS, DEFAULT_BACKFILL_DAYS)
            )
        days = max(1, min(int(days), MAX_BACKFILL_DAYS))

        now_local = dt_util.now(self.time_zone)
        start = (now_local - timedelta(days=days)).date()

        try:
            points = await self.client.async_fetch_points(start, now_local.date())
        except PsegliAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except PsegliError as err:
            raise HomeAssistantError(f"PSEG backfill failed: {err}") from err

        if not points:
            _LOGGER.info("PSEG returned no data for the last %s days", days)
            return

        await async_import_usage(self.hass, points, self.tariff, self.time_zone)
        _LOGGER.info("Imported %s PSEG intervals from the last %s days", len(points), days)
        await self.async_request_refresh()
