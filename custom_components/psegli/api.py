"""Async client for the PSEG Long Island MySmartEnergy portal."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from datetime import date, timedelta
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
        self._login_blocked = ""
        self.update_cookie(cookie)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def can_login(self) -> bool:
        """Return True when stored credentials allow an automatic sign-in."""
        return bool(self._email and self._password)

    @property
    def login_blocked(self) -> str | None:
        """Return why automatic sign-in is paused, or None when it is not."""
        return self._login_blocked or None

    def update_cookie(self, cookie: str) -> None:
        """Replace the stored session cookie and load it into the jar."""
        self._stored_cookie = normalize_cookie(cookie)
        cookies = parse_cookie_header(self._stored_cookie)
        if cookies:
            # A fresh cookie is how a user gets past a captcha or 2FA wall.
            self._login_blocked = ""
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

    async def async_keep_alive(self) -> None:
        """Touch the dashboard so the portal's idle timer keeps the session.

        MyMeter has no heartbeat endpoint; the browser only keeps its own
        session alive because it keeps making requests.  A cheap authenticated
        request does the same for a session that no browser is using.
        """
        await self._async_dashboard_token()
        self._persist_cookie()

    async def async_login(self, *, force: bool = False) -> None:
        """Sign in with the stored username and password.

        ``force`` is only used by the diagnostics action, where the user asked
        for one deliberate attempt; polling always respects the cooldown.
        """
        if not self.can_login:
            raise PsegliLoginRequired(
                "No PSEG username and password are stored, so a new session "
                "cookie has to be pasted by hand"
            )
        if self._login_blocked:
            # Retrying only makes the captcha/2FA wall worse.
            raise PsegliLoginRequired(self._login_blocked)

        async with self._login_lock:
            if not force and self._cooldown_remaining():
                raise PsegliLoginRequired(
                    "PSEG rejected the session and a sign-in was already tried "
                    "in the last few minutes"
                )
            self._last_login_attempt = time.monotonic()
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
        return await self._async_chart(token, start, end)

    async def _async_chart(
        self, token: str, start: date, end: date
    ) -> list[UsagePoint]:
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

        _url, html, _content_type = await self._async_request(
            "GET", "/", headers=HTML_HEADERS, referer=f"{self._base_url}/"
        )

        # The portal's own JavaScript appends the page's antiforgery token to
        # every AJAX POST, login included (BaseJs.min.js processAjax).  Without
        # it PSEG answers the generic "invalid email address or password" even
        # when the credentials are right.
        token = extract_request_verification_token(html) or ""
        if not token:
            _LOGGER.debug("PSEG login page did not contain a verification token")

        form = {
            "__RequestVerificationToken": token,
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
            "POST",
            LOGIN_PATH,
            data=form,
            headers={**AJAX_HEADERS, "Origin": self._base_url},
            referer=f"{self._base_url}/",
        )

        try:
            payload = json.loads(text) if text.strip() else {}
        except ValueError:
            payload = {}

        if not isinstance(payload, Mapping):
            payload = {}
        try:
            self._raise_for_login_challenge(payload)
        except PsegliAuthError as err:
            _LOGGER.warning("PSEG did not accept the automatic sign-in: %s", err)
            raise

        # Trust only a dashboard that actually renders with the new cookies.
        try:
            await self._async_dashboard_token()
        except PsegliAuthError as err:
            raise self._login_blocked_error(
                "PSEG accepted the sign-in request but did not open a session. "
                "It is probably asking for a captcha or another confirmation, so "
                "automatic sign-in is paused until a fresh session cookie is "
                "supplied."
            ) from err

    def _cooldown_remaining(self) -> int:
        """Return how many seconds of the sign-in cooldown are left."""
        if not self._last_login_attempt:
            return 0
        remaining = LOGIN_COOLDOWN - (time.monotonic() - self._last_login_attempt)
        return max(0, int(remaining))

    def _login_blocked_error(self, reason: str) -> PsegliLoginRequired:
        """Remember that only a human can sign in, and build the error."""
        self._login_blocked = reason
        return PsegliLoginRequired(reason)

    def _raise_for_login_challenge(self, payload: Mapping[str, Any]) -> None:
        """Turn a sign-in response into a precise error for the user."""
        data = payload.get("Data")
        if not isinstance(data, Mapping):
            data = {}

        message = str(data.get("LoginErrorMessage") or "").strip()
        if message:
            if "captcha" in message.lower():
                raise self._login_blocked_error(
                    "PSEG asks for a captcha before it will sign in. That cannot "
                    "be answered automatically, so automatic sign-in is paused "
                    "until a fresh session cookie is supplied."
                )
            raise PsegliAuthError(f"PSEG rejected the sign-in: {message}")

        if _is_truthy(data.get("RecaptchaOnLogin")):
            raise self._login_blocked_error(
                "PSEG wants a reCAPTCHA for this sign-in. That cannot be "
                "answered automatically, so automatic sign-in is paused until a "
                "fresh session cookie is supplied."
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
                raise self._login_blocked_error(
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

    async def async_diagnose(self) -> dict[str, Any]:
        """Walk the sign-in and data path and report where it stops.

        The stored session is checked first and the login page is only fetched
        when it is already dead, so running this never disturbs a working
        session.  No cookie value or password appears in the result.
        """
        steps: list[dict[str, Any]] = []
        report: dict[str, Any] = {
            "base_url": self._base_url,
            "sign_in_method": "email and password" if self.can_login else "cookie",
            "stored_cookie_names": sorted(parse_cookie_header(self._stored_cookie)),
            "cooldown_seconds": self._cooldown_remaining(),
            "login_blocked": self.login_blocked,
            "steps": steps,
            "ok": False,
            "summary": "",
        }

        # 1. Does the stored session still open the dashboard?
        session_ok = False
        dashboard_token = ""
        try:
            dashboard_token = await self._async_dashboard_token()
        except PsegliAuthError as err:
            steps.append({"step": "open the dashboard", "ok": False, "error": str(err)})
        except PsegliError as err:
            steps.append({"step": "open the dashboard", "ok": False, "error": str(err)})
            report["summary"] = "The PSEG portal could not be reached."
            return report
        else:
            session_ok = True
            steps.append({"step": "open the dashboard", "ok": True})

        if not session_ok:
            # 2. The session is gone, so it is safe to load the login page and
            # check the antiforgery token a sign-in needs.
            try:
                _url, html, _content_type = await self._async_request(
                    "GET", "/", headers=HTML_HEADERS, referer=f"{self._base_url}/"
                )
            except PsegliError as err:
                steps.append({"step": "load the login page", "ok": False, "error": str(err)})
                report["summary"] = "The PSEG login page could not be reached."
                return report

            token = extract_request_verification_token(html)
            steps.append(
                {
                    "step": "load the login page",
                    "ok": token is not None,
                    "antiforgery_token_found": token is not None,
                }
            )

            if not self.can_login:
                steps.append(
                    {
                        "step": "sign in automatically",
                        "ok": False,
                        "error": "no username and password are stored",
                    }
                )
                report["summary"] = (
                    "The stored cookie is no longer accepted and no password is "
                    "stored. Paste a fresh cookie or switch to email and password."
                )
                return report

            # 3. One deliberate sign-in attempt.
            try:
                await self.async_login(force=True)
            except PsegliAuthError as err:
                steps.append(
                    {"step": "sign in automatically", "ok": False, "error": str(err)}
                )
                report["summary"] = f"PSEG rejected the sign-in: {err}"
                return report
            except PsegliError as err:
                steps.append(
                    {"step": "sign in automatically", "ok": False, "error": str(err)}
                )
                report["summary"] = "The sign-in request failed before PSEG answered."
                return report

            steps.append({"step": "sign in automatically", "ok": True})
            try:
                dashboard_token = await self._async_dashboard_token()
            except PsegliError as err:
                steps.append(
                    {
                        "step": "open the dashboard after signing in",
                        "ok": False,
                        "error": str(err),
                    }
                )
                report["summary"] = (
                    "PSEG accepted the sign-in but the dashboard still refused "
                    "the new session."
                )
                return report
            steps.append({"step": "open the dashboard after signing in", "ok": True})

        # 4. Can the chart endpoints be read?
        today = date.today()
        try:
            points = await self._async_chart(
                dashboard_token, today - timedelta(days=1), today
            )
        except PsegliError as err:
            steps.append({"step": "download chart data", "ok": False, "error": str(err)})
            report["summary"] = (
                "The session is accepted but the usage data could not be "
                f"downloaded: {err}"
            )
            return report

        steps.append(
            {
                "step": "download chart data",
                "ok": True,
                "intervals_returned": len(points),
            }
        )

        self._persist_cookie()
        report["ok"] = True
        report["summary"] = (
            "The session is alive and PSEG returned "
            f"{len(points)} intervals for the last day."
        )
        report["cookie_names"] = sorted(parse_cookie_header(self.current_cookie()))
        return report
