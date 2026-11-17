"""Tests for the session timeline summary.

``session_timeline`` deliberately has no Home Assistant imports, so the pruning
and the per-day/session-age summary can be tested directly.
"""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

COMPONENT_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "psegli"
NOW = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)


def _load() -> types.ModuleType:
    package = types.ModuleType("psegli_under_test")
    package.__path__ = [str(COMPONENT_DIR)]  # type: ignore[attr-defined]
    sys.modules.setdefault("psegli_under_test", package)
    module_name = "psegli_under_test.session_timeline"
    spec = importlib.util.spec_from_file_location(
        module_name, COMPONENT_DIR / "session_timeline.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


timeline = _load()


def event(
    kind: str,
    at: datetime,
    age: float | None = None,
    request: str = "GET /Dashboard",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "at": at.isoformat(),
        "kind": kind,
        "request": request,
        "reason": "PSEG redirected to the login page",
    }
    if age is not None:
        payload["session_age_minutes"] = age
    return payload


class SummarizeEventsTests(unittest.TestCase):
    def test_counts_rejections_per_utc_day(self) -> None:
        events = [
            event("rejected", datetime(2026, 11, 16, 23, 30, tzinfo=timezone.utc), 30.0),
            event("recovered", datetime(2026, 11, 16, 23, 31, tzinfo=timezone.utc)),
            event("rejected", datetime(2026, 11, 17, 0, 5, tzinfo=timezone.utc), 30.0),
            event("rejected", datetime(2026, 11, 17, 9, 0, tzinfo=timezone.utc), 31.0),
        ]

        summary = timeline.summarize_events(events, now=NOW)

        self.assertEqual(summary["rejections_by_day_utc"], {"2026-11-16": 1, "2026-11-17": 2})
        self.assertEqual(summary["rejections"], 3)
        self.assertEqual(summary["recoveries"], 1)
        self.assertEqual(summary["events"], 4)

    def test_reports_the_session_age_at_each_rejection(self) -> None:
        events = [
            event("rejected", datetime(2026, 11, 17, 6, 0, tzinfo=timezone.utc), 30.0),
            event("rejected", datetime(2026, 11, 17, 7, 0, tzinfo=timezone.utc), 31.0),
            event("rejected", datetime(2026, 11, 17, 8, 0, tzinfo=timezone.utc), 60.0),
            # A rejection without a measured age must not skew the numbers.
            event("rejected", datetime(2026, 11, 17, 8, 30, tzinfo=timezone.utc)),
        ]

        ages = timeline.summarize_events(events, now=NOW)[
            "session_age_at_rejection_minutes"
        ]

        self.assertEqual(ages["samples"], 3)
        self.assertEqual(ages["values"], [30.0, 31.0, 60.0])
        self.assertEqual(ages["min"], 30.0)
        self.assertEqual(ages["median"], 31.0)
        self.assertEqual(ages["max"], 60.0)
        self.assertEqual(ages["spread_minutes"], 30.0)
        self.assertFalse(ages["consistent_age"])

    def test_marks_a_consistent_session_age_as_suspicious(self) -> None:
        events = [
            event("rejected", datetime(2026, 11, 17, 4, 0, tzinfo=timezone.utc), 29.9),
            event("rejected", datetime(2026, 11, 17, 5, 0, tzinfo=timezone.utc), 30.1),
            event("rejected", datetime(2026, 11, 17, 6, 0, tzinfo=timezone.utc), 30.0),
        ]

        ages = timeline.summarize_events(events, now=NOW)[
            "session_age_at_rejection_minutes"
        ]

        self.assertTrue(ages["consistent_age"])
        self.assertEqual(ages["median"], 30.0)

    def test_a_single_sample_is_not_called_consistent(self) -> None:
        events = [
            event("rejected", datetime(2026, 11, 17, 6, 0, tzinfo=timezone.utc), 30.0)
        ]

        ages = timeline.summarize_events(events, now=NOW)[
            "session_age_at_rejection_minutes"
        ]

        self.assertEqual(ages["samples"], 1)
        self.assertEqual(ages["spread_minutes"], None)
        self.assertFalse(ages["consistent_age"])

    def test_stale_and_malformed_events_are_ignored(self) -> None:
        events = [
            event("rejected", NOW - timedelta(days=30), 30.0),
            {"at": "not a timestamp", "kind": "rejected", "session_age_minutes": 5},
            {"kind": "rejected", "session_age_minutes": 5},
            "nonsense",
            event("rejected", NOW - timedelta(days=1), 30.0),
        ]

        summary = timeline.summarize_events(events, now=NOW)

        self.assertEqual(summary["events"], 1)
        self.assertEqual(summary["rejections"], 1)
        self.assertEqual(summary["first_event"], summary["last_event"])

    def test_empty_timeline_reports_zeroes(self) -> None:
        summary = timeline.summarize_events([], now=NOW)

        self.assertEqual(summary["events"], 0)
        self.assertEqual(summary["rejections_by_day_utc"], {})
        self.assertIsNone(summary["first_event"])
        self.assertIsNone(summary["session_age_at_rejection_minutes"]["median"])
        self.assertFalse(summary["session_age_at_rejection_minutes"]["consistent_age"])

    def test_pruning_keeps_only_the_newest_entries(self) -> None:
        events = [
            event("rejected", NOW - timedelta(minutes=5 * index), float(index))
            for index in range(timeline.MAX_EVENTS + 25)
        ]

        kept = timeline.prune_events(events, now=NOW)

        self.assertEqual(len(kept), timeline.MAX_EVENTS)
        self.assertEqual(kept[-1]["at"], events[0]["at"])


if __name__ == "__main__":
    unittest.main()
