# HAS-PSEGLI

A Home Assistant custom integration for **PSEG Long Island** (MySmartEnergy / MyMeter) that brings hourly, daily, weekly and monthly electricity usage and estimated cost into Home Assistant.

It exists because the official Opower integration does not cover PSEG Long Island, and the existing community integration only writes two hidden statistics with no sensors and no cost data.

> Not affiliated with, endorsed by, or supported by PSEG, LIPA, VertexOne or Accelerated Innovations.

## What it does

- Logs into `mysmartenergy.psegliny.com` with a browser session cookie you paste in once.
- Downloads the hourly usage series (the same data the portal charts) and aggregates it locally.
- Creates real sensor entities for usage and cost: today, yesterday, this week, this month, last month and the current billing cycle.
- Writes both kWh and USD into Home Assistant long-term statistics, so the built-in Energy Dashboard can use them.
- Adds a `psegli.backfill` service to pull up to 400 days of history.
- Asks you for a fresh cookie through Home Assistant's normal "Re-authenticate" flow when the session expires.

## Why a cookie instead of username and password?

The PSEG portal sits behind reCAPTCHA and email/SMS verification codes, and there is no OAuth or public API. Automating the login reliably is not possible from a Home Assistant integration, so this integration uses the session cookie your browser already has. That is also why it does not lock your account by retrying bad passwords.

## Installation

### HACS (recommended)

1. In HACS, open the three-dot menu → **Custom repositories**.
2. Add `https://github.com/kenhermesagent-lab/HAS-PSEGLI` with category **Integration**.
3. Install **PSEG Long Island**, then restart Home Assistant.
4. Go to **Settings → Devices & Services → Add Integration** and search for **PSEG Long Island**.

### Manual

Copy `custom_components/psegli` into your Home Assistant `config/custom_components/` folder and restart.

> If you previously installed another `psegli` custom component, remove it first — two integrations cannot share the same domain.

## Getting your session cookie

1. Log in at <https://mysmartenergy.psegliny.com/Dashboard>.
2. Open your browser developer tools (F12) → **Network** tab.
3. Reload the page.
4. Click the `Dashboard` request.
5. In **Request Headers**, copy the entire value of the `Cookie` header.
6. Paste it into the integration setup form.

The cookie is stored in Home Assistant's `.storage` and is only sent to `mysmartenergy.psegliny.com`. When it expires, Home Assistant will show a **Re-authenticate** button; paste a fresh cookie there.

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

The period sensors use `state_class: total` with a `last_reset` at the start of each period, so they behave correctly on the Energy Dashboard and in statistics graphs.

### Energy Dashboard

Go to **Settings → Dashboards → Energy → Add consumption** and choose **PSEG Long Island usage** (the long-term statistic) as the grid consumption source. Add **PSEG Long Island cost** as the cost source if you want dollars alongside kWh.

## Backfill history

```yaml
action: psegli.backfill
data:
  days: 365
```

PSEG keeps sub-daily interval data for about 13 months. Existing intervals are skipped, so running it twice does not double count.

## What to expect

- **Data is not real-time.** PSEG states the smart meter transmits about three times a day, so hourly readings typically appear in batches, hours after the fact. This is a billing and cost view, not a live power monitor.
- **The cookie will expire eventually.** Re-authenticate when Home Assistant asks.
- **PSEG can change the portal.** The endpoints used here are the same ones the dashboard calls; if PSEG redesigns MySmartEnergy, the integration will need an update.
- **Cost is estimated from your inputs**, not read from your bill. It will drift from the real bill if the supply rate changes or your usage falls under a tiered block.

## Troubleshooting

**"PSEG rejected that cookie"** — the cookie is expired or was copied from a page that is not logged in. Repeat the cookie steps and paste the whole header.

**No data, no errors** — confirm MySmartEnergy shows usage for the account. Brand-new accounts and meters without Smart Energy enabled will return an empty series.

**Timestamps off by an hour or more** — set *Timestamp correction* in the options. Positive values move readings later.

**Debug logging**

```yaml
logger:
  logs:
    custom_components.psegli: debug
```

## Development

The parsing, aggregation and tariff code has no Home Assistant imports and is covered by offline tests:

```bash
python -m unittest discover -s tests -t . -v
```

## License

MIT
