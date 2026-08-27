# Setup: unattended IBKR portfolio reads

## Why reads can be automatic

The IBKR TWS/Gateway API has no API key. Authentication happens once, when the
**IB Gateway** process logs in; after that the Gateway holds the session and any
local API client can connect over TCP without further credentials.

So the split is:

| Layer | Runs | Needs a human? |
|---|---|---|
| IB Gateway + IBC (login) | long-lived background process | once per login, only if 2FA is on |
| `ib-agent sync` (reads) | any time, any frequency | never |

`IBC` (already installed at `/opt/ibc`) types the username/password into the
Gateway login dialog for you, so no interactive terminal is required. Xvfb gives
the Gateway a virtual display, since it is a Java GUI app.

## The one thing that cannot be fully automated

If the IBKR account has two-factor auth (IBKR Mobile / IB Key), IBKR sends a
push notification at each **fresh login** and it must be tapped on the phone.
There is no headless bypass — by design.

Mitigations, already configured in `~/ibc/config.ini`:

- `AutoRestartTime=11:45 PM` — the Gateway restarts itself daily and **reuses the
  existing authentication**, so no 2FA prompt. IBKR forces a real re-login only
  after the weekly server reset (Sunday), so expect roughly one phone tap a week.
- `ReloginAfterSecondFactorAuthenticationTimeout=yes` and
  `ExitAfterSecondFactorAuthenticationTimeout=no` — if a 2FA prompt is missed,
  IBC retries instead of dying.

If even a weekly tap is unacceptable, the alternatives are in
"Fully token-based options" below.

## Current machine configuration

- IB Gateway 10.37 at `~/Jts/ibgateway/1037`
- IBC 3.23.0 at `/opt/ibc`, config `~/ibc/config.ini` (holds the credentials;
  backup written as `config.ini.bak.<date>`)
- `TradingMode=live`, `OverrideTwsApiPort=4001`
- `ReadOnlyApi=yes` — the API socket cannot place, modify or cancel orders.
  This is deliberate for a portfolio-tracking project; flip to `no` later if you
  want the agent to trade.
- `CommandServerPort=7462`, bound to `127.0.0.1` — lets scripts ask IBC to
  restart or stop the Gateway.

Port convention: `4001` live, `4002` paper for the Gateway; `7496`/`7497` for TWS.

## Network exposure caveat

The Gateway binds its API socket to **all interfaces** (`ss -ltn` shows
`*:4001`), not just loopback. What protects it today is `TrustedIPs=127.0.0.1`
in `~/Jts/jts.ini`: connections from any other address are refused, and
`ReadOnlyApi=yes` means even a successful client cannot trade. There is no
authentication on the socket itself, so do not relax `TrustedIPs`, and if this
host is ever reachable from an untrusted network, also tick
*Configure → API → Settings → Allow connections from localhost only* in the
Gateway UI.

## Verified on 2026-08-04

- `scripts/gateway-up.sh` brought up Xvfb `:99` + Gateway; IBC typed the
  credentials, IBKR pushed a 2FA request which was approved on the phone once,
  and port 4001 opened ~75 s after launch.
- `ib-agent sync` then ran repeatedly (snapshots #1–#5) with **no further
  prompt of any kind**: 91 positions for the account and 13 account
  value rows per snapshot, NetLiquidation moving 132,968.31 → 133,009.92 as the
  market ticked.
- `scripts/gateway-down.sh` is not exercised by the test run (stopping the
  session would cost another 2FA tap on the next start).

## Daily operation

```bash
scripts/gateway-up.sh        # idempotent: starts Xvfb + Gateway, waits for :4001
uv run ib-agent status       # is the port live?
uv run ib-agent sync         # one snapshot -> sqlite + json
uv run ib-agent watch --interval 900   # unattended loop
scripts/gateway-down.sh      # stop everything
```

`ib-agent sync` calls `gateway-up.sh` itself when `IB_AUTO_START_GATEWAY=true`,
so a cron entry needs only the sync command.

### Keeping it up across reboots

WSL has no systemd session by default here, so use either:

```bash
# cron: ensure gateway is alive, then snapshot every 15 min during market hours
*/15 * * * * cd /mnt/d/project/ib_agent && ./scripts/gateway-up.sh >> logs/cron.log 2>&1 && .venv/bin/ib-agent sync --quiet >> logs/cron.log 2>&1
```

or a Windows Task Scheduler entry running `wsl -d Ubuntu -- .../gateway-up.sh`
at logon.

## Fully token-based options (no Gateway, no 2FA at all)

1. **Flex Web Service** — generate a token + Flex query in Client Portal
   (Performance & Reports → Flex Queries). Fetching is a plain HTTPS call with
   the token; the token lives ~1 year. Gives end-of-day positions, trades, cash
   report — perfect for a long-term portfolio ledger, but not intraday prices.
   `ibflex` is already a dependency; set `IB_FLEX_TOKEN` / `IB_FLEX_QUERY_ID`.
2. **Web API with OAuth 1.0a** — fully headless, no Gateway process, RSA-signed
   requests. **Not available to individual accounts**: IBKR lists OAuth 1.0a
   support for advisor, broker/FCM, proprietary trading group, hedge/mutual fund
   and third-party developer accounts only. The individual-account Web API path
   is the Client Portal Gateway, which needs a browser login on the same machine
   and is therefore worse than IBC for unattended use.

Recommendation: keep the Gateway path for live/intraday portfolio state, and add
Flex later as a nightly reconciliation source that needs no login at all.

## Troubleshooting

- `IBC returned exit status 1` right after "Found Gateway main window" (seen on
  2026-03-30 in `~/ibc/logs/`): the Gateway JVM died during login. Usually a
  display problem — make sure `Xvfb :99` is running and `DISPLAY` is set, which
  `gateway-up.sh` handles.
- Port 4001 never opens: check `logs/gateway-start.log` and
  `~/ibc/logs/ibc-*_GATEWAY-*.txt`.
- "Existing session detected": `ExistingSessionDetectedAction=primaryoverride`
  means this Gateway wins and an existing TWS/mobile session may be logged out.
- Data subscriptions: `marketPrice`/`marketValue` come from the account update
  stream. Without market data permissions some fields stay empty; positions and
  average cost still arrive.
