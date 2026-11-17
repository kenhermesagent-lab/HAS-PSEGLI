"""Async client for the PSEG Long Island MySmartEnergy portal."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from datetime import date
from typing import Any, Callable, Mapping, MutableMapping
from urllib.parse import urlparse

import aiohttp
from yarl import URL

from .const import BASE_URL, CHART_DATA_PATH, CHART_PATH, DASHBOARD_PATH, LOGIN_PATH
from .exceptions import (
    PsegliAuthError,
    PsegliConnectionError,
    PsegliError,
    PsegliLoginRequired,
)
from .parser import UsagePoint, extract_request_verification_token, parse_chart_payload

_LOGGER = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
)
TIMEOUT = aiohttp.ClientTimeout(total=90)

#: Minimum time between two sign-in attempts so a broken password never turns
#: into a stream of failed logins (which is how utility portals lock accounts).
LOGIN_COOLDOWN = 15 * 60

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


def parse_cookie_header(cookie: str) -> dict[str, str]:
    """Split a Cookie header into a name -> value mapping."""
    cookies: dict[str, str] = {}
    for part in normalize_cookie(cookie).split(";"):
        name, separator, value = part.partition("=")
        name = name.strip()
        if not separator or not name:
            continue
        cookies[name] = value.strip()
    return cookies


def build_cookie_header(cookies: Mapping[str, str]) -> str:
    """Build a Cookie header value from a mapping."""
    return "; ".join(f"{name}={value}" for name, value in sorted(cookies.items()))


def _is_truthy(value: Any) -> bool:
    """Interpret the loose booleans the MyMeter JSON payloads use."""
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value)


class PsegliClient:
    """Talk to the MySmartEnergy portal.

    The client keeps the session in the cookie jar of the aiohttp session it is
    given, so cookies the portal rotates on every response are kept and can be
    stored back into the config entry.  When a username and password are
    available it signs in again on its own instead of asking the user to paste
    a fresh cookie.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        cookie: str = "",
        *,
        email: str | None = None,
        password: str | None = None,
        base_url: str = BASE_URL,
        timestamp_offset_minutes: int = 0,
        on_cookie_refresh: Callable[[str], None] | None = None,
    ) -> None:
        if isinstance(session.cookie_jar, aiohttp.DummyCookieJar):
            raise ValueError("PsegliClient needs a session with a real cookie jar")

        self._session = session
        self._base_url = base_url.rstrip("/")
        self._email = (email or "").strip() or None
        self._password = password or None
        self._on_cookie_refresh = on_cookie_refresh
        self._offset = int(timestamp_offset_minutes or 0)
        self._stored_cookie = ""
        self._login_lock = asyncio.Lock()
        self._last_login_attempt = 0.0
        self.update_cookie(cookie)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def can_login(self) -> bool:
        """Return True when stored credentials allow an automatic sign-in."""
        return bool(self._email and self._password)

    def update_cookie(self, cookie: str) -> None:
        """Replace the stored session cookie and load it into the jar."""
        self._stored_cookie = normalize_cookie(cookie)
        cookies = parse_cookie_header(self._stored_cookie)
        if cookies:
            self._session.cookie_jar.update_cookies(
                cookies, response_url=URL(f"{self._base_url}/")
            )

    def update_timestamp_offset(self, offset_minutes: int) -> None:
        """Update the correction applied to portal timestamps."""
        self._offset = int(offset_minutes or 0)

    def current_cookie(self) -> str:
        """Return the cookies the portal currently holds for this session."""
        try:
            filtered = self._session.cookie_jar.filter_cookies(
                URL(f"{self._base_url}/")
            )
        except (AttributeError, KeyError, ValueError):  # pragma: no cover - safety
            return self._stored_cookie
        return build_cookie_header({key: morsel.value for key, morsel in filtered.items()})

    async def async_validate(self) -> None:
        """Check that the current session still opens the dashboard."""
        await self._async_dashboard_token()
        self._persist_cookie()

    async def async_login(self) -> None:
        """Sign in with the stored username and password."""
        if not self.can_login:
            raise PsegliLoginRequired(
                "No PSEG username and password are stored, so a new session "
                "cookie has to be pasted by hand"
            )

        async with self._login_lock:
            now = time.monotonic()
            if (
                self._last_login_attempt
                and now - self._last_login_attempt < LOGIN_COOLDOWN
            ):
                raise PsegliLoginRequired(
                    "PSEG rejected the session and a sign-in was already tried "
                    "in the last few minutes"
                )
            self._last_login_attempt = now
            await self._async_perform_login()
            self._persist_cookie(force=True)

    async def async_fetch_points(self, start: date, end: date) -> list[UsagePoint]:
        """Fetch hourly usage between two local dates (inclusive)."""
        try:
            points = await self._async_fetch_points(start, end)
        except PsegliAuthError:
            if not self.can_login:
                raise
            _LOGGER.info("PSEG session expired, signing in again automatically")
            await self.async_login()
            points = await self._async_fetch_points(start, end)
        # The portal rotates its session cookies while we browse; keep the
        # stored copy current so a restart does not need a new sign-in.
        self._persist_cookie()
        return points

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _async_fetch_points(self, start: date, end: date) -> list[UsagePoint]:
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

    async def _async_perform_login(self) -> None:
        """Load the login page, post the credentials and verify the session."""
        # Start from a clean jar: a half-expired session cookie confuses the
        # portal's sign-in handler.  CookieJar.clear() is a plain call in newer
        # aiohttp releases and a coroutine in older ones.
        cleared = self._session.cookie_jar.clear()
        if inspect.isawaitable(cleared):
            await cleared
        self._stored_cookie = ""

        await self._async_request("GET", "/", headers=HTML_HEADERS, referer=None)

        form = {
            "LoginEmail": self._email or "",
            "LoginPassword": self._password or "",
            "RedirectUrl": "",
            "LoginErrorMessage": "",
            "ExternalLogin": "False",
            "TwoFactorRendered": "False",
            "SecretQuestionRendered": "False",
            "RememberMe": "true",
        }
        _url, text, _content_type = await self._async_request(
            "POST", LOGIN_PATH, data=form, headers=AJAX_HEADERS, referer=None
        )

        try:
            payload = json.loads(text) if text.strip() else {}
        except ValueError:
            payload = {}

        if not isinstance(payload, Mapping):
            payload = {}
        self._raise_for_login_challenge(payload)

        # Trust only a dashboard that actually renders with the new cookies.
        await self._async_dashboard_token()

    @staticmethod
    def _raise_for_login_challenge(payload: Mapping[str, Any]) -> None:
        """Turn a sign-in response into a precise error for the user."""
        data = payload.get("Data")
        if not isinstance(data, Mapping):
            data = {}

        message = str(data.get("LoginErrorMessage") or "").strip()
        if message:
            raise PsegliAuthError(f"PSEG rejected the sign-in: {message}")

        if _is_truthy(data.get("RecaptchaOnLogin")):
            raise PsegliLoginRequired(
                "PSEG wants a reCAPTCHA for this sign-in. Sign in by hand and "
                "paste a session cookie instead."
            )

        for key, reason in (
            ("SecretQuestionError", "a security question"),
            ("SecretQuestionModalData", "a security question"),
            ("SecretQuestionRendered", "a security question"),
            ("TwoFactorError", "a verification code"),
            ("TwoFactorModalData", "a verification code"),
            ("TwoFactorRequired", "a verification code"),
            ("TwoFactorRendered", "a verification code"),
            ("PasswordExpiredModalData", "a password change"),
            ("PasswordExpired", "a password change"),
        ):
            if _is_truthy(data.get(key)):
                raise PsegliLoginRequired(
                    f"PSEG asks for {reason} during sign-in, which cannot be "
                    "answered automatically. Sign in by hand and paste a "
                    "session cookie instead."
                )

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
        referer: str | None = f"{BASE_URL}{DASHBOARD_PATH}",
    ) -> tuple[str, str, str]:
        """Perform one request and return (final URL, body, content type)."""
        request_headers = {
            **headers,
            "User-Agent": USER_AGENT,
        }
        if referer is not None:
            request_headers["Referer"] = referer

        try:
            async with self._session.request(
                method,
                f"{self._base_url}{path}",
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

    def _persist_cookie(self, force: bool = False) -> None:
        """Hand rotated cookies back so they survive a Home Assistant restart."""
        cookie = self.current_cookie()
        if not cookie or (cookie == self._stored_cookie and not force):
            return
        self._stored_cookie = cookie
        if self._on_cookie_refresh is None:
            return
        try:
            self._on_cookie_refresh(cookie)
        except Exception:  # noqa: BLE001 - persistence must never break polling
            _LOGGER.debug("Could not store the refreshed PSEG cookie", exc_info=True)

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
            if not isinstance(payload, MutableMapping):
                return
            for item in (payload or {}).get("AjaxResults") or []:
                if str(item.get("Action", "")).lower() == "redirect":
                    raise PsegliAuthError("PSEG rejected the session cookie")
