"""Pure helpers for the stored PSEG session timeline.

No Home Assistant imports live here, so the pruning and the summary can be unit
tested.  The storage wrapper is in ``session_log.py``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

#: A week is enough to see a daily or half-hourly pattern, and small enough to
#: keep the stored file trivial.
MAX_AGE = timedelta(days=7)
MAX_EVENTS = 200

#: Ages this close together look like a server-side session cap rather than
#: requests being bounced at random.
CONSISTENT_AGE_SPREAD_MINUTES = 5.0


def parse_timestamp(value: Any) -> datetime | None:
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
    kept: list[tuple[datetime, dict[str, Any]]] = []
    for event in events:
        if not isinstance(event, Mapping):
            continue
        stamp = parse_timestamp(event.get("at"))
        if stamp is None or stamp < cutoff:
            continue
        kept.append((stamp, dict(event)))
    # Sort by the event's own timestamp rather than trusting the file order, so
    # an edited or interleaved timeline still keeps the newest entries.
    kept.sort(key=lambda item: item[0])
    return [event for _stamp, event in kept][-MAX_EVENTS:]


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    middle = len(values) // 2
    if len(values) % 2:
        return round(values[middle], 1)
    return round((values[middle - 1] + values[middle]) / 2, 1)


def summarize_events(
    events: Iterable[Any], *, now: datetime | None = None
) -> dict[str, Any]:
    """Summarise the stored timeline: rejections per day and the session age.

    ``session_age_minutes`` is how long the session had been in use when the
    rejection happened.  When that number is the same at every failure while
    the keep-alive is working, the portal is ending the session on a timer, not
    the integration losing the cookie.
    """
    now = now or datetime.now(timezone.utc)
    kept = prune_events(events, now=now)
    rejections = [event for event in kept if event.get("kind") == "rejected"]
    recoveries = [event for event in kept if event.get("kind") == "recovered"]

    by_day: dict[str, int] = {}
    for event in rejections:
        stamp = parse_timestamp(event.get("at"))
        if stamp is None:
            continue
        key = stamp.astimezone(timezone.utc).date().isoformat()
        by_day[key] = by_day.get(key, 0) + 1

    ages = sorted(
        float(age)
        for age in (event.get("session_age_minutes") for event in rejections)
        if isinstance(age, (int, float)) and not isinstance(age, bool)
    )
    spread = round(ages[-1] - ages[0], 1) if len(ages) >= 2 else None

    return {
        "generated_at": now.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "window_days": MAX_AGE.days,
        "events": len(kept),
        "rejections": len(rejections),
        "recoveries": len(recoveries),
        "rejections_by_day_utc": dict(sorted(by_day.items())),
        "session_age_at_rejection_minutes": {
            "samples": len(ages),
            "values": ages,
            "min": ages[0] if ages else None,
            "median": _median(ages),
            "max": ages[-1] if ages else None,
            "spread_minutes": spread,
            "consistent_age": (
                len(ages) >= 3 and spread is not None and spread <= CONSISTENT_AGE_SPREAD_MINUTES
            ),
        },
        "first_event": kept[0].get("at") if kept else None,
        "last_event": kept[-1].get("at") if kept else None,
    }
