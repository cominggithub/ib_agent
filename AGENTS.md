# AGENTS.md

Answer portfolio questions by running this project's CLI, not by guessing.
Always add `--json` when you need to parse the result.

```bash
ib-agent <command> [filters] --json          # after ./scripts/install-cli.sh
cd /mnt/d/project/ib_agent && uv run ib-agent ...   # without it
```

Full reference: `man ib-agent`. The skill at `.kiro/skills/ib-agent/SKILL.md`
carries the short version.

The Gateway must be logged in; `ib-agent status` tells you. If the port is
closed, the login needs one 6-digit Mobile Authenticator code from the user's
phone — ask first, then:

```bash
ib-agent gateway up --no-prompt --json   # exit 5 = "a code is needed now"
ib-agent gateway code 123456 --json      # submit it; waits for the API port
```

Codes expire in ~30 s, so ask for one that has just refreshed. `gateway code`
also refreshes a session dropped by IBKR's weekly reset. Details:
`docs/GATEWAY-LOGIN.md`.

## Question -> command

| Question | Command |
|---|---|
| what do I hold? | `positions --json` |
| what expires soon? | `positions --dte-max 14 --sort dte --json` |
| my option book by expiry | `expiries --json` (add `-r put` / `-r call`) |
| exposure per underlying | `underlyings --json` |
| short puts on ETFs | `positions --right put --asset etf --json` |
| everything on GDX | `positions -u GDX --json` |
| biggest losers | `positions --sort pnl --reverse --limit 10 --json` |
| account value / cash | `sync --json` (net_liquidation) or `show --json` |
| price of a ticker | `watchlist quotes TSM SPY --json` |
| is GDX an ETF? | `instruments --json` |
| history of net liq | `history --json` |
| delta/theta/vega of the book | `greeks --json` (read `.totals`) |
| greeks of specific contracts | `greeks "GDX 2026-09-18 P 45" --json` |
| conid for a symbol | `resolve GDX --json` |
| underlying conid of held options | `resolve --from-positions --options --json` |
| expiries and strikes available | `chain GDX -e 2026-09 --json` |
| working orders | `orders --json` |
| fills today | `executions --json` (alias `fills`) |

Notes:

- `positions` hits the Gateway live each call (~3 s). Use `--stored` for the
  last snapshot when freshness does not matter, `--save` to persist a fetch.
- Quantities are negative for short positions; this book is mostly short
  option premium, so negative `market_value` is normal and `unrealized_pnl`
  positive means the short option lost value.
- `dte` is days to expiry; `asset_class` is `ETF`, `COMMON` or `ADR`.
- The API session is read-only (`ReadOnlyApi=yes`): no command can trade.
- The Gateway logs in as `agent-user` (account `U1234567`). Never point any
  automated session, script or `~/ibc/config.ini` at `human-user` — that
  username is the human's, for IBKR Mobile and Client Portal only, and IBKR
  allows one trading session per username. See `docs/ROADMAP.md`.
- `agent-user`, `human-user` and `U1234567` are placeholders throughout this
  repository, which is public. The real username, password and account id live
  only in `~/ibc/config.ini`, `.env` and `.secrets/`, none of them tracked.
  Never paste a real id into docs, tests or commit messages; a pre-commit hook
  refuses it (`./scripts/install-hooks.sh`). See `docs/SECRETS.md`.
- Money figures come from IB's account-update stream, delayed data outside
  market hours.

## Working on this project

- Tests: `uv run pytest`. Keep them offline — no test may need a live Gateway.
  The live acceptance checks live in `tests/test_live.py` and skip unless
  `IB_LIVE_TESTS=1` and the port answers: `IB_LIVE_TESTS=1 uv run pytest
  tests/test_live.py -v -s`. A skip there is not a pass — it is what keeps
  phase 3 marked unverified.
- This CLI is a published interface. Other projects parse its `--json`
  payloads, so treat their keys as a contract: add fields freely, rename or
  remove nothing without bumping `schema`.
- Read `docs/OH-INTEGRATION-PLAN.md` before adding commands for another
  project; it defines the access-control model and what deliberately stays out
  (order placement, per-position what-if margin).
- Never add order-placing code outside the phase described in
  `docs/ROADMAP.md`.
