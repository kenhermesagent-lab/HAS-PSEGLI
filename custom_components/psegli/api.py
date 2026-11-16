"""Async client for the PSEG Long Island MySmartEnergy portal."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import date
from typing import Any
from urllib.parse import urlparse

import aiohttp

from .const import BASE_URL, CHART_DATA_PATH, CHART_PATH, DASHBOARD_PATH
from .exceptions import PsegliAuthError, PsegliConnectionError, PsegliError
from .parser import UsagePoint, extract_request_verification_token, parse_chart_payload

_LOGGER = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
)
TIMEOUT = aiohttp.ClientTimeout(total=90)

HTML_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
}
AJAX_HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "en-US,en;q=0.9",
    "X-Requested-With": "XMLHttpRequest",
}


def normalize_cookie(cookie: str) -> str:
    """Accept a raw Cookie header or a pasted value and normalise whitespace."""
    value = (cookie or "").strip()
    if value.lower().startswith("cookie:"):
        value = value[len("cookie:") :].strip()
    return " ".join(value.split())


class PsegliClient:
    """Talk to the MySmartEnergy portal with a browser session cookie."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        cookie: str,
        timestamp_offset_minutes: int = 0,
    ) -> None:
        self._session = session
        self._cookie = normalize_cookie(cookie)
        self._offset = int(timestamp_offset_minutes or 0)

    def update_cookie(self, cookie: str) -> None:
        """Replace the stored session cookie."""
        self._cookie = normalize_cookie(cookie)

    def update_timestamp_offset(self, offset_minutes: int) -> None:
        """Update the correction applied to portal timestamps."""
        self._offset = int(offset_minutes or 0)

    async def async_validate(self) -> None:
        """Check that the cookie still opens the dashboard."""
        await self._async_dashboard_token()

    async def async_fetch_points(self, start: date, end: date) -> list[UsagePoint]:
        """Fetch hourly usage between two local dates (inclusive)."""
        token = await self._async_dashboard_token()

        form = {
            "__RequestVerificationToken": token,
            "UsageInterval": "5",  # hourly
            "UsageType": "1",  # consumption
            "jsTargetName": "StorageType",
            "EnableHoverChart": "true",
            "Start": start.isoformat(),
            "End": end.isoformat(),
            "IsRangeOpen": "False",
            "MaintainMaxDate": "true",
            "SelectedViaDateRange": "False",
            "ChartComparison": "1",
            "ChartComparison2": "0",
            "ChartComparison3": "0",
            "ChartComparison4": "0",
        }
        url, text, content_type = await self._async_request(
            "POST", CHART_PATH, data=form, headers=AJAX_HEADERS
        )
        self._assert_logged_in(url, text, content_type)
        if text.strip() and "json" in content_type:
            try:
                payload = json.loads(text)
            except ValueError:
                payload = None
            for item in (payload or {}).get("AjaxResults") or []:
                if str(item.get("Action", "")).lower() == "redirect":
                    raise PsegliAuthError("PSEG rejected the session cookie")

        cache_buster = str(int(time.time() * 1000))
        url, text, content_type = await self._async_request(
            "GET",
            CHART_DATA_PATH,
            params={"_": cache_buster},
            headers=AJAX_HEADERS,
        )
        self._assert_logged_in(url, text, content_type)

        try:
            payload = json.loads(text)
        except ValueError as err:
            raise PsegliError(
                "PSEG returned an unexpected response instead of chart data"
            ) from err

        return parse_chart_payload(payload, self._offset)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _async_dashboard_token(self) -> str:
        url, text, content_type = await self._async_request(
            "GET", DASHBOARD_PATH, headers=HTML_HEADERS
        )
        self._assert_logged_in(url, text, content_type)
        token = extract_request_verification_token(text)
        if not token:
            raise PsegliAuthError(
                "The PSEG dashboard loaded but no session token was found. "
                "The cookie is probably incomplete or expired."
            )
        return token

    async def _async_request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        params: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
    ) -> tuple[str, str, str]:
        """Perform one request and return (final URL, body, content type)."""
        request_headers = {
            **headers,
            "Cookie": self._cookie,
            "Referer": f"{BASE_URL}{DASHBOARD_PATH}",
            "User-Agent": USER_AGENT,
        }
        try:
            async with self._session.request(
                method,
                f"{BASE_URL}{path}",
                headers=request_headers,
                params=params,
                data=data,
                allow_redirects=True,
                timeout=TIMEOUT,
            ) as response:
                body = await response.text()
                if response.status in (401, 403):
                    raise PsegliAuthError(
                        f"PSEG denied the request (HTTP {response.status})"
                    )
                if response.status >= 400:
                    raise PsegliError(f"PSEG returned HTTP {response.status}")
                return (
                    str(response.url),
                    body,
                    response.headers.get("Content-Type", ""),
                )
        except PsegliError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise PsegliConnectionError(f"Could not reach PSEG: {err}") from err

    @staticmethod
    def _assert_logged_in(url: str, text: str, content_type: str) -> None:
        """Raise when the portal answered with a login page or AJAX redirect."""
        path = urlparse(url).path.lower()
        if "login" in path or path in ("", "/"):
            raise PsegliAuthError("PSEG redirected to the login page")
        if "loginemail" in (text or "").lower():
            raise PsegliAuthError("PSEG returned the login page")
        if "json" in content_type:
            try:
                payload: Any = json.loads(text)
            except ValueError:
                return
            for item in (payload or {}).get("AjaxResults") or []:
                if str(item.get("Action", "")).lower() == "redirect":
                    raise PsegliAuthError("PSEG rejected the session cookie")
