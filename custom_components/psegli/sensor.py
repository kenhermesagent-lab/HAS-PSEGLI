"""Sensors for the PSEG Long Island integration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import BASE_URL, DOMAIN, NAME
from .coordinator import PsegliCoordinator

#: All state comes from the coordinator, so there is nothing to throttle per
#: entity and no benefit in serialising the updates.
PARALLEL_UPDATES = 0


@dataclass(frozen=True)
class PeriodDescription:
    """One aggregated period exposed as a pair of sensors."""

    key: str
    translation_key: str
    state_class: SensorStateClass


PERIODS: tuple[PeriodDescription, ...] = (
    # Today, this week, this month and the billing cycle only grow inside their
    # period and restart at the period boundary: that is TOTAL_INCREASING.
    PeriodDescription("today", "today", SensorStateClass.TOTAL_INCREASING),
    # Yesterday and last month are snapshots of a finished period. Their value
    # can move either way, so they stay TOTAL with an explicit reset point.
    PeriodDescription("yesterday", "yesterday", SensorStateClass.TOTAL),
    PeriodDescription("week", "week", SensorStateClass.TOTAL_INCREASING),
    PeriodDescription("month", "month", SensorStateClass.TOTAL_INCREASING),
    PeriodDescription("last_month", "last_month", SensorStateClass.TOTAL),
    PeriodDescription("cycle", "cycle", SensorStateClass.TOTAL_INCREASING),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up PSEG sensors."""
    coordinator: PsegliCoordinator = entry.runtime_data

    entities: list[SensorEntity] = [PsegliLatestIntervalSensor(coordinator)]
    for period in PERIODS:
        entities.append(PsegliUsageSensor(coordinator, period))
        entities.append(PsegliCostSensor(coordinator, period))
    entities.append(PsegliLastReadingSensor(coordinator))

    async_add_entities(entities)


class PsegliBaseSensor(CoordinatorEntity[PsegliCoordinator], SensorEntity):
    """Shared device wiring."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, coordinator: PsegliCoordinator, key: str) -> None:
        super().__init__(coordinator)
        entry = coordinator.entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=NAME,
            manufacturer=NAME,
            model="MySmartEnergy",
            configuration_url=BASE_URL,
        )


class PsegliUsageSensor(PsegliBaseSensor):
    """Consumption for a calendar period."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_suggested_display_precision = 3

    def __init__(
        self, coordinator: PsegliCoordinator, period: PeriodDescription
    ) -> None:
        super().__init__(coordinator, f"{period.key}_usage")
        self._period = period.key
        self._attr_translation_key = f"{period.translation_key}_usage"
        self._attr_state_class = period.state_class

    @property
    def native_value(self) -> float | None:
        totals = self.coordinator.data.aggregates.get(self._period)
        return None if totals is None else totals.kwh

    @property
    def last_reset(self) -> datetime | None:
        """Start of the period for TOTAL sensors; TOTAL_INCREASING resets itself."""
        if self._attr_state_class != SensorStateClass.TOTAL:
            return None
        totals = self.coordinator.data.aggregates.get(self._period)
        return None if totals is None else totals.start

    @property
    def extra_state_attributes(self) -> dict[str, int] | None:
        totals = self.coordinator.data.aggregates.get(self._period)
        return None if totals is None else {"days": totals.days}


class PsegliCostSensor(PsegliBaseSensor):
    """Estimated cost for a calendar period."""

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = "USD"
    # The monetary device class only accepts TOTAL, so the period reset is
    # expressed with last_reset instead of TOTAL_INCREASING.
    _attr_state_class = SensorStateClass.TOTAL
    _attr_suggested_display_precision = 2

    def __init__(
        self, coordinator: PsegliCoordinator, period: PeriodDescription
    ) -> None:
        super().__init__(coordinator, f"{period.key}_cost")
        self._period = period.key
        self._attr_translation_key = f"{period.translation_key}_cost"

    @property
    def native_value(self) -> float | None:
        totals = self.coordinator.data.aggregates.get(self._period)
        return None if totals is None else totals.cost

    @property
    def last_reset(self) -> datetime | None:
        totals = self.coordinator.data.aggregates.get(self._period)
        return None if totals is None else totals.start

    @property
    def extra_state_attributes(self) -> dict[str, float | int] | None:
        totals = self.coordinator.data.aggregates.get(self._period)
        if totals is None:
            return None
        return {"days": totals.days, "kwh": totals.kwh}


class PsegliLatestIntervalSensor(PsegliBaseSensor):
    """Most recent hourly reading available from PSEG."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_translation_key = "latest_interval_usage"
    _attr_suggested_display_precision = 3

    def __init__(self, coordinator: PsegliCoordinator) -> None:
        super().__init__(coordinator, "latest_interval_usage")

    @property
    def native_value(self) -> float | None:
        latest = self.coordinator.data.latest
        return None if latest is None else latest.kwh

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        latest = self.coordinator.data.latest
        if latest is None:
            return None
        local = latest.start.astimezone(self.coordinator.time_zone)
        return {
            "interval_start": latest.start.isoformat(),
            "peak": self.coordinator.data.tariff.is_peak(local),
            "rate": round(self.coordinator.data.tariff.rate_at(local), 5),
        }


class PsegliLastReadingSensor(PsegliBaseSensor):
    """Timestamp of the newest reading PSEG has published."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_translation_key = "last_reading"
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: PsegliCoordinator) -> None:
        super().__init__(coordinator, "last_reading")

    @property
    def native_value(self) -> datetime | None:
        latest = self.coordinator.data.latest
        return None if latest is None else latest.start
