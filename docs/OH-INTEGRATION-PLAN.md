# Interface plan — ib_agent as the only door to IBKR

Status: plan, 2026-08-11. Consumer under design: **option_harvester** (OH).

## 1. Principle

One process family talks to IBKR; everything else talks to `ib-agent`.

Today OH reaches IBKR through a Chrome extension that runs inside the user's
logged-in Client Portal tab and piggybacks on the browser session
(`credentials: "include"` against `/portal.proxy/v1/portal`). That works, but it
means: a browser must be open and logged in, the session is the user's *human*
session, every new data need becomes another scraped endpoint, and there is no
single place to enforce "reads only".

The target: `ib-agent` owns the Gateway session and exposes a CLI whose `--json`
payloads are the contract. OH shells out. The extension keeps only the handful of
things the TWS socket API genuinely cannot do.

## 2. What OH gets from IBKR today

From the extension inventory (`extension/background.js`, line refs kept):

| Data | Client Portal endpoint | OH route | Cadence |
|---|---|---|---|
| Positions | `GET /portfolio/{acct}/positions/all` :32 | `POST /api/positions` | every sync |
| Balances / margin / NLV | `GET /portfolio/{acct}/summary` :35 | `POST /api/balances` | every sync |
| Pending orders | `GET /iserver/account/orders` :33 | `POST /api/orders` | every sync |
| Executions (~7 days) | `GET /iserver/account/trades` :34 | `POST /api/trades` | every sync |
| User watchlists | `GET /iserver/watchlists`, `?id=` :41,:46 | `POST /api/watchlist` | every sync |
| Greeks Δ Γ Θ Vega + IV | `GET /iserver/marketdata/snapshot?fields=…7308-7311,7283` :390 | `POST /api/greeks` | manual + deep |
| Per-position margin | `POST /iserver/account/{acct}/orders/whatif` :454 | `POST /api/margin` | deep |
| Ticker conids | `GET /trsrv/stocks?symbols=` :273 | `POST /api/securities/conids` | deep |
| Underlying conid of held options | `GET /trsrv/secdef?conids=`, `/iserver/contract/{id}/info` :767,:776 | `POST /api/underlying-conids` | deep |
| Chain: months, strikes, ATM info | `secdef/search`, `secdef/strikes`, `secdef/info` :313-326 | `POST /api/options` | on demand |
| Spot / bid / ask / IV snapshot | `GET /iserver/marketdata/snapshot?fields=31,84,86,87,7283` | `POST /api/options` | on demand |
| OH→IB list push + read-back | `POST/DELETE /iserver/watchlist`, `GET /iserver/watchlist?id=` :594,:537,:586 | `GET /api/oh-watchlists`, `POST /api/oh-verify` | manual + deep |

## 3. Replacement map

| Item | TWS socket API equivalent | `ib-agent` command | Status |
|---|---|---|---|
| Positions | `ib.positions()` + `ib.portfolio()` | `positions`, `sync` | **works today** |
| Balances / NLV / cash | `ib.accountSummary()` | `sync`, `show` | works; needs more tags (§5) |
| Instrument class (ETF/common) | `reqContractDetails().stockType` | `instruments` | **works today** |
| Ticker conid | `qualifyContracts()` / `reqContractDetails()` | `resolve` | **works today** — **strictly better** than `/trsrv/stocks` |
| Underlying conid of an option | `ContractDetails.underConId` / `Contract.undConId` | `resolve --from-positions` | **works today** — **removes the pin machinery** |
| Chain months / strikes | `reqSecDefOptParams()` | `chain` | **works today** |
| Spot / bid / ask | `reqTickers()` | `watchlist quotes` | **works today** |
| Greeks Δ Γ Θ Vega, IV | `Ticker.modelGreeks` | `greeks` | **works today**; one verification risk (§7) |
| Pending orders | `reqAllOpenOrders()` | `orders` (read-only) | **works today**; needs master client id |
| Executions | `reqExecutions()` (today only) + Flex for history | `executions` | **works today**; Flex history still to add |
| Per-position margin | `whatIfOrder()` | — | **blocked by design** (§4) |
| Read user watchlists | none | — | **Client-Portal only** |
| Create/delete IB watchlists | none | — | **Client-Portal only** |

Verified: the socket API has no watchlist CRUD. Grepping `ib_async` for
"watchlist" finds only a news-provider code and a fill-notification flag.

### What stays in the extension

Three things, and only three:

1. **OH→IB list push** (`OH:*`, ids 990001+) and its read-back verify. No socket
   equivalent exists. Unchanged.
2. **Pulling the user's own IB watchlists.** No socket equivalent.
3. **Per-position maintenance margin** via the closing-order what-if.

Everything else the extension does becomes dead code once the phases below land.
Deleting it is part of the work, not an afterthought — a half-migrated bridge is
worse than either end state.

## 4. Why per-position margin stays out

The exact per-position margin OH stores in `PositionMargin` comes from a what-if
*closing* order. `whatIfOrder()` is delivered as an order message with
`whatIf=true`, so `ReadOnlyApi=yes` is expected to reject it — the Gateway does
not inspect intent. Supporting it means opening gate 1 for the whole session,
which trades the strongest guarantee in the design for one derived number.

Decision: leave margin on the extension path. Revisit only in the order phase
(`docs/ROADMAP.md` step 5), where a `whatif`-only scope can be introduced
deliberately and tested on paper first. Account-level margin
(`FullInitMarginReq`, `FullMaintMarginReq`) does come through `accountSummary`
and covers the `/api/balances` need.

*Unverified:* that read-only mode rejects `whatIf`. Cheap to test once the
Gateway is up; if it is allowed, this decision can be revisited, but the default
stays closed.

## 5. Access control

Honest framing first: OH and ib_agent run as the **same Unix user on the same WSL
box**. In-process tokens and scopes would be theatre — anything that can call a
token-protected endpoint can equally run `ib-agent` directly, or read
`~/ibc/config.ini`. So the controls worth building are the ones enforced outside
the Python process, plus an audit trail for accidents.

**Layer 1 — IBKR account rights.** The Gateway login is a separate username with
Trading granted, **Funding and Account Settings withheld**. Money cannot leave
the account through this path regardless of any code bug. (`docs/ROADMAP.md` §1.)

**Layer 2 — Gateway.** `ReadOnlyApi=yes` in `~/ibc/config.ini`. The Gateway
rejects order messages on the socket.

**Layer 3 — client.** `IB_READONLY=true`, pinned by the installed wrapper
(`scripts/install-cli.sh`) so no caller can flip it by exporting a variable.

**Layer 4 — no order code in the package.** Enforce with a test that greps the
package for `placeOrder`/`whatIfOrder` and fails if either appears outside a
future, explicitly-flagged module.

**Layer 5 — profile + audit.** Every invocation of the wrapper appends
`timestamp, profile, pid, rc, duration, argv` to `logs/cli-audit.log`. Consumers
set `IB_AGENT_PROFILE` (OH sets `option_harvester`). Next step: an
`access.toml` mapping profile → allowed subcommands → allowed accounts, checked
in `cli.py` before dispatch, refusing anything unlisted. This bounds *accidents*
and gives attribution; it is not a sandbox, and the docs should not pretend
otherwise.

**Layer 6 — IBKR account-level precautionary limits.** Order size/value caps set
in Client Portal, which no local code can bypass.

**Optional layer 7 — real isolation.** Run ib_agent as its own Unix user
(`ibagent`) owning `data/` and the venv; grant OH access through group
membership on a Unix socket or a read-only export file. Only then do scopes
become enforceable. Worth doing if OH ever runs untrusted code; not before.

### Finding worth acting on, in OH not here

OH's write routes (`POST /api/positions`, `/api/orders`, `/api/greeks`, …) have
**no authentication**, and prod binds a port reachable outside the NAT
(`<prod-host>:<port>`). Any host that can reach that port can overwrite the
position book. That is a larger hole than anything in ib_agent's interface, and
this migration does not fix it: it changes who *calls* those routes, not who
*can*. Fix separately — bind to localhost, or require a shared secret on writes.

## 6. CLI surface to build

Existing: `status`, `sync`, `positions`/`pos`, `expiries`, `underlyings`, `show`,
`history`, `watch`, `watchlist`/`wl`, `instruments`, `gateway`. Documented in
`man/ib-agent.1`.

New commands, all `--json`, all read-only:

| Command | Purpose | Key output |
|---|---|---|
| `greeks [--stored] [--conids CSV]` | model greeks + IV for held options (default) or given conids | `con_id, delta, gamma, theta, vega, implied_vol, opt_price, und_price, model_time` |
| `resolve SYMBOL...` | ticker → conid, exchange, long name, asset class | `symbol, con_id, primary_exchange, asset_class, long_name` |
| `resolve --from-positions` | underlying conid for every held option | `underlying, und_con_id, source: "ib-option"` |
| `chain SYMBOL [--months N]` | expiries, strikes, multiplier, exchanges | `expirations[], strikes[], trading_class, multiplier` |
| `orders` | open orders, read-only | `order_id, con_id, action, quantity, type, limit, tif, status` |
| `executions [--days N]` | fills; socket for today, Flex beyond | `exec_id, con_id, side, shares, price, time, commission` |
| `export [--out PATH]` | write the full payload atomically for zero-spawn readers | one JSON file, `as_of`, `schema` |
| `diff [--from ID] [--to ID]` | opened / closed / resized legs between snapshots | `opened[], closed[], changed[]` |

Contract hardening the same phase:

- `"schema": 1` in every payload; bump on breaking change, never on addition.
- Distinct exit codes: `3` gateway unreachable, `4` no data — today both are `1`,
  so a caller cannot tell "IB is down" from "nothing stored yet" and cannot
  decide whether retrying helps.
- A test asserting `json.loads(stdout)` succeeds for every `--json` command.
- Add to `SUMMARY_TAGS`: `FullInitMarginReq`, `FullMaintMarginReq`, `RegTEquity`,
  `RegTMargin`, `Leverage` — needed for `/api/balances` parity.
- New tables: `position_greeks`, `option_chains`, `open_orders`, `executions`,
  `underlying_conids`.

## 7. Verification risks

Each of these is a "test it, do not assume it" item.

1. **Delayed greeks.** `Ticker.modelGreeks` is populated from IB's model option
   computation tick. Whether it arrives under market data type 3 (delayed) for an
   account without an OPRA subscription is unconfirmed. If it does not, options
   are: real-time options data subscription, `calculateImpliedVolatility` /
   `calculateOptionPrice`, computing greeks locally from IV, or leaving greeks on
   the extension. Test before committing OH's RED list to this path — RED
   silently under-populates when deltas are missing, which is the failure mode
   that hides a real assignment risk.
2. **Execution history depth.** `reqExecutions()` returns the current session's
   fills, not OH's rolling seven days. Flex Web Service closes the gap
   (`IB_FLEX_TOKEN`, `IB_FLEX_QUERY_ID` already in `config.py`, `ibflex` already a
   dependency) but is a separate, slower, token-authenticated path.
3. **Seeing manual orders.** `reqAllOpenOrders` shows other clients' orders only
   with `OverrideTwsMasterClientID` set in `~/ibc/config.ini`.
4. **Market data line limits.** Batch greeks over a large option book can exceed
   the account's simultaneous market data lines; chunk and pace, like the
   extension already does with its snapshot polling.
5. **Startup cost on `/mnt/d`.** Measured on this box: `ib-agent --help` took ~5 s
   cold and `status` ~2 s warm, because the project lives on the Windows drive via
   DrvFs. That is interpreter + import overhead, not IB latency. Consequence: a
   subprocess per HTTP request is not viable for OH's request path — use `export`
   plus a file read there, and reserve subprocess calls for cron and scripts.
   Moving the venv to the Linux filesystem is the other lever.

## 8. Phases

Each phase ends with something runnable and verified.

**Phase 0 — packaging (done).** `man/ib-agent.1`, `.kiro/skills/ib-agent/SKILL.md`,
`scripts/install-cli.sh` installing a read-only-pinned, audited wrapper to
`~/.local/bin`, the man page to `~/.local/share/man`, and the skill to
`~/.kiro/skills`. Verified: wrapper runs, `man ib-agent` resolves, troff renders
without warnings, audit log written.

**Phase 1 — second username (user action).** `docs/ROADMAP.md` §1-2. Acceptance:
IBKR Mobile logged in on the primary username *and* `ib-agent positions` working
at the same time.

**Phase 2 — core extraction + contract: done.** `contract.py` (schema version,
exit codes), `api.py` (typed functions, no argparse, no printing, no exits),
`cli.py` reduced to an adapter, `SUMMARY_TAGS` extended. 95 tests pass, none
needing a Gateway; `tests/test_api.py` asserts by AST that `api.py` imports no
argparse, calls no `print` and never exits, so a future adapter cannot inherit
CLI concerns by accident.

**Phase 3 — the data OH needs: built 2026-08-17, unverified against a live
Gateway.** `market.py` (`resolve`, `resolve --from-positions`, `chain`, `greeks`)
and `activity.py` (`orders`, `executions`), each with an `api.py` payload builder,
a CLI subcommand and a table renderer. 152 tests pass, none needing a Gateway:
the IB objects are mapped by pure functions (`resolved_from_details`,
`chain_from_params`, `greeks_from_ticker`, `order_row_from_trade`,
`execution_row_from_fill`) which the tests drive with stubs, and
`tests/test_activity.py` greps the package for `placeOrder`/`whatIfOrder`/
`cancelOrder`/`reqGlobalCancel` so the read-only guarantee is enforced rather
than promised.

Still open, and only a live session can close it: acceptance is that every held
option gets a delta and that `resolve --from-positions` reproduces the four
known-good conid corrections (B, COIN, GDX, DOW). Both are blocked on the
Gateway login, not on code — see `docs/ROADMAP.md` §2. The specific unknowns are
whether the delayed feed carries `modelGreeks` (§7.1) and whether `orders`
returns anything before `OverrideTwsMasterClientID` is set.

Those checks are now written rather than described: `tests/test_live.py`, run
with `IB_LIVE_TESTS=1 uv run pytest tests/test_live.py -v -s`, skips itself
unless the port answers so the offline rule holds. The expected conids were
pulled from OH's own pin registry into
`tests/fixtures/underlying_conids.json` — `option_harvest_security_conids` for
B (780709675), COIN (481691285), GDX (229726316), FXI (31421120) and the one
manual pin SMCI (731466419), plus DOW (356576040) from
`option_harvest_securities` where the pin no longer survives. When that file's
expectations hold against IB, the pin registry has nothing left to do.

**Phase 4 — writer + export.** `watch` as a systemd user unit, writing
`data/exports/latest.json` atomically each cycle. Acceptance: `latest.json`
newer than one interval, and a reader never sees a partial file.

**Phase 5 — OH consumes it.** `src/lib/ibagent.ts`, routes fed from ib_agent,
extension paths deleted. See `option_harvester/docs/ib-agent-integration.md`.

**Phase 6 — orders.** Unchanged from `docs/ROADMAP.md` §3-5: paper first,
separate opt-in, `whatIf` preflight, dry-run default, kill switch.

## 9. Building before the second login exists

Phases 2-5 do not depend on phase 1. Switching login later is a configuration
change, provided the code stays account-agnostic — which is a property to
maintain deliberately, not a happy accident:

- **The login lives outside this project.** Username and password are only in
  `~/ibc/config.ini`. Switching means editing `IbLoginId` / `IbPassword` there
  and restarting the Gateway. No code, no `.env` change.
- **The account lives in one variable.** `IB_ACCOUNT` selects it; empty means all
  managed accounts. Never hardcode an account id, and never assume exactly one:
  positions, account values and payload metadata are already keyed by account.
- **If a real subaccount arrives** (a distinct account id holding its own
  positions, rather than a second username on the same account) then
  `IB_ACCOUNT` and the `--account` filter are what select it, and stored history
  will span both books. That is why every payload reports `accounts`.

While developing against the primary username, the constraint is not code, it is
the login: every Gateway login on that username competes with IBKR Mobile. Work
around it rather than through it.

1. **Most work needs no Gateway.** The `api.py` extraction, storage schema,
   filters, JSON shapes, exit codes, tests, and the TypeScript client are all
   offline. The 87 tests in `tests/` run with no IB session, and it must stay
   that way.
2. **Use the paper account for live paths.** Paper is a *separate username*, so
   the Gateway can hold a paper session without touching the mobile login. Set
   `IB_PORT=4002` and build `greeks`, `chain`, `resolve`, `orders` and
   `executions` against it. Shapes and field semantics are identical; only the
   book differs.
3. **Record fixtures once.** During one short live window, capture real
   responses for the option book (greeks, `undConId`, chain params) into
   `tests/fixtures/`, then develop offline against them. This doubles as the
   regression suite for the OH cutover.
4. **Batch the live checks.** Verification against the real book — does every
   held option get a delta, does `resolve --from-positions` reproduce the four
   known conid corrections — needs a few minutes on the live username. Schedule
   those, do not scatter them.

One thing to settle early, because it may need an IBKR subscription rather than
code: whether delayed data yields model greeks (§7.1). Market data entitlements
attach to the login, so if real-time options data turns out to be required, that
is a per-username purchase and worth knowing before the second username is
created rather than after.
