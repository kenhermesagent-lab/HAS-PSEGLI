"""Offline tests for the PSEG client's sign-in and cookie handling.

A tiny fake portal speaks the same subset of the MyMeter protocol the client
uses (form login returning JSON, rotating session cookie, dashboard token and
chart data), so the session logic can be exercised without PSEG.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

COMPONENT_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "psegli"

EMAIL = "user@example.com"
PASSWORD = "correct horse battery"
TOKEN = "token-abc"

LOGIN_HTML = (
    '<html><form action="/Home/Login">'
    '<input id="LoginEmail" name="LoginEmail" />'
    f'<input name="__RequestVerificationToken" type="hidden" value="{TOKEN}" />'
    "</form></html>"
)
DASHBOARD_HTML = (
    f'<html><input name="__RequestVerificationToken" value="{TOKEN}" /></html>'
)


def _load_component() -> None:
    """Import the component modules that do not need Home Assistant."""
    package = types.ModuleType("psegli_under_test")
    package.__path__ = [str(COMPONENT_DIR)]  # type: ignore[attr-defined]
    sys.modules["psegli_under_test"] = package
    for name in ("parser", "exceptions", "const", "api"):
        module_name = f"psegli_under_test.{name}"
        if module_name in sys.modules:
            continue
        spec = importlib.util.spec_from_file_location(
            module_name, COMPONENT_DIR / f"{name}.py"
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)


_load_component()

api = sys.modules["psegli_under_test.api"]
exceptions = sys.modules["psegli_under_test.exceptions"]


class FakePortal:
    """Minimal MySmartEnergy stand-in."""

    def __init__(self) -> None:
        self.valid_sessions: set[str] = set()
        self.login_attempts = 0
        self.challenge: dict[str, Any] | None = None
        self.rotate_session = True
        self.last_login_form: dict[str, Any] = {}
        self.last_login_headers: dict[str, str] = {}
        self.captcha_message: str | None = None
        self.accept_login_without_session = False
        self.fail_chart_once = False
        self._counter = 0

    def new_session(self) -> str:
        self._counter += 1
        sid = f"sess-{self._counter}"
        self.valid_sessions.add(sid)
        return sid

    def _logged_in(self, request: web.Request) -> bool:
        return request.cookies.get("MM_SID") in self.valid_sessions

    async def root(self, request: web.Request) -> web.Response:
        response = web.Response(text=LOGIN_HTML, content_type="text/html")
        response.set_cookie("MM_SID", "anonymous")
        return response

    async def login_page(self, request: web.Request) -> web.Response:
        return web.Response(text=LOGIN_HTML, content_type="text/html")

    async def login(self, request: web.Request) -> web.Response:
        form = await request.post()
        self.login_attempts += 1
        self.last_login_form = dict(form)
        self.last_login_headers = dict(request.headers)
        # The real portal's JavaScript always submits the page's antiforgery
        # token with the login POST.
        if form.get("__RequestVerificationToken") != TOKEN:
            return web.json_response(
                {
                    "AjaxResults": [],
                    "Data": {"LoginErrorMessage": "Invalid email address or password"},
                }
            )
        if self.accept_login_without_session:
            # PSEG answers the POST as if it worked but never opens a session.
            return web.json_response({"AjaxResults": [], "Data": {}})
        if self.captcha_message is not None:
            return web.json_response(
                {
                    "AjaxResults": [],
                    "Data": {"LoginErrorMessage": self.captcha_message},
                }
            )
        if self.challenge is not None:
            return web.json_response({"AjaxResults": [], "Data": self.challenge})
        if (
            form.get("LoginEmail") == EMAIL
            and form.get("LoginPassword") == PASSWORD
        ):
            response = web.json_response({"AjaxResults": [], "Data": {}})
            response.set_cookie("MM_SID", self.new_session())
            return response
        return web.json_response(
            {
                "AjaxResults": [],
                "Data": {"LoginErrorMessage": "Invalid email address or password"},
            }
        )

    async def dashboard(self, request: web.Request) -> web.StreamResponse:
        if not self._logged_in(request):
            raise web.HTTPFound("/Home/Login")
        return web.Response(text=DASHBOARD_HTML, content_type="text/html")

    async def chart(self, request: web.Request) -> web.Response:
        if not self._logged_in(request):
            raise web.HTTPFound("/Home/Login")
        if self.fail_chart_once:
            # A busy portal answers one AJAX request with a login redirect even
            # though the session is fine.
            self.fail_chart_once = False
            raise web.HTTPFound("/Home/Login")
        await request.post()
        response = web.json_response({"AjaxResults": [], "Data": {}})
        if self.rotate_session:
            response.set_cookie("MM_SID", self.new_session())
        return response

    async def chart_data(self, request: web.Request) -> web.StreamResponse:
        if not self._logged_in(request):
            raise web.HTTPFound("/Home/Login")
        timestamp = int(
            datetime(2026, 1, 10, 15, tzinfo=timezone.utc).timestamp() * 1000
        )
        return web.json_response(
            {
                "Data": {
                    "series": [
                        {"name": "Usage", "data": [{"x": timestamp, "y": 1.5}]}
                    ]
                }
            }
        )

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/", self.root)
        app.router.add_get("/Home/Login", self.login_page)
        app.router.add_post("/Home/Login", self.login)
        app.router.add_get("/Dashboard", self.dashboard)
        app.router.add_post("/Dashboard/Chart", self.chart)
        app.router.add_get("/Dashboard/ChartData", self.chart_data)
        return app


class PsegliClientTests(unittest.IsolatedAsyncioTestCase):
    """Exercise the client against the fake portal."""

    portal: FakePortal
    session: aiohttp.ClientSession
    base_url: str
    refreshed: list[str]
    events: list[dict[str, Any]]

    async def asyncSetUp(self) -> None:
        self.portal = FakePortal()
        self._runner = web.AppRunner(self.portal.app())
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        port = self._runner.addresses[0][1]
        self.base_url = f"http://127.0.0.1:{port}"
        self.session = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True))
        self.refreshed = []
        self.events = []

    async def asyncTearDown(self) -> None:
        await self.session.close()
        await self._runner.cleanup()

    def client(
        self,
        cookie: str = "",
        *,
        email: str | None = None,
        password: str | None = None,
    ) -> Any:
        return api.PsegliClient(
            self.session,
            cookie,
            email=email,
            password=password,
            base_url=self.base_url,
            on_cookie_refresh=self.refreshed.append,
            on_session_event=self.events.append,
        )

    async def fetch(self, client: Any) -> list[Any]:
        return await client.async_fetch_points(
            date(2026, 1, 10), date(2026, 1, 11)
        )

    async def test_login_replaces_expired_session_and_stores_cookies(self) -> None:
        client = self.client("MM_SID=stale", email=EMAIL, password=PASSWORD)

        points = await self.fetch(client)

        self.assertEqual(len(points), 1)
        self.assertAlmostEqual(points[0].kwh, 1.5)
        self.assertEqual(self.portal.login_attempts, 1)
        self.assertTrue(self.refreshed)
        self.assertIn("MM_SID=", self.refreshed[-1])
        self.assertNotIn("stale", client.current_cookie())

    async def test_valid_cookie_does_not_sign_in(self) -> None:
        sid = self.portal.new_session()
        client = self.client(f"MM_SID={sid}", email=EMAIL, password=PASSWORD)

        await self.fetch(client)

        self.assertEqual(self.portal.login_attempts, 0)

    async def test_rotated_cookie_is_reused_without_signing_in(self) -> None:
        sid = self.portal.new_session()
        client = self.client(f"MM_SID={sid}", email=EMAIL, password=PASSWORD)

        await self.fetch(client)
        first = client.current_cookie()
        await self.fetch(client)

        self.assertEqual(self.portal.login_attempts, 0)
        self.assertNotEqual(first, f"MM_SID={sid}")
        self.assertIn(client.current_cookie().split("MM_SID=")[1], self.portal.valid_sessions)

    async def test_captcha_pauses_further_sign_in_attempts(self) -> None:
        self.portal.captcha_message = "Please provide a valid login captcha"
        client = self.client(email=EMAIL, password=PASSWORD)

        with self.assertRaises(exceptions.PsegliLoginRequired) as first:
            await client.async_login()
        with self.assertRaises(exceptions.PsegliLoginRequired):
            await client.async_login()

        self.assertIn("captcha", str(first.exception).lower())
        self.assertIn("paused", str(first.exception).lower())
        # The second call must not touch the portal at all.
        self.assertEqual(self.portal.login_attempts, 1)

    async def test_a_fresh_cookie_clears_the_captcha_block(self) -> None:
        self.portal.captcha_message = "Please provide a valid login captcha"
        client = self.client(email=EMAIL, password=PASSWORD)
        with self.assertRaises(exceptions.PsegliLoginRequired):
            await client.async_login()
        self.assertIsNotNone(client.login_blocked)

        client.update_cookie(f"MM_SID={self.portal.new_session()}")
        self.assertIsNone(client.login_blocked)
        points = await self.fetch(client)

        self.assertEqual(len(points), 1)
        self.assertEqual(self.portal.login_attempts, 1)

    async def test_sign_in_that_opens_no_session_asks_for_a_human(self) -> None:
        self.portal.accept_login_without_session = True
        client = self.client(email=EMAIL, password=PASSWORD)

        with self.assertRaises(exceptions.PsegliLoginRequired) as raised:
            await client.async_login()

        self.assertIn("did not open a session", str(raised.exception))

    async def test_wrong_password_raises_auth_error(self) -> None:
        client = self.client(email=EMAIL, password="wrong password")

        with self.assertRaises(exceptions.PsegliAuthError) as raised:
            await client.async_login()

        self.assertNotIsInstance(raised.exception, exceptions.PsegliLoginRequired)
        self.assertIn("Invalid email address", str(raised.exception))

    async def test_security_question_needs_a_human(self) -> None:
        self.portal.challenge = {"SecretQuestionRendered": "True"}
        client = self.client(email=EMAIL, password=PASSWORD)

        with self.assertRaises(exceptions.PsegliLoginRequired):
            await client.async_login()

    async def test_visual_challenge_needs_a_human(self) -> None:
        self.portal.challenge = {"RecaptchaOnLogin": True}
        client = self.client(email=EMAIL, password=PASSWORD)

        with self.assertRaises(exceptions.PsegliLoginRequired):
            await client.async_login()

    async def test_failed_sign_in_is_not_retried_immediately(self) -> None:
        client = self.client(email=EMAIL, password="wrong password")

        with self.assertRaises(exceptions.PsegliAuthError):
            await client.async_login()
        with self.assertRaises(exceptions.PsegliLoginRequired):
            await client.async_login()

        self.assertEqual(self.portal.login_attempts, 1)

    async def test_expired_session_without_credentials_asks_for_a_cookie(self) -> None:
        client = self.client("MM_SID=stale")

        with self.assertRaises(exceptions.PsegliAuthError):
            await self.fetch(client)

        self.assertEqual(self.portal.login_attempts, 0)

    async def test_login_posts_the_antiforgery_token_like_the_portal(self) -> None:
        client = self.client(email=EMAIL, password=PASSWORD)

        await client.async_login()

        self.assertEqual(
            self.portal.last_login_form.get("__RequestVerificationToken"), TOKEN
        )
        self.assertEqual(self.portal.last_login_form.get("RememberMe"), "true")
        self.assertEqual(self.portal.last_login_headers.get("Origin"), self.base_url)
        self.assertTrue(
            self.portal.last_login_headers.get("Referer", "").startswith(self.base_url)
        )

    async def test_keep_alive_keeps_a_valid_session(self) -> None:
        sid = self.portal.new_session()
        client = self.client(f"MM_SID={sid}")

        await client.async_keep_alive()

        self.assertEqual(self.portal.login_attempts, 0)

    async def test_keep_alive_reports_a_dead_session_without_signing_in(self) -> None:
        client = self.client("MM_SID=stale", email=EMAIL, password=PASSWORD)

        with self.assertRaises(exceptions.PsegliAuthError):
            await client.async_keep_alive()

        self.assertEqual(self.portal.login_attempts, 0)

    async def test_one_bounced_data_request_does_not_end_the_session(self) -> None:
        self.portal.fail_chart_once = True
        client = self.client(f"MM_SID={self.portal.new_session()}")

        points = await self.fetch(client)

        self.assertEqual(len(points), 1)
        self.assertEqual(self.portal.login_attempts, 0)
        self.assertEqual(client.consecutive_auth_failures, 0)
        self.assertEqual(
            client.recent_auth_failures[-1]["request"], "POST /Dashboard/Chart"
        )

    async def test_a_dead_cookie_records_the_rejection(self) -> None:
        client = self.client("MM_SID=stale")

        with self.assertRaises(exceptions.PsegliAuthError):
            await self.fetch(client)

        self.assertIsNone(client.session_age_minutes)
        self.assertTrue(client.recent_auth_failures)
        self.assertEqual(
            client.recent_auth_failures[-1]["request"], "GET /Dashboard"
        )
        self.assertIn("login page", client.recent_auth_failures[-1]["reason"])
        self.assertNotIn("stale", json.dumps(client.recent_auth_failures))

    async def test_keep_alive_records_success_and_failure(self) -> None:
        client = self.client(f"MM_SID={self.portal.new_session()}")

        await client.async_keep_alive()

        self.assertIsNotNone(client.keep_alive_stats["last_ok"])
        self.assertIsNotNone(client.session_age_minutes)
        self.assertIn("MM_SID", client.cookie_expiry())

        self.portal.valid_sessions.clear()
        with self.assertRaises(exceptions.PsegliAuthError):
            await client.async_keep_alive()

        self.assertEqual(client.keep_alive_stats["consecutive_failures"], 1)

    async def test_session_age_restarts_only_with_a_new_session(self) -> None:
        client = self.client(f"MM_SID={self.portal.new_session()}")

        await self.fetch(client)
        age = client.session_age_minutes
        await self.fetch(client)

        self.assertIsNotNone(age)
        self.assertGreaterEqual(client.session_age_minutes, age)

    async def test_a_recovered_session_is_recorded(self) -> None:
        self.portal.fail_chart_once = True
        client = self.client(f"MM_SID={self.portal.new_session()}")

        await self.fetch(client)

        self.assertEqual(
            [event["kind"] for event in self.events], ["rejected", "recovered"]
        )
        self.assertIn("after 1 rejected", self.events[-1]["reason"])

    async def test_a_dead_session_records_no_recovery(self) -> None:
        client = self.client("MM_SID=stale")

        with self.assertRaises(exceptions.PsegliAuthError):
            await self.fetch(client)

        self.assertTrue(self.events)
        self.assertEqual({event["kind"] for event in self.events}, {"rejected"})
        self.assertNotIn("stale", json.dumps(self.events))

    async def test_diagnose_reports_a_working_session(self) -> None:
        sid = self.portal.new_session()
        client = self.client(f"MM_SID={sid}")

        report = await client.async_diagnose()

        self.assertTrue(report["ok"])
        self.assertEqual(self.portal.login_attempts, 0)
        steps = {step["step"]: step for step in report["steps"]}
        self.assertTrue(steps["open the dashboard"]["ok"])
        self.assertTrue(steps["download chart data"]["ok"])
        self.assertEqual(steps["download chart data"]["intervals_returned"], 1)
        # A live session must not be disturbed by touching the login page.
        self.assertNotIn("load the login page", steps)
        self.assertNotIn("sign in automatically", steps)

    async def test_diagnose_signs_in_when_the_cookie_is_dead(self) -> None:
        client = self.client("MM_SID=stale", email=EMAIL, password=PASSWORD)

        report = await client.async_diagnose()

        self.assertTrue(report["ok"])
        self.assertEqual(self.portal.login_attempts, 1)
        steps = {step["step"]: step for step in report["steps"]}
        self.assertFalse(steps["open the dashboard"]["ok"])
        self.assertTrue(steps["load the login page"]["antiforgery_token_found"])
        self.assertTrue(steps["sign in automatically"]["ok"])
        self.assertTrue(steps["open the dashboard after signing in"]["ok"])

    async def test_diagnose_quotes_the_rejection(self) -> None:
        client = self.client("MM_SID=stale", email=EMAIL, password="wrong password")

        report = await client.async_diagnose()

        self.assertFalse(report["ok"])
        self.assertIn("Invalid email address", report["summary"])
        steps = {step["step"]: step for step in report["steps"]}
        self.assertFalse(steps["sign in automatically"]["ok"])

    async def test_diagnose_explains_a_missing_password(self) -> None:
        client = self.client("MM_SID=stale")

        report = await client.async_diagnose()

        self.assertFalse(report["ok"])
        steps = {step["step"]: step for step in report["steps"]}
        self.assertIn(
            "no username and password", steps["sign in automatically"]["error"]
        )

    async def test_diagnose_never_leaks_credentials(self) -> None:
        client = self.client("MM_SID=stale", email=EMAIL, password=PASSWORD)

        report = await client.async_diagnose()
        dumped = json.dumps(report)

        self.assertNotIn(PASSWORD, dumped)
        self.assertNotIn("stale", dumped)
        self.assertEqual(report["stored_cookie_names"], ["MM_SID"])

    def test_cookie_header_round_trip(self) -> None:
        header = api.normalize_cookie(
            " Cookie: MM_SID=abc%2F123; __RequestVerificationToken=xyz "
        )

        self.assertEqual(header, "MM_SID=abc%2F123; __RequestVerificationToken=xyz")
        self.assertEqual(
            api.parse_cookie_header(header),
            {"MM_SID": "abc%2F123", "__RequestVerificationToken": "xyz"},
        )
        self.assertEqual(api.build_cookie_header(api.parse_cookie_header(header)), header)


if __name__ == "__main__":
    unittest.main()
