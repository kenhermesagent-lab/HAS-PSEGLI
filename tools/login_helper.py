#!/usr/bin/env python3
"""Capture a PSEG Long Island session cookie from a real browser.

PSEG puts reCAPTCHA in front of the MySmartEnergy login and only demands it when
it does not trust the request, which is what happens to automated sign-ins.
Home Assistant should not solve that, so this helper hands the human step to a
real browser:

  1. it opens Chromium at https://mysmartenergy.psegliny.com/Dashboard
  2. you sign in yourself, captcha and any verification code included
  3. once the dashboard renders, it copies the session cookies and either prints
     the Cookie header or pushes it to Home Assistant with the
     ``psegli.set_cookie`` action

It never types your password and never stores anything on disk.

Install the dependency:

    pip install playwright
    playwright install chromium

Use it:

    python tools/login_helper.py

    # straight into Home Assistant, with a long-lived access token
    HASS_TOKEN=... python tools/login_helper.py --ha-url http://homeassistant.local:8123
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE_URL = "https://mysmartenergy.psegliny.com"
DASHBOARD_URL = f"{BASE_URL}/Dashboard"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ha-url",
        help="Base URL of your Home Assistant, e.g. http://homeassistant.local:8123",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("HASS_TOKEN", ""),
        help="Home Assistant long-lived access token (default: $HASS_TOKEN)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=300,
        help="Seconds to wait for you to finish signing in (default: 300)",
    )
    return parser.parse_args()


def _cookie_header(cookies: list[dict[str, str]]) -> str:
    return "; ".join(f"{cookie['name']}={cookie['value']}" for cookie in cookies)


def _push_to_home_assistant(ha_url: str, token: str, cookie: str) -> None:
    payload = json.dumps({"cookie": cookie}).encode("utf-8")
    request = urllib.request.Request(
        f"{ha_url.rstrip('/')}/api/services/psegli/set_cookie",
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        print(f"Home Assistant accepted the cookie (HTTP {response.status}).")


def main() -> int:
    args = _parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright is missing. Run: pip install playwright", file=sys.stderr)
        return 1

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        try:
            page.goto(DASHBOARD_URL, wait_until="domcontentloaded")
            print("A browser window is open. Sign in to PSEG there, captcha included.")
            print(f"Waiting up to {args.timeout} seconds for the dashboard...")

            deadline = time.time() + args.timeout
            while time.time() < deadline:
                login_field = page.locator("#LoginEmail")
                on_dashboard = "ashboard" in page.url
                if on_dashboard and login_field.count() == 0:
                    break
                page.wait_for_timeout(1000)
            else:
                print("Never reached the dashboard. Nothing was saved.", file=sys.stderr)
                return 1

            # Give the dashboard a moment to settle so any rotated cookie is set.
            page.wait_for_timeout(2000)
            cookies = context.cookies(BASE_URL)
            if not cookies:
                print("The browser has no PSEG cookies. Nothing was saved.", file=sys.stderr)
                return 1

            cookie = _cookie_header(cookies)
            described = []
            for item in sorted(cookies, key=lambda entry: entry["name"]):
                expiry = item.get("expires")
                if not expiry or float(expiry) < 0:
                    described.append(f"{item['name']} (session)")
                else:
                    described.append(
                        f"{item['name']} (expires "
                        f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(expiry))})"
                    )
            print("Captured cookies:", ", ".join(described))

            if args.ha_url and args.token:
                _push_to_home_assistant(args.ha_url, args.token, cookie)
            else:
                print("\nCookie header (paste it into the Re-authenticate dialog):\n")
                print(cookie)
                print(
                    "\nTo push it automatically next time, pass --ha-url and put a "
                    "long-lived token in HASS_TOKEN."
                )
            return 0
        except urllib.error.HTTPError as err:
            body = err.read().decode("utf-8", "replace")[:400]
            print(f"Home Assistant rejected the call: HTTP {err.code} {body}", file=sys.stderr)
            return 1
        except urllib.error.URLError as err:
            print(f"Could not reach Home Assistant: {err.reason}", file=sys.stderr)
            return 1
        finally:
            context.close()
            browser.close()


if __name__ == "__main__":
    raise SystemExit(main())
