# HAS-PSEGLI

A Home Assistant custom integration for **PSEG Long Island** (MySmartEnergy / MyMeter) that brings hourly, daily, weekly and monthly electricity usage and estimated cost into Home Assistant.

It exists because the official Opower integration does not cover PSEG Long Island, and the existing community integration only writes two hidden statistics with no sensors and no cost data.

> Not affiliated with, endorsed by, or supported by PSEG, LIPA, VertexOne or Accelerated Innovations.

## What it does

- Signs in to `mysmartenergy.psegliny.com` with your PSEG email and password, and signs in again on its own when PSEG expires the session.
- Keeps the session cookies PSEG rotates on every response, so a Home Assistant restart does not log you out.
- Falls back to a pasted browser session cookie for accounts that PSEG makes interactive (security question, verification code, reCAPTCHA).
- Downloads the hourly usage series (the same data the portal charts) and aggregates it locally.
- Creates real sensor entities for usage and cost: today, yesterday, this week, this month, last month and the current billing cycle.
- Writes both kWh and USD into Home Assistant long-term statistics, so the built-in Energy Dashboard can use them.
- Adds a `psegli.backfill` service to pull up to 400 days of history.
- Only asks you for anything through Home Assistant's normal "Re-authenticate" flow when PSEG blocks an automatic sign-in.

## How sign-in works

There is no OAuth or public API, so the integration talks to the same ASP.NET endpoints the portal's own login form uses.

| Method | What happens |
| --- | --- |
| **Email and password** (recommended) | The integration posts your credentials to `/Home/Login`, keeps the resulting session cookies, and signs in again automatically whenever the portal expires them. |
| **Browser session cookie** | You paste a `Cookie` header from a browser that is already logged in. Nothing is signed in automatically. |

Three things keep the automatic mode out of your way:

- **Rotated cookies are saved.** PSEG hands back fresh session cookies as you browse; the integration stores them in the config entry, so a restart or a cookie rotation does not force a new login.
- **Sign-ins are rate limited.** After a failed sign-in it waits 15 minutes before trying again, so a wrong password can never lock your account.
- **PSEG's challenges are detected.** If the account is asked for a security question, an email/SMS code, a reCAPTCHA or a password change, the integration says so instead of guessing, and you finish with the cookie method.

The sign-in reproduces what the portal's own page does: it loads the login page, submits the `__RequestVerificationToken` the page renders, sends the same browser headers (`Origin`, `Referer`, `X-Requested-With`) and asks for *Remember Me*, so the session comes back as a persistent cookie.

Home Assistant also touches the dashboard every 10 minutes. MyMeter expires a session that stops being used — that is why a pasted cookie used to die as soon as the browser tab was closed — so the periodic touch keeps the stored session, pasted or automatic, alive between the 30-minute polls.

Credentials live in Home Assistant's `.storage` and are only sent to `mysmartenergy.psegliny.com`.

### When PSEG asks for a captcha

PSEG puts reCAPTCHA in front of the login form but only demands it when it does not trust the request — a few automated attempts from Home Assistant's IP is enough. The integration does not try to solve it: as soon as PSEG asks for a captcha, automatic sign-in pauses and the entry asks you for a session cookie instead, so the captcha is not fed and the account is not hammered.

The easiest way to hand it a good cookie without opening developer tools is the helper in `tools/`:

```bash
pip install playwright
playwright install chromium
python tools/login_helper.py
```

A Chromium window opens at the portal. Sign in yourself, captcha and any verification code included; the script waits for the dashboard, then prints the `Cookie` header. With `--ha-url http://homeassistant.local:8123` and a long-lived token in `HASS_TOKEN` it pushes the cookie straight into Home Assistant instead.

You can also set the cookie yourself:

```yaml
action: psegli.set_cookie
data:
  cookie: "MM_SID=...; __RequestVerificationToken=..."
```

The action validates the cookie before storing it, then reloads the entry and starts polling with it immediately. It works even while Home Assistant is showing the *Re-authenticate* prompt, because it does not need the integration to be loaded. After that the keep-alive keeps the session warm (every ten minutes by default, adjustable under **Configure**), so this is an occasional job rather than a per-restart one. Automatic sign-in resumes whenever PSEG stops asking for a captcha, for example once the account cools down or after a password reset.

## Installation

### HACS (recommended)

1. In HACS, open the three-dot menu → **Custom repositories**.
2. Add `https://github.com/kenhermesagent-lab/HAS-PSEGLI` with category **Integration**.
3. Install **PSEG Long Island**, then restart Home Assistant.
4. Go to **Settings → Devices & Services → Add Integration** and search for **PSEG Long Island**.

### Manual

Copy `custom_components/psegli` into your Home Assistant `config/custom_components/` folder and restart.

> If you previously installed another `psegli` custom component, remove it first — two integrations cannot share the same domain.

## Getting your session cookie (cookie mode only)

Use this when your account asks for a verification code, or when you prefer not to store a password.

1. Log in at <https://mysmartenergy.psegliny.com/Dashboard> and tick **Remember Me** — otherwise the portal only gives the browser a session cookie that it discards when the tab closes.
2. Open your browser developer tools (F12) → **Network** tab.
3. Reload the page.
4. Click the `Dashboard` request.
5. In **Request Headers**, copy the entire value of the `Cookie` header.
6. Paste it into the integration setup form.

The cookie is stored in Home Assistant's `.storage` and is only sent to `mysmartenergy.psegliny.com`. When it expires, Home Assistant shows a **Re-authenticate** button; paste a fresh cookie there. If the entry has a stored password, pressing **Re-authenticate** is usually enough on its own, because the integration signs in again first.

## Tariff configuration

Costs are an **estimate**, not your actual bill. The portal only returns kWh, so the integration multiplies each interval by the rates you configure in **Settings → Devices & Services → PSEG Long Island → Configure**.

| Setting | Meaning |
| --- | --- |
| Rate mode | `flat` for one delivery rate all day, `tou` for a time-of-day rate |
| Delivery charge | USD/kWh delivery portion. For TOU this is the off-peak value |
| Power supply charge | USD/kWh for the current month — PSEG publishes a new value monthly |
| Peak delivery charge | Delivery portion during peak hours |
| Peak supply multiplier | Multiplier on the supply charge during peak hours |
| Daily service charge | Fixed USD/day, added once per day to period totals |
| Peak start/end hour | Local hours of the peak window (default 16–20) |
| Peak weekdays only | Peak pricing Monday–Friday only |
| Week start | First day of the "this week" sensor |
| Billing day | Day of month your billing cycle starts |
| Days of history | Automatic first-run backfill (0 disables) |
| Timestamp correction | Only change if readings look shifted in your timezone |

PSEG Long Island residential rates have a delivery charge, a power supply charge that changes every month, and a fixed daily service charge. Time-of-day plans add a peak window. Enter the values from your current rate booklet or bill.

## Sensors

Each period has a usage sensor (kWh) and a cost sensor (USD):

- `sensor.pseg_long_island_today_usage` / `today_cost`
- `sensor.pseg_long_island_yesterday_usage` / `yesterday_cost`
- `sensor.pseg_long_island_this_week_usage` / `this_week_cost`
- `sensor.pseg_long_island_this_month_usage` / `this_month_cost`
- `sensor.pseg_long_island_last_month_usage` / `last_month_cost`
- `sensor.pseg_long_island_billing_cycle_usage` / `billing_cycle_cost`
- `sensor.pseg_long_island_latest_interval_usage` — newest hourly reading, with rate and peak attributes
- `sensor.pseg_long_island_last_reading` — timestamp of the newest reading (disabled by default)

The accumulating period sensors (today, this week, this month, billing cycle) use `state_class: total_increasing`, which is what Home Assistant defines for a total that restarts at a period boundary. Yesterday and last month are snapshots of a finished period and can move either way, so they use `total` with a `last_reset` at the start of the period. The cost sensors are `total` with `last_reset` as well, because the monetary device class does not accept `total_increasing`.

### Energy Dashboard

Go to **Settings → Dashboards → Energy → Add consumption** and choose **PSEG Long Island usage** (the long-term statistic) as the grid consumption source. Add **PSEG Long Island cost** as the cost source if you want dollars alongside kWh.

## Backfill history

```yaml
action: psegli.backfill
data:
  days: 365
```

PSEG keeps sub-daily interval data for about 13 months. The import rewrites the hours it covers instead of appending blindly, so running it twice does not double count, an hour PSEG later revises to zero loses its old value, and running it again with a larger range adds the older months and recalculates the cumulative sums from them.

## What to expect

- **Data is not real-time.** PSEG states the smart meter transmits about three times a day, so hourly readings typically appear in batches, hours after the fact. This is a billing and cost view, not a live power monitor.
- **The session will expire eventually.** With a stored password this is invisible; without one, re-authenticate when Home Assistant asks. PSEG can also invalidate sessions after a password change.
- **PSEG can change the portal.** The endpoints used here are the same ones the dashboard calls; if PSEG redesigns MySmartEnergy, the integration will need an update.
- **Cost is estimated from your inputs**, not read from your bill. It will drift from the real bill if the supply rate changes or your usage falls under a tiered block.

## Troubleshooting

**"PSEG rejected that cookie"** — the cookie is expired or was copied from a page that is not logged in. Repeat the cookie steps and paste the whole header.

**Automatic sign-in stopped working** — check the log for the PSEG message, which is logged verbatim. *Invalid email address or password* means the stored password changed: remove and re-add the integration, or re-authenticate. A message about a security question, verification code or reCAPTCHA means the account needs a human; switch the entry to the session cookie method. PSEG may also lock the account the same way a browser would, so keep the sign-in method honest rather than retrying a bad password.

**The cookie worked, then stopped after closing the browser** — the pasted cookie was a session cookie and the portal expired the idle session. The integration now touches the dashboard every 10 minutes to prevent that; if it still expires, log in again with **Remember Me** ticked and paste the new header, which gives the portal a persistent cookie to renew from.

**Sign-in succeeded but no data** — the login worked and the account simply has no usage for that range; check the portal itself.

**No data, no errors** — confirm MySmartEnergy shows usage for the account. Brand-new accounts and meters without Smart Energy enabled will return an empty series.

**Timestamps off by an hour or more** — set *Timestamp correction* in the options. Positive values move readings later.

**Checking the session without reading logs**

Run the `psegli.check_session` action from **Developer Tools → Actions** with *Return response* enabled. It walks the chain — reach the portal, load the login page, open the dashboard with the stored session, sign in automatically, download chart data — and returns PSEG's own message at the step that fails. It performs one deliberate sign-in attempt if the session is dead, which also resolves a "Re-authenticate" prompt when a password is stored.

**Diagnostics**

**Settings → Devices & Services → PSEG Long Island → Download diagnostics** returns a redacted snapshot: sign-in method, cookie names (never values), the last update result and exception, and how far back the usage and cost statistics reach.

**Debug logging**

```yaml
logger:
  logs:
    custom_components.psegli: debug
```

## Development

The parsing, aggregation, tariff and sign-in code has no Home Assistant imports and is covered by offline tests. The client tests run against a small fake portal that speaks the same login/JSON/cookie protocol:

```bash
python -m unittest discover -s tests -t . -v
```

## License

MIT
