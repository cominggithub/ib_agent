---
name: ib-agent
description: Query the user's Interactive Brokers portfolio (positions, options, expiries, account value, quotes, watchlist) through the read-only ib-agent CLI. Use whenever a question concerns what the user holds, option expiries or assignment risk, account or cash value, a ticker's price, or when another project (for example option_harvester) needs IBKR data. Also use before writing code that would otherwise call IBKR directly.
---

# ib-agent

`ib-agent` is the only sanctioned way to reach Interactive Brokers on this
machine. Credentials, the Gateway session and the read-only guarantee live
inside it, so never call the TWS API or the IBKR Client Portal from anywhere
else.

Answer portfolio questions by running the CLI. Do not guess, and do not reuse
figures from earlier in the conversation — the book changes.

## Rules

1. Always pass `--json` when you will parse the output. Parse stdout only;
   diagnostics go to stderr.
2. Check `status` first if a command fails; the Gateway may be down.
3. Starting the Gateway (`ib-agent gateway up`) needs a 6-digit Mobile
   Authenticator code from the user's phone. **Ask the user before starting it**,
   then run `ib-agent gateway up --no-prompt --json`: exit `5` means "a code is
   needed now". Ask the user for a code that has just refreshed (they live ~30 s)
   and submit it with `ib-agent gateway code 123456 --json`. Never run either in
   a loop.
4. Prefer `--stored` when freshness does not matter: it reads the last snapshot,
   contacts nothing, and returns instantly. A live call costs about 3 seconds.
5. Report `as_of` from the payload when you quote figures, and say whether the
   source was `live` or `snapshot`.
6. Nothing here can trade. If the user asks to place, modify or cancel an order,
   say that the session is read-only by design and point at
   `docs/ROADMAP.md`.
7. The Gateway login is `agent-user` (account `U1234567`). Never use, suggest
   or configure `human-user` for any automated session — it is the human's
   username for IBKR Mobile and Client Portal only.

## Question to command

| Question | Command |
|---|---|
| what do I hold? | `ib-agent positions --json` |
| what expires soon / assignment risk? | `ib-agent positions --dte-max 14 --sort dte --json` |
| option book by expiry | `ib-agent expiries --json` (add `-r put` / `-r call`) |
| exposure per underlying | `ib-agent underlyings --json` |
| short puts on ETFs | `ib-agent positions --right put --asset etf --json` |
| everything on one name | `ib-agent positions -u GDX --json` |
| biggest losers | `ib-agent positions --sort pnl --reverse --limit 10 --json` |
| account value, cash, buying power | `ib-agent show --json` (stored) or `ib-agent sync --json` (fresh) |
| price of a ticker | `ib-agent watchlist quotes TSM SPY --json` |
| is this an ETF or a common stock? | `ib-agent instruments --json` |
| how has net liquidation moved? | `ib-agent history --json` |
| is the Gateway up? | `ib-agent status --json` |
| what is my delta / theta / vega exposure? | `ib-agent greeks --json` (`.totals`) |
| greeks of one contract | `ib-agent greeks "GDX 2026-09-18 P 45" --json` |
| conid of a symbol | `ib-agent resolve GDX --json` |
| underlying conid of held options | `ib-agent resolve --from-positions --options --json` |
| what expiries and strikes exist? | `ib-agent chain GDX -e 2026-09 --json` |
| any working orders? | `ib-agent orders --json` |
| what filled today? | `ib-agent executions --json` (alias `fills`) |

## Filters

AND-ed, comma-separated: `-t/--type`, `-r/--right` (put/call), `-u/--underlying`,
`-a/--asset` (etf/common/adr), `-e/--expiry` (`2026-09`, `202609`, `20260904`),
`--expiry-from`, `--expiry-to`, `--dte-min`, `--dte-max`, `--side` (long/short),
`--options`, `--equities`, `--contains`, `--account`.

Output control: `-g/--group-by` (expiry, right, underlying, sec_type,
asset_class, account, side), `-s/--sort` (symbol, expiry, dte, strike, quantity,
value, pnl, underlying), `--reverse`, `--limit`, `--totals-only`.

## Reading the numbers

- Quantities are **negative for short** positions. This book is mostly short
  option premium, so negative `market_value` is normal.
- Positive `unrealized_pnl` on a short option means the option lost value —
  a gain for the seller.
- `dte` is days to expiry. `asset_class` is `ETF`, `COMMON` or `ADR`.
- Money figures come from IB's account-update stream and are delayed outside
  market hours. Quotes default to market data type 3 (delayed unless the account
  has a real-time subscription).
- Grouped payloads carry `groups[].key`, `groups[].totals` and
  `groups[].positions`; flat payloads carry `positions`.
- `greeks` rows carry `position_delta` (delta × size × multiplier) and `source`,
  naming the greek set IB returned (`model`, `last`, `bid`, `ask`). A contract
  with no option market data is still listed, with `error` set and greeks null —
  report the gap instead of treating it as zero.
- `orders` is read-only and cannot place or cancel anything. An empty list does
  not prove nothing is working: orders from another client id (IBKR Mobile) need
  `OverrideTwsMasterClientID` in `~/ibc/config.ini`, which the payload repeats in
  `master_client_id_hint`.
- `executions` covers **today only** (`window: today`); older history needs the
  Flex Web Service. `proceeds` is signed: negative when buying.

## Exit status

`0` success, `1` unexpected failure, `2` usage error, `3` Gateway unreachable
(retrying later may help — ask the user about `gateway up`), `4` no data yet
(nothing synced, or an empty watchlist — retrying will not help; run `sync`),
`5` a login is waiting for a 2FA code — ask the user for one and pass it to
`gateway code`. `130` interrupted. Branch on the code, not on text.

Every `--json` payload is an object carrying `schema` (currently 1). List-shaped
commands wrap their rows: `history` → `snapshots`, `instruments` →
`instruments`, `watchlist list` → `watchlist`, each with a `count`.

## Full reference

`man ib-agent` for the complete manual. Project docs:
`/mnt/d/project/ib_agent/README.md`, `docs/SETUP.md` (auth, 2FA, cron),
`docs/ROADMAP.md` (second login, future order layer),
`docs/OH-INTEGRATION-PLAN.md` (how other projects consume this).
