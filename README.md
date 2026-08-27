# ib_agent

Read-only IBKR portfolio sync and query CLI over the TWS API. Built so that
**portfolio reads need no login and no manual approval**: IBC keeps one IB
Gateway logged in in the background, and every read is just a local socket
connection to it.

Safety default: `ReadOnlyApi=yes` in IBC, plus `readonly=True` on the client — the
API session cannot place, change or cancel orders.

## Quick start

```bash
cp .env.example .env          # adjust IB_PORT / IB_ACCOUNT if needed
uv sync
./scripts/install-cli.sh      # ib-agent on PATH, man page, Kiro skill
ib-agent gateway up           # starts the Gateway; asks for one 2FA code
ib-agent status
ib-agent positions
```

`install-cli.sh` puts a wrapper at `~/.local/bin/ib-agent` that pins
`IB_READONLY=true`, gives each invocation its own client id, and appends one line
per call to `logs/cli-audit.log`. It also links `man ib-agent` and the Kiro skill
at `~/.kiro/skills/ib-agent/SKILL.md`. Without it, use `uv run ib-agent ...`.

## Commands

| Command | Purpose |
|---|---|
| `ib-agent status` | is the Gateway up and the API port reachable? |
| `ib-agent positions` (`pos`) | list positions with filters, grouping, totals |
| `ib-agent expiries` | option exposure grouped by expiry |
| `ib-agent underlyings` | exposure grouped by underlying |
| `ib-agent sync` | fetch a snapshot and store it |
| `ib-agent show` | print the latest stored snapshot |
| `ib-agent history` | list stored snapshots with net liquidation |
| `ib-agent watch --interval 900` | unattended sync loop |
| `ib-agent watchlist add\|remove\|list\|quotes` | manage and quote a watchlist |
| `ib-agent instruments` | cached underlying reference data (ETF vs common) |
| `ib-agent resolve` | symbol/conid lookup; `--from-positions` adds underlying conids |
| `ib-agent chain SYM` | option expirations and strikes for an underlying |
| `ib-agent greeks` | model greeks per contract plus book totals |
| `ib-agent orders` | working orders, read-only |
| `ib-agent executions` (`fills`) | today's fills, commission, realized P&L |
| `ib-agent gateway up\|code\|status\|down` | start the Gateway and complete its 2FA login |

Every data command takes `--json`.

## Starting the session

The Gateway login is the one step a human cannot be removed from: the agent's
username is enrolled with IBKR Mobile Authenticator, whose 6-digit code only the
phone can produce. `gateway up` automates everything around that code — it starts
Xvfb and the Gateway, waits for IBKR's dialog, asks for the code at the moment it
is needed, types it on the headless display and waits for the API port.

```bash
ib-agent gateway up                    # prompts for the code when IBKR asks
ib-agent gateway up --no-prompt        # exit 5 = "a code is needed now"
ib-agent gateway code 123456           # answer a login already waiting
ib-agent gateway status                # same as `status`
```

One code per session, not per read: once the port is open every later query is a
local socket connect. `gateway code` also refreshes a session dropped by IBKR's
weekly server reset, and never starts a second Gateway. Details, failure modes and
debugging: [docs/GATEWAY-LOGIN.md](docs/GATEWAY-LOGIN.md).

## Querying positions

`positions` reads live from the Gateway by default; add `--stored` to use the
last snapshot without touching IB, or `--save` to persist the fresh fetch.

```bash
# option book by expiry, with a subtotal per expiry
ib-agent positions --options --group-by expiry

# short puts on ETF underlyings expiring within a month
ib-agent positions --right put --asset etf --dte-max 30 --sort dte

# everything on one underlying
ib-agent positions -u GDX

# worst unrealized P&L first
ib-agent positions --sort pnl --reverse --limit 10

# calendar view: contracts and value per expiry
ib-agent expiries --right call
```

Filters (all AND-ed, all csv-friendly):

| Flag | Meaning |
|---|---|
| `-t, --type` | sec type: `stock`, `option`, `fut`, `cash`, ... |
| `-r, --right` | `put` / `call` |
| `-u, --underlying` | underlying symbols |
| `-a, --asset` | asset class of the underlying: `etf`, `common`, `adr` |
| `-e, --expiry` | expiry prefix: `2026-09`, `202609`, `20260904` |
| `--expiry-from` / `--expiry-to` | ISO date range |
| `--dte-min` / `--dte-max` | days to expiry |
| `--side` | `long` / `short` |
| `--options` / `--equities` | shorthand type filters |
| `--contains` | substring match on the IB local symbol |
| `--account` | account ids |

Output control: `-g/--group-by` (`expiry`, `right`, `underlying`, `sec_type`,
`asset_class`, `account`, `side`), `-s/--sort` (`symbol`, `expiry`, `dte`,
`strike`, `quantity`, `value`, `pnl`, `underlying`), `--reverse`, `--limit`,
`--totals-only`, `--json`.

ETF-vs-stock classification comes from IB contract details (`stockType`) and is
cached in the `instruments` table, so it costs one lookup per new underlying.

## Watchlist

```bash
ib-agent watchlist add SPY GDX --note "core"
ib-agent watchlist list
ib-agent watchlist quotes                 # stored watchlist
ib-agent watchlist quotes TSM ASML --json # ad-hoc symbols
```

Quotes use market data type 3 (delayed), which returns real-time data for
products you subscribe to and free delayed data otherwise, so a quote never
fails for lack of a subscription. Override with `IB_MARKET_DATA_TYPE`.

## JSON for integration

```bash
ib-agent positions --options --group-by expiry --json
ib-agent expiries --json | jq '.groups[] | select(.count > 10)'
ib-agent watchlist quotes --json | jq '.quotes[].change_pct'
```

Grouped payloads carry `groups[].key`, `groups[].totals` and (unless
`--totals-only`) `groups[].positions`; flat payloads carry `positions`. Every
payload includes `source` (`live` or `snapshot`), `as_of`, `accounts`, `filters`
and `totals`.

## Layout

```
src/ib_agent/config.py     settings from .env
src/ib_agent/contract.py   schema version + exit codes (the published contract)
src/ib_agent/gateway.py    port probe + start/stop delegation
src/ib_agent/login.py      headless 2FA: drives IBKR's code dialog via XTEST
src/ib_agent/portfolio.py  IB API reads -> Snapshot (positions, values, instruments)
src/ib_agent/market.py     resolve / chain / greeks: reference data + analytics
src/ib_agent/activity.py   working orders and executions (reads only)
src/ib_agent/query.py      filtering, grouping, totals, table rendering
src/ib_agent/api.py        programmatic interface: loads data, builds payloads
src/ib_agent/watchlist.py  watchlist quotes
src/ib_agent/store.py      sqlite + json persistence, migrations
src/ib_agent/cli.py        argparse adapter over api.py
scripts/gateway-up.sh      idempotent headless Gateway start
scripts/install-cli.sh     install CLI wrapper + man page + Kiro skill
scripts/install-hooks.sh   pre-commit secret scan (see docs/SECRETS.md)
man/ib-agent.1             full CLI manual (`man ib-agent`)
.kiro/skills/ib-agent/     skill so Kiro drives the CLI instead of guessing
tests/                     run with `uv run pytest`
```

Snapshots go to `data/portfolio.sqlite3` (tables: `snapshots`, `positions`,
`account_values`, `instruments`, `watchlist`) plus a JSON copy under
`data/snapshots/`.

Auth model, 2FA caveat, cron setup and token-only alternatives (Flex Web
Service, Web API OAuth): see [docs/SETUP.md](docs/SETUP.md).

Goal and next steps — two simultaneous logins (mobile + agent) and an
order-placing agent account: see [docs/ROADMAP.md](docs/ROADMAP.md).

What the agent's username can and cannot reach — account-scoped reads versus
username-scoped watchlists, item by item: see
[docs/SECOND-USERNAME-SCOPE.md](docs/SECOND-USERNAME-SCOPE.md).

How other projects consume this (option_harvester first), the access-control
model and the phased plan: see
[docs/OH-INTEGRATION-PLAN.md](docs/OH-INTEGRATION-PLAN.md) and, on the consumer
side, `option_harvester/docs/ib-agent-integration.md`.
