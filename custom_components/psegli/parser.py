"""Parsing, aggregation and tariff maths for the PSEG Long Island integration.

This module deliberately has no Home Assistant imports so the logic can be
unit tested without a Home Assistant installation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Any, Final, Mapping, Sequence

# ---------------------------------------------------------------------------
# Tariff option keys. const.py re-exports these so the whole integration uses
# one set of strings.
# ---------------------------------------------------------------------------

OPT_RATE_MODE: Final = "rate_mode"
OPT_DELIVERY_RATE: Final = "delivery_rate"
OPT_SUPPLY_RATE: Final = "supply_rate"
OPT_PEAK_DELIVERY_RATE: Final = "peak_delivery_rate"
OPT_PEAK_SUPPLY_MULTIPLIER: Final = "peak_supply_multiplier"
OPT_DAILY_SERVICE_CHARGE: Final = "daily_service_charge"
OPT_PEAK_START_HOUR: Final = "peak_start_hour"
OPT_PEAK_END_HOUR: Final = "peak_end_hour"
OPT_PEAK_WEEKDAYS_ONLY: Final = "peak_weekdays_only"
OPT_BACKFILL_DAYS: Final = "backfill_days"
OPT_WEEK_START: Final = "week_start"
OPT_BILLING_DAY: Final = "billing_day"
OPT_TIMESTAMP_OFFSET: Final = "timestamp_offset_minutes"

RATE_MODE_FLAT: Final = "flat"
RATE_MODE_TOU: Final = "tou"
WEEK_START_MONDAY: Final = "monday"
WEEK_START_SUNDAY: Final = "sunday"

# Defaults are intentionally conservative: the user must enter their own rate
# because PSEG changes the power supply charge every month.
DEFAULT_TARIFF: Final[dict[str, Any]] = {
    OPT_RATE_MODE: RATE_MODE_FLAT,
    OPT_DELIVERY_RATE: 0.1064,
    OPT_SUPPLY_RATE: 0.0,
    OPT_PEAK_DELIVERY_RATE: 0.2324,
    OPT_PEAK_SUPPLY_MULTIPLIER: 1.0,
    OPT_DAILY_SERVICE_CHARGE: 0.56,
    OPT_PEAK_START_HOUR: 16,
    OPT_PEAK_END_HOUR: 20,
    OPT_PEAK_WEEKDAYS_ONLY: True,
    OPT_BACKFILL_DAYS: 30,
    OPT_WEEK_START: WEEK_START_MONDAY,
    OPT_BILLING_DAY: 1,
    OPT_TIMESTAMP_OFFSET: 0,
}

# ---------------------------------------------------------------------------
# Payload parsing
# ---------------------------------------------------------------------------

TOKEN_PATTERNS = (
    re.compile(r'name="__RequestVerificationToken"[^>]*?value="([^"]+)"', re.IGNORECASE),
    re.compile(r'value="([^"]+)"[^>]*?name="__RequestVerificationToken"', re.IGNORECASE),
)

_IGNORED_SERIES = re.compile(r"(temp|degree|weather|cost|price|bill)", re.IGNORECASE)
_TOTAL_SERIES = re.compile(r"(total|usage|consumption|kwh)", re.IGNORECASE)
_PEAK_SPLIT = re.compile(r"peak", re.IGNORECASE)
_OFF_PEAK = re.compile(r"off[\s_-]*peak", re.IGNORECASE)

MAX_PLAUSIBLE_KWH = 1000.0


@dataclass(frozen=True)
class UsagePoint:
    """A single interval of energy use."""

    start: datetime  # timezone aware, UTC
    kwh: float


def extract_request_verification_token(html: str) -> str | None:
    """Return the ASP.NET anti-forgery token from a MyMeter page."""
    if not html:
        return None
    for pattern in TOKEN_PATTERNS:
        match = pattern.search(html)
        if match:
            return match.group(1)
    return None


def _iter_series_points(
    series: Mapping[str, Any], offset_minutes: int
) -> list[tuple[datetime, float]]:
    """Normalise one chart series into (timestamp, kWh) pairs."""
    points: list[tuple[datetime, float]] = []
    for raw in series.get("data") or []:
        x: Any = None
        y: Any = None
        if isinstance(raw, Mapping):
            x, y = raw.get("x"), raw.get("y")
        elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
            if len(raw) >= 2:
                x, y = raw[0], raw[1]
        if x is None or y is None:
            continue
        try:
            kwh = float(y)
            timestamp = float(x)
        except (TypeError, ValueError):
            continue
        if kwh < 0 or kwh > MAX_PLAUSIBLE_KWH:
            continue
        if timestamp > 1e11:  # milliseconds
            timestamp /= 1000.0
        try:
            start = datetime.fromtimestamp(timestamp, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            continue
        start += timedelta(minutes=offset_minutes)
        points.append((start, kwh))
    return points


def parse_chart_payload(
    payload: Mapping[str, Any] | None, offset_minutes: int = 0
) -> list[UsagePoint]:
    """Turn a /Dashboard/ChartData response into sorted usage points.

    The portal returns either one total series or a peak/off-peak split.  When
    it returns a split we add the series together; when a single total series
    is present we prefer it so the split is never double counted.  Comparison
    series (temperature, cost) are ignored.
    """
    series_list = ((payload or {}).get("Data") or {}).get("series") or []
    usable: list[tuple[str, list[tuple[datetime, float]]]] = []

    for series in series_list:
        if not isinstance(series, Mapping):
            continue
        name = str(series.get("name") or "")
        if _IGNORED_SERIES.search(name):
            continue
        points = _iter_series_points(series, offset_minutes)
        if points:
            usable.append((name, points))

    if not usable:
        return []

    splits = [item for item in usable if _PEAK_SPLIT.search(item[0])]
    totals = [item for item in usable if not _PEAK_SPLIT.search(item[0])]

    if totals:
        preferred = [item for item in totals if _TOTAL_SERIES.search(item[0])]
        chosen = preferred[:1] or totals[:1]
    else:
        chosen = splits

    merged: dict[datetime, float] = {}
    for _name, points in chosen:
        for start, kwh in points:
            merged[start] = merged.get(start, 0.0) + kwh

    return [UsagePoint(start=start, kwh=round(kwh, 6)) for start, kwh in sorted(merged.items())]


# ---------------------------------------------------------------------------
# Tariff
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Tariff:
    """A simplified model of a PSEG Long Island residential rate."""

    mode: str = RATE_MODE_FLAT
    delivery_rate: float = 0.0
    supply_rate: float = 0.0
    peak_delivery_rate: float = 0.0
    peak_supply_multiplier: float = 1.0
    daily_service_charge: float = 0.0
    peak_start_hour: int = 16
    peak_end_hour: int = 20
    peak_weekdays_only: bool = True

    @classmethod
    def from_options(cls, options: Mapping[str, Any] | None) -> "Tariff":
        options = options or {}

        def as_float(key: str, default: float) -> float:
            try:
                return float(options.get(key, default))
            except (TypeError, ValueError):
                return float(default)

        def as_int(key: str, default: int) -> int:
            try:
                return int(float(options.get(key, default)))
            except (TypeError, ValueError):
                return int(default)

        mode = str(options.get(OPT_RATE_MODE, DEFAULT_TARIFF[OPT_RATE_MODE])).lower()
        if mode not in (RATE_MODE_FLAT, RATE_MODE_TOU):
            mode = RATE_MODE_FLAT

        return cls(
            mode=mode,
            delivery_rate=as_float(OPT_DELIVERY_RATE, DEFAULT_TARIFF[OPT_DELIVERY_RATE]),
            supply_rate=as_float(OPT_SUPPLY_RATE, DEFAULT_TARIFF[OPT_SUPPLY_RATE]),
            peak_delivery_rate=as_float(
                OPT_PEAK_DELIVERY_RATE, DEFAULT_TARIFF[OPT_PEAK_DELIVERY_RATE]
            ),
            peak_supply_multiplier=as_float(
                OPT_PEAK_SUPPLY_MULTIPLIER, DEFAULT_TARIFF[OPT_PEAK_SUPPLY_MULTIPLIER]
            ),
            daily_service_charge=as_float(
                OPT_DAILY_SERVICE_CHARGE, DEFAULT_TARIFF[OPT_DAILY_SERVICE_CHARGE]
            ),
            peak_start_hour=as_int(
                OPT_PEAK_START_HOUR, DEFAULT_TARIFF[OPT_PEAK_START_HOUR]
            ),
            peak_end_hour=as_int(OPT_PEAK_END_HOUR, DEFAULT_TARIFF[OPT_PEAK_END_HOUR]),
            peak_weekdays_only=bool(
                options.get(
                    OPT_PEAK_WEEKDAYS_ONLY, DEFAULT_TARIFF[OPT_PEAK_WEEKDAYS_ONLY]
                )
            ),
        )

    def is_peak(self, local_dt: datetime) -> bool:
        """Return True when the given local time falls inside the peak window."""
        if self.mode != RATE_MODE_TOU:
            return False
        if self.peak_weekdays_only and local_dt.weekday() >= 5:
            return False
        hour = local_dt.hour
        if self.peak_start_hour <= self.peak_end_hour:
            return self.peak_start_hour <= hour < self.peak_end_hour
        return hour >= self.peak_start_hour or hour < self.peak_end_hour

    def rate_at(self, local_dt: datetime) -> float:
        """Return the all-in $/kWh rate for a local timestamp."""
        if self.is_peak(local_dt):
            return self.peak_delivery_rate + self.supply_rate * self.peak_supply_multiplier
        return self.delivery_rate + self.supply_rate


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PeriodTotals:
    """Usage and estimated cost for one calendar period."""

    key: str
    kwh: float
    cost: float
    days: int
    start: datetime


def _month_before(today: date) -> tuple[date, date]:
    if today.month == 1:
        year, month = today.year - 1, 12
    else:
        year, month = today.year, today.month - 1
    start = date(year, month, 1)
    next_month = date(year + (month == 12), (month % 12) + 1, 1)
    return start, next_month - timedelta(days=1)


def _cycle_start(today: date, billing_day: int) -> date:
    billing_day = max(1, min(28, int(billing_day)))
    if today.day >= billing_day:
        return today.replace(day=billing_day)
    if today.month == 1:
        year, month = today.year - 1, 12
    else:
        year, month = today.year, today.month - 1
    return date(year, month, billing_day)


def aggregate(
    points: Sequence[UsagePoint],
    tariff: Tariff,
    tz: tzinfo,
    *,
    now: datetime | None = None,
    week_start: str = WEEK_START_MONDAY,
    billing_day: int = 1,
) -> dict[str, PeriodTotals]:
    """Build today/yesterday/week/month/last-month/billing-cycle totals."""
    now_local = (now or datetime.now(timezone.utc)).astimezone(tz)
    today = now_local.date()

    weekday = 6 if str(week_start).lower() == WEEK_START_SUNDAY else 0
    week_first = today - timedelta(days=(today.weekday() - weekday) % 7)
    last_month_start, last_month_end = _month_before(today)

    ranges: dict[str, tuple[date, date]] = {
        "today": (today, today),
        "yesterday": (today - timedelta(days=1), today - timedelta(days=1)),
        "week": (week_first, today),
        "month": (today.replace(day=1), today),
        "last_month": (last_month_start, last_month_end),
        "cycle": (_cycle_start(today, billing_day), today),
    }

    kwh_buckets: dict[str, float] = {key: 0.0 for key in ranges}
    cost_buckets: dict[str, float] = {key: 0.0 for key in ranges}

    for point in points:
        local = point.start.astimezone(tz)
        point_date = local.date()
        rate = tariff.rate_at(local)
        for key, (start, end) in ranges.items():
            if start <= point_date <= end:
                kwh_buckets[key] += point.kwh
                cost_buckets[key] += point.kwh * rate

    result: dict[str, PeriodTotals] = {}
    for key, (start, end) in ranges.items():
        days = (end - start).days + 1
        energy_cost = cost_buckets[key]
        fixed_cost = tariff.daily_service_charge * days
        result[key] = PeriodTotals(
            key=key,
            kwh=round(kwh_buckets[key], 3),
            cost=round(energy_cost + fixed_cost, 2),
            days=days,
            start=datetime.combine(start, time.min, tzinfo=tz),
        )
    return result


def latest_point(points: Sequence[UsagePoint]) -> UsagePoint | None:
    """Return the most recent interval, if any."""
    return max(points, key=lambda point: point.start) if points else None
