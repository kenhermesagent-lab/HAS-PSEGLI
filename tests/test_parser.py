"""Offline tests for the PSEG Long Island parsing and tariff logic."""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

COMPONENT_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "psegli"
sys.path.insert(0, str(COMPONENT_DIR))

import parser  # noqa: E402

NY = ZoneInfo("America/New_York")


class TokenTests(unittest.TestCase):
    def test_extracts_token(self) -> None:
        html = (
            '<input name="__RequestVerificationToken" type="hidden" '
            'value="abc123" />'
        )
        self.assertEqual(parser.extract_request_verification_token(html), "abc123")

    def test_extracts_token_with_reordered_attributes(self) -> None:
        html = '<input value="xyz" name="__RequestVerificationToken" />'
        self.assertEqual(parser.extract_request_verification_token(html), "xyz")

    def test_missing_token(self) -> None:
        self.assertIsNone(parser.extract_request_verification_token("<html></html>"))


class ParseChartTests(unittest.TestCase):
    def _payload(self, series: list[dict]) -> dict:
        return {"Data": {"series": series}}

    def test_single_total_series(self) -> None:
        start = datetime(2026, 1, 10, 15, tzinfo=timezone.utc)
        payload = self._payload(
            [
                {
                    "name": "Usage",
                    "data": [{"x": int(start.timestamp() * 1000), "y": 1.25}],
                }
            ]
        )
        points = parser.parse_chart_payload(payload)
        self.assertEqual(len(points), 1)
        self.assertAlmostEqual(points[0].kwh, 1.25)
        self.assertEqual(points[0].start, start)

    def test_peak_and_off_peak_are_summed(self) -> None:
        ts = int(datetime(2026, 1, 10, 15, tzinfo=timezone.utc).timestamp() * 1000)
        payload = self._payload(
            [
                {"name": "On-Peak Usage", "data": [{"x": ts, "y": 1.0}]},
                {"name": "Off-Peak Usage", "data": [{"x": ts, "y": 2.0}]},
            ]
        )
        points = parser.parse_chart_payload(payload)
        self.assertEqual(len(points), 1)
        self.assertAlmostEqual(points[0].kwh, 3.0)

    def test_total_wins_over_split(self) -> None:
        ts = int(datetime(2026, 1, 10, 15, tzinfo=timezone.utc).timestamp() * 1000)
        payload = self._payload(
            [
                {"name": "Total Usage", "data": [{"x": ts, "y": 3.0}]},
                {"name": "On-Peak Usage", "data": [{"x": ts, "y": 1.0}]},
                {"name": "Off-Peak Usage", "data": [{"x": ts, "y": 2.0}]},
            ]
        )
        points = parser.parse_chart_payload(payload)
        self.assertAlmostEqual(points[0].kwh, 3.0)

    def test_comparison_series_ignored(self) -> None:
        ts = int(datetime(2026, 1, 10, 15, tzinfo=timezone.utc).timestamp() * 1000)
        payload = self._payload(
            [
                {"name": "Usage", "data": [{"x": ts, "y": 4.0}]},
                {"name": "Temperature", "data": [{"x": ts, "y": 41.0}]},
                {"name": "Cost", "data": [{"x": ts, "y": 99.0}]},
            ]
        )
        points = parser.parse_chart_payload(payload)
        self.assertEqual(len(points), 1)
        self.assertAlmostEqual(points[0].kwh, 4.0)

    def test_array_points_and_offset(self) -> None:
        start = datetime(2026, 1, 10, 15, tzinfo=timezone.utc)
        payload = self._payload(
            [{"name": "Usage", "data": [[int(start.timestamp() * 1000), 0.5]]}]
        )
        points = parser.parse_chart_payload(payload, offset_minutes=60)
        self.assertEqual(points[0].start, start + timedelta(hours=1))

    def test_negative_and_missing_values_dropped(self) -> None:
        ts = int(datetime(2026, 1, 10, 15, tzinfo=timezone.utc).timestamp() * 1000)
        payload = self._payload(
            [
                {
                    "name": "Usage",
                    "data": [
                        {"x": ts, "y": None},
                        {"x": ts, "y": -1},
                        {"x": ts + 3600000, "y": 2},
                    ],
                }
            ]
        )
        points = parser.parse_chart_payload(payload)
        self.assertEqual(len(points), 1)
        self.assertAlmostEqual(points[0].kwh, 2.0)


class TariffTests(unittest.TestCase):
    def test_flat_rate(self) -> None:
        tariff = parser.Tariff(
            mode=parser.RATE_MODE_FLAT,
            delivery_rate=0.10,
            supply_rate=0.08,
        )
        noon = datetime(2026, 1, 5, 12, tzinfo=NY)
        evening = datetime(2026, 1, 5, 18, tzinfo=NY)
        self.assertAlmostEqual(tariff.rate_at(noon), 0.18)
        self.assertAlmostEqual(tariff.rate_at(evening), 0.18)

    def test_tou_peak_weekday(self) -> None:
        tariff = parser.Tariff(
            mode=parser.RATE_MODE_TOU,
            delivery_rate=0.11,
            supply_rate=0.10,
            peak_delivery_rate=0.23,
            peak_supply_multiplier=1.5,
            peak_start_hour=16,
            peak_end_hour=20,
        )
        monday_peak = datetime(2026, 1, 5, 18, tzinfo=NY)
        monday_off = datetime(2026, 1, 5, 12, tzinfo=NY)
        self.assertAlmostEqual(tariff.rate_at(monday_peak), 0.23 + 0.15)
        self.assertAlmostEqual(tariff.rate_at(monday_off), 0.21)

    def test_tou_peak_skipped_on_weekend(self) -> None:
        tariff = parser.Tariff(
            mode=parser.RATE_MODE_TOU,
            delivery_rate=0.11,
            supply_rate=0.10,
            peak_delivery_rate=0.23,
            peak_start_hour=16,
            peak_end_hour=20,
            peak_weekdays_only=True,
        )
        saturday_peak = datetime(2026, 1, 10, 18, tzinfo=NY)
        self.assertFalse(tariff.is_peak(saturday_peak))
        self.assertAlmostEqual(tariff.rate_at(saturday_peak), 0.21)


class CumulativeHourlyTests(unittest.TestCase):
    """The rows the recorder stores for usage and cost."""

    def setUp(self) -> None:
        self.tariff = parser.Tariff(
            mode=parser.RATE_MODE_FLAT,
            delivery_rate=0.10,
            supply_rate=0.05,
        )

    def _point(self, hour: int, minute: int, kwh: float) -> parser.UsagePoint:
        return parser.UsagePoint(
            start=datetime(2026, 1, 15, hour, minute, tzinfo=timezone.utc), kwh=kwh
        )

    def test_values_aggregate_into_utc_hours(self) -> None:
        rows = parser.cumulative_hourly(
            [
                self._point(10, 5, 1.0),
                self._point(11, 0, 2.0),
                self._point(10, 40, 0.5),
            ],
            self.tariff,
            NY,
            usage_base=10.0,
            cost_base=2.0,
        )

        self.assertEqual(
            [row[0] for row in rows],
            [
                datetime(2026, 1, 15, 10, tzinfo=timezone.utc),
                datetime(2026, 1, 15, 11, tzinfo=timezone.utc),
            ],
        )
        self.assertAlmostEqual(rows[0][1], 11.5)
        self.assertAlmostEqual(rows[1][1], 13.5)
        self.assertAlmostEqual(rows[0][2], 2.0 + 1.5 * 0.15)
        self.assertAlmostEqual(rows[1][2], 2.0 + 3.5 * 0.15)

    def test_after_skips_the_already_stored_hour(self) -> None:
        rows = parser.cumulative_hourly(
            [self._point(10, 0, 1.0), self._point(11, 0, 2.0)],
            self.tariff,
            NY,
            usage_base=5.0,
            after=datetime(2026, 1, 15, 10, tzinfo=timezone.utc),
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0].hour, 11)
        self.assertAlmostEqual(rows[0][1], 7.0)

    def test_revised_hour_is_rewritten_without_its_old_value(self) -> None:
        rows = parser.cumulative_hourly(
            [self._point(10, 0, 0.0)],
            self.tariff,
            NY,
            usage_base=7.0,
        )

        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0][1], 7.0)
        self.assertAlmostEqual(rows[0][2], 0.0)

    def test_sums_stay_monotonic_across_a_second_backfill(self) -> None:
        first = parser.cumulative_hourly(
            [self._point(10, 0, 1.0), self._point(11, 0, 1.0)], self.tariff, NY
        )
        # A later run reaches further back, so the stored sums are recomputed
        # from the older base instead of being appended to.
        second = parser.cumulative_hourly(
            [
                self._point(8, 0, 2.0),
                self._point(9, 0, 2.0),
                self._point(10, 0, 1.0),
                self._point(11, 0, 1.0),
            ],
            self.tariff,
            NY,
        )

        self.assertAlmostEqual(first[-1][1], 2.0)
        self.assertAlmostEqual(second[-1][1], 6.0)
        self.assertEqual(
            [row[1] for row in second], sorted(row[1] for row in second)
        )


class AggregateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tariff = parser.Tariff(
            mode=parser.RATE_MODE_FLAT,
            delivery_rate=0.10,
            supply_rate=0.05,
            daily_service_charge=0.50,
        )
        self.now = datetime(2026, 1, 15, 12, tzinfo=NY)

    def _point(self, local: datetime, kwh: float) -> parser.UsagePoint:
        return parser.UsagePoint(start=local.astimezone(timezone.utc), kwh=kwh)

    def test_period_buckets(self) -> None:
        points = [
            self._point(datetime(2026, 1, 15, 10, tzinfo=NY), 2.0),
            self._point(datetime(2026, 1, 14, 10, tzinfo=NY), 3.0),
            self._point(datetime(2026, 1, 12, 10, tzinfo=NY), 4.0),
            self._point(datetime(2025, 12, 20, 10, tzinfo=NY), 5.0),
        ]
        totals = parser.aggregate(
            points, self.tariff, NY, now=self.now, billing_day=1
        )

        self.assertAlmostEqual(totals["today"].kwh, 2.0)
        self.assertAlmostEqual(totals["yesterday"].kwh, 3.0)
        self.assertAlmostEqual(totals["week"].kwh, 9.0)  # Mon 12th onward
        self.assertAlmostEqual(totals["month"].kwh, 9.0)
        self.assertAlmostEqual(totals["last_month"].kwh, 5.0)
        self.assertAlmostEqual(totals["cycle"].kwh, 9.0)

    def test_daily_charge_added_per_period_day(self) -> None:
        totals = parser.aggregate([], self.tariff, NY, now=self.now, billing_day=1)
        self.assertAlmostEqual(totals["today"].cost, 0.50)
        self.assertEqual(totals["today"].days, 1)
        self.assertAlmostEqual(totals["month"].cost, 0.50 * 15)

    def test_cycle_start_rolls_back_a_month(self) -> None:
        totals = parser.aggregate([], self.tariff, NY, now=self.now, billing_day=20)
        self.assertEqual(totals["cycle"].start.day, 20)
        self.assertEqual(totals["cycle"].start.month, 12)
        self.assertEqual(totals["cycle"].days, 27)

    def test_latest_point(self) -> None:
        older = self._point(datetime(2026, 1, 15, 8, tzinfo=NY), 1.0)
        newer = self._point(datetime(2026, 1, 15, 9, tzinfo=NY), 1.0)
        self.assertEqual(parser.latest_point([older, newer]), newer)
        self.assertIsNone(parser.latest_point([]))


if __name__ == "__main__":
    unittest.main()
