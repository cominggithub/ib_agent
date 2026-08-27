# Session log — 2026-08-27

Handoff for a fresh context. Read this first, then `AGENTS.md`.

## Step 0 — the account is locked out (found 2026-08-27, must be fixed first)

The Gateway launched on Aug 21 was never logged in and never stopped. IBC retries
for as long as it lives, so over three days it made **536 login attempts**, was
throttled **267 times** with escalating backoff (up to ~5 minutes), and at
2026-08-24 13:06 ended on a dialog reading:

```
UNRECOGNIZED USERNAME OR PASSWORD
Passwords are case sensitive.
If you need to retrieve your username or reset your password, you can do so here.
```

It then sat idle for three days; screenshot confirmed on the headless display
before the process was stopped on Aug 27. So IBKR no longer accepts the
credentials for `agent-user` — locked, or the password was invalidated.

**User action, before anything else:** log in to Client Portal (as the human
username, or use the password-reset link), check the status of `agent-user`, and
reset its password. Then update `IbPassword` in `~/ibc/config.ini` (mode 600,
backup at `config.ini.bak.20260804`). Nothing in this project can proceed until a
login is accepted.

Guard added the same day so this cannot recur: `run_login` now stops a Gateway it
launched itself when the outcome means no code is coming
(`no_dialog`/`timeout`/`code_rejected`/`no_process` — `login.FATAL_REASONS`), and
the CLI warns loudly when it leaves one waiting for a human. Never leave
`gateway up` pending for hours, and never loop it.

## State

Published at `github.com/cominggithub/ib_agent` (public), two commits on `main`:

| Commit | Contents |
|---|---|
| `315d7a1` | the project: 42 files, CLI + api + store + 2FA login |
| `f96eea8` | secret hygiene: pre-commit scanner, `docs/SECRETS.md`, `.secrets/` ignored |

`uv run pytest` → **197 passed, 7 skipped**. The 7 skips are `tests/test_live.py`,
which refuses to run without a Gateway. That skip is the honest marker of what is
still unverified; it is not a pass.

Phases (from `docs/OH-INTEGRATION-PLAN.md` §8): 0 packaging **done**,
1 second username **blocked, see below**, 2 core+contract **done**,
3 the data OH needs **built, unverified live**, 4 writer+export **not started**,
5 OH consumes it **not started**, 6 orders **not started, deliberately**.

## The one blocker

**No Gateway session has ever completed as `agent-user`.** IBC logs in, IBKR shows
a `Second Factor Authentication` dialog wanting a typed 6-digit Mobile
Authenticator code, and IBC cannot produce one — it can only auto-approve an IB
Key *push* tap. Attempts on Aug 14, 17, 20, 21 all ended there.

Everything around the code is now automated (`src/ib_agent/login.py`):

```bash
ib-agent gateway up --no-prompt --json   # exit 5 = "a code is needed now"
# ask the user for a code that has JUST refreshed, then within seconds:
ib-agent gateway code 123456 --json      # types it via XTEST, waits for the port
ib-agent gateway up                      # interactive: prompts at the right moment
ib-agent gateway status --json           # includes dialog_expires_in
```

Two codes were lost on 2026-08-21 before the guards existed. Both guards are in
place and unit-tested, but **neither has been proven against a real IBKR login**:

- a dialog IBC has just closed lingers in the X tree; typing into it loses the
  code silently. Now refused before typing → `lost_race`, "IBKR never saw it".
- a code already in hand is not spent on a dialog about to expire → `dialog_stale`.

## Next steps, in order

1. **Reset the credentials** (step 0 above). Nothing else can be verified until
   IBKR accepts a login for `agent-user` again.
2. **Get a session up.** `gateway up --no-prompt`, ask the user for a fresh code,
   `gateway code NNNNNN`. Ask only when a dialog is actually open — check
   `dialog_expires_in` from `gateway status --json` first. Codes live ~30 s;
   turnaround must be seconds, not a minute. If the user cannot supply a code
   right now, run `gateway down` rather than leaving the login pending.
3. **Run the live acceptance.** `IB_LIVE_TESTS=1 uv run pytest tests/test_live.py -v -s`.
   Two questions decide whether phase 3 is real: does *every* held option get a
   `delta` under delayed market data (type 3), and does `resolve --from-positions`
   reproduce the six known conids in `tests/fixtures/underlying_conids.json`?
   If greeks come back empty, the options are in `docs/OH-INTEGRATION-PLAN.md` §7.1
   — an options data subscription, `calculateImpliedVolatility`, local greeks, or
   leaving greeks on the extension. This may cost money, not code.
4. **Switch `agent-user` to IB Key** (user action, IBKR Mobile). The real fix: it
   is the only route to a login that survives a reboot or IBKR's weekly reset
   without a human. Then set `SecondFactorDevice=IBKEY` in `~/ibc/config.ini`,
   which is currently empty.
5. **Phase 4 — `export` and `diff`.** Both are in the plan (§6) and absent from
   `cli.py`. OH needs `export` specifically: interpreter startup on `/mnt/d` is
   2–5 s, so a subprocess per HTTP request is not viable; `watch` should write
   `data/exports/latest.json` atomically and OH should read the file.
6. **Tell the user when a code is wanted.** Nothing notifies today. A systemd user
   timer probing the port and reporting exit 5 would close the gap.
7. **Phase 5 — OH consumes it.** `src/lib/ibagent.ts`, routes fed from ib_agent,
   extension paths deleted. Only three things stay in the extension: IB watchlist
   read, IB watchlist write, per-position what-if margin.
8. **In option_harvester, not here:** its write routes have no authentication and
   prod binds a port reachable outside the NAT. Bind to localhost or require a
   shared secret. This migration does not fix it.

## Facts worth not re-deriving

- **XTEST only.** `xdotool type --window` is silently dropped by Swing. Move the
  pointer onto the field, click, then type to the focused window.
- **IBC's 180 s dialog timeout does not always fire.** A dialog opened 17:36:48 on
  Aug 21 still accepted input at 17:42:46. So window existence is ground truth and
  `dialog_remaining()` returns `None` rather than a negative number — it only
  vetoes a close that is provably imminent.
- **IBKR throttles repeated logins**: `Too many failed login attempts. Please wait
  58 seconds`, escalating to minutes, and after enough of them it stops accepting
  the credentials entirely. Never loop `gateway up`, and never leave a login
  pending; `login.FATAL_REASONS` now stops a Gateway we launched for exactly this
  reason.
- **`You do not have access to this platform yet`** appears *after* a timed-out
  2FA, not only when a username has never used the website. It is a symptom, not
  necessarily the website prerequisite.
- **Never** put `ibcalpha.ibc.IbcGateway` in a command line that invokes
  `gateway-down.sh`, and never `pgrep -f` it from a shell whose own command line
  contains it — the pattern matches the invoking shell. Use `pgrep -f 'ibc[a]lpha…'`.
- **Exit codes are a contract**: 0 ok, 1 error, 2 usage, 3 gateway unreachable
  (retry may help), 4 no data (retry will not), 5 a human must supply a 2FA code,
  130 interrupted. `5` is not `3` on purpose.
- **Per-position what-if margin stays out** by design: `whatIfOrder` is delivered
  as an order message, so supporting it means opening the read-only gate for the
  whole session. `docs/OH-INTEGRATION-PLAN.md` §4.
- **Session lifetime is unverified.** `AutoRestartTime=11:45 PM` is documented to
  reuse the existing authentication, implying roughly one code per week. No session
  here has ever lived long enough to confirm it. If the nightly restart does prompt,
  it becomes one code per day and step 3 stops being optional.

## Secrets

Tracked files use `agent-user`, `human-user`, `U1234567`, `<prod-host>:<port>`.
Real values live only in `.secrets/` (gitignored) and `~/ibc/config.ini` (outside
the repo). `./scripts/install-hooks.sh` installs a pre-commit scanner that refuses
a commit containing a real identifier; it reads its patterns from
`.secrets/patterns.txt`, which is untracked. Verified: history contains zero
occurrences of every real value, and GitHub serves 404 for `.secrets/` and `.env`.
Full policy, including what to do if something leaks (rotate first, rewrite
second): `docs/SECRETS.md`.
