# What the agent username can and cannot do

Status: 2026-08-17. Question answered here: with the Gateway logged in as
`agent-user`, what can the agent do to the **primary** account and to the
human's own view of it?

## The one rule that decides everything

`agent-user` is a second *username* on account `U1234567` — not a second
account. So:

- **Account-scoped** things are shared: positions, balances, orders, executions,
  trading permissions, market data entitlements at account level. The agent sees
  exactly the human's book.
- **Username-scoped** things are not shared: platform settings and watchlists.
  `~/Jts/jts.ini` has `s3store=true` (settings live in IBKR's cloud keyed to the
  username) and the Web API returns watchlists as `user_lists`.

Second rule, independent of the first: **the TWS socket API has no watchlist
CRUD**. Writing an IB watchlist is a Web API-only operation
(`POST /iserver/watchlist`), so no Gateway session of any username can do it.

## Capability table

Legend: **yes** = works or will work through `ib-agent`; **no** = not possible on
this path; *test* = plausible but unverified, see notes.

| # | Item | Mechanism | Doable as `agent-user`? | Notes |
|---|---|---|---|---|
| 1 | Sync the primary account's positions | `ib.positions()` / `ib.portfolio()` | **yes** | Account-scoped. `positions`, `sync` already do it |
| 2 | Balances, NLV, cash, account-level margin | `ib.accountSummary()` | **yes** | Account-scoped |
| 3 | Snapshot history in sqlite | local | **yes** | `history`, `show` |
| 4 | ETF / common classification | `reqContractDetails().stockType` | **yes** | `instruments` |
| 5 | Spot / bid / ask quotes | `reqTickers()` | **yes** | Delayed feed (type 3) needs no subscription |
| 6 | Greeks, IV | `Ticker.modelGreeks` | **yes** | `greeks`, built 2026-08-17; delta/gamma/theta/vega per contract plus book totals |
| 7 | Chain months / strikes | `reqSecDefOptParams()` | **yes** | `chain`, built 2026-08-17 |
| 8 | Ticker / underlying conids | `qualifyContracts()`, `underConId` | **yes** | `resolve` / `resolve --from-positions`, built 2026-08-17 |
| 9 | Read pending orders placed on mobile | `reqAllOpenOrders()` | **yes** | `orders`, built 2026-08-17. Needs `OverrideTwsMasterClientID`; account-scoped, so the human's orders are visible |
| 10 | Read executions | `reqExecutions()` + Flex for history | **yes** | `executions`, built 2026-08-17; today only, Flex history not yet wired |
| 11 | Local `ib-agent` watchlist + quotes | sqlite `watchlist` table | **yes** | Purely local (`store.py:71`); never touches IBKR |
| 12 | Read the human's IB watchlists | — | **no** | No socket API; and username-scoped, so the agent's Web API session would not see them either |
| 13 | Write an IB watchlist the human sees in TWS / mobile | `POST /iserver/watchlist` | **no** | Two blockers: no socket CRUD, and lists land under the writing username. Stays with the OH extension in the human's portal tab (`extension/background.js:516`) |
| 14 | Write an IB watchlist under the agent's own username | Web API as `agent-user` | *test* | Would work mechanically but is invisible to the human — useful only for machine read-back |
| 15 | Per-position maintenance margin | `whatIfOrder()` | **no** | Order message, so `ReadOnlyApi=yes` rejects it. OH-INTEGRATION-PLAN §4 |
| 16 | Place / modify / cancel orders | `placeOrder()` | **no**, by design | Gated three ways today; ROADMAP steps 4-5 |
| 17 | Move money | Client Portal only | **no** | Withhold Funding rights on the user (ROADMAP §1) |
| 18 | Run while the human is on IBKR Mobile as `human-user` | two usernames | *test* | The whole point of the split; acceptance test in ROADMAP §2, not yet passed |
| 19 | Log into Client Portal as `agent-user` while the Gateway runs | — | **no** | Breaks the Gateway's auto-reconnect. Always `gateway-down.sh` first |
| 20 | Unattended restart after the weekly IBKR reset | IBC | *test* | Currently **broken**: `agent-user` uses Mobile Authenticator (typed TOTP), which IBC cannot generate. Switch to IB Key push |

## Verification status

| Claim | How it stands |
|---|---|
| Socket API has no watchlist CRUD | Verified: `grep -ri watchlist` in `ib_async` yields exactly three source hits — `fillWatchlist` (a fill-notification flag, `objects.py:210`, `client.py:1171`) and a `"watchlist"` conid key inside a Wall Street Horizon event-data filter (`ib.py:2010`). No create/read/delete. IBKR's TWS API docs offer only "Requesting Watchlist Data", i.e. market-data ticks |
| `POST /iserver/watchlist` is the write path and shows in TWS + Client Portal | Verified from IBKR Web API docs and from OH's working implementation |
| Watchlists are username-scoped | **Inferred**, not documented by IBKR: endpoints return `user_lists`, and `jts.ini s3store=true` stores settings per username. Cheap test below |
| Portfolio data is account-scoped | Verified in practice: the current Gateway session reads the same book the human sees on mobile |
| `whatIf` is rejected under `ReadOnlyApi=yes` | Unverified; see OH-INTEGRATION-PLAN §4 |

Test for the one inference that matters (row 12/13/14): once `agent-user` can
log into Client Portal, look at its watchlists. If the human's lists appear,
watchlists are shared and the agent can push them directly; if the list is
empty, they are per-username and the push must stay on `human-user`.

## Consequence

Reads: give them all to `ib-agent` on `agent-user`.
IB watchlist writes: leave on the extension in the human's session until the
test above says otherwise.
