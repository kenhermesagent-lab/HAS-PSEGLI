"""Diagnostics support for the PSEG Long Island integration."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .api import parse_cookie_header
from .const import (
    AUTH_MODE_COOKIE,
    CONF_AUTH_MODE,
    CONF_COOKIE,
    CONF_EMAIL,
    CONF_PASSWORD,
    COST_STATISTIC_ID,
    USAGE_STATISTIC_ID,
)
from .session_log import SessionLog
from .session_timeline import summarize_events
from .statistics import async_statistics_coverage


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return a redacted snapshot of the entry.

    Cookie values and the stored password never appear here; only the cookie
    names and whether a credential exists at all.
    """
    report: dict[str, Any] = {
        "entry": {
            "auth_mode": entry.data.get(CONF_AUTH_MODE, AUTH_MODE_COOKIE),
            "email_stored": bool(entry.data.get(CONF_EMAIL)),
            "password_stored": bool(entry.data.get(CONF_PASSWORD)),
            "cookie_stored": bool(entry.data.get(CONF_COOKIE)),
            "cookie_names": sorted(
                parse_cookie_header(entry.data.get(CONF_COOKIE, ""))
            ),
            "options": dict(entry.options),
        },
        "statistics": {
            "usage": await async_statistics_coverage(hass, USAGE_STATISTIC_ID),
            "cost": await async_statistics_coverage(hass, COST_STATISTIC_ID),
        },
    }

    coordinator = getattr(entry, "runtime_data", None)
    # The timeline lives on disk, so it is readable even when setup failed -
    # which is exactly when the diagnostics are worth downloading.
    session_log = getattr(coordinator, "session_log", None) or SessionLog(
        hass, entry.entry_id
    )
    report["session_log"] = await session_log.async_events()
    report["session_summary"] = summarize_events(report["session_log"])

    if coordinator is None:
        report["session"] = {"available": False}
        return report

    data = coordinator.data
    report["session"] = {
        "sign_in_method": (
            "email and password" if coordinator.client.can_login else "cookie"
        ),
        "cookie_names": sorted(
            parse_cookie_header(coordinator.client.current_cookie())
        ),
        "last_update_success": coordinator.last_update_success,
        "last_exception": (
            str(coordinator.last_exception)
            if coordinator.last_exception is not None
            else None
        ),
        "update_interval_minutes": (
            int(coordinator.update_interval.total_seconds() // 60)
            if coordinator.update_interval is not None
            else None
        ),
        "consecutive_failed_polls": coordinator.consecutive_auth_failures,
        "session_age_minutes": coordinator.client.session_age_minutes,
        "keep_alive": coordinator.client.keep_alive_stats,
        "cookie_expiry": coordinator.client.cookie_expiry(),
        "recent_auth_failures": coordinator.client.recent_auth_failures,
        "session_events_stored": len(report["session_log"]),
    }
    report["data"] = {
        "intervals": len(data.points) if data else 0,
        "latest_interval": (
            data.latest.start.isoformat() if data and data.latest else None
        ),
        "periods": sorted(data.aggregates) if data else [],
    }
    return report
