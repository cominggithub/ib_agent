# Goal

Two IBKR logins active at the same time:

| Login | Used by | Access |
|---|---|---|
| `human-user` (primary) | IBKR Mobile, Client Portal — human only | full |
| `agent-user` (agent) | this project's agent, headless IB Gateway | sync positions, place and cancel orders |

**Rule:** no agent, script or Gateway may log in as `human-user`. That username
is reserved for the human on mobile and Client Portal; every automated session
uses `agent-user`. Account is `U1234567` either way, so both see the same book.

IBKR allows only one trading session per username, so a second username is not
an optimisation — it is the only way both can be logged in at once. Everything
is account-scoped: orders the agent places appear on mobile, and positions
opened on mobile appear in the agent's reads.

What that scoping does and does not cover — including why IB watchlist writes
cannot come from the agent's session — is tabulated in
[SECOND-USERNAME-SCOPE.md](SECOND-USERNAME-SCOPE.md).

## Current state (2026-08-04)

Working: unattended Gateway login (IBC + Xvfb), live position/account sync,
filtered queries, watchlist quotes, sqlite history. All reads, no writes.

| Gate | Value | Meaning |
|---|---|---|
| `~/ibc/config.ini` `ReadOnlyApi` | `yes` | Gateway rejects order messages on the API socket |
| `.env` `IB_READONLY` | `true` | `ib_async` refuses order calls client-side |
| `src/ib_agent/` | no order code | nothing can trade even if both gates opened |
| `~/ibc/config.ini` `IbLoginId` | `human-user` | still the primary; must become `agent-user` once IBKR activates it (step 2) |

## Step 1 — second username (IBKR side, manual) — submitted 2026-08-12

Done in Client Portal → user menu → Settings → User & Access Rights:

- User role `syncagent` created (ref REDACTED).
- User `agent-user` submitted with that role (ref REDACTED), listed *Pending*.
  IBKR processes user applications received before 11:00 ET by the end of the
  next business day, so expect activation around 2026-08-14 Taipei time.

Caveat to verify on activation: the Users panel shows `agent-user` with
relationship *Account Holder*, i.e. a secondary username of the primary holder,
which inherits full rights — so the Funding / Account Settings withholding the
`syncagent` role intends may not bind. Open the user and check the rights screen;
if Funding is granted, rely on the IBKR-side backstops in step 5 instead.

For reference, the two paths the wizard offers:

- **Secondary username of the primary account holder.** Answer "Yes" to
  secondary user. Inherits full rights including trading. Limit: two usernames
  for the primary holder.
- **Separate added user.** The wizard grants rights per group: User Settings,
  Trading, Reporting, Funding, Account Settings. Grant Trading, withhold
  Funding and Account Settings — the agent can trade but can never move money.

Remaining before step 2:

1. `./scripts/gateway-down.sh`, then log into Client Portal once as
   `agent-user` to confirm details and set its final password.
2. Enroll IB Key 2FA for `agent-user` (IBKR Mobile supports multiple users in
   one app).
3. Market data subscriptions only if live quotes are wanted; otherwise the
   delayed feed already used by `watchlist quotes` keeps working.

Rule to keep: never log into Client Portal with the agent's username while the
Gateway runs, or the Gateway will not auto-reconnect after the server reset.

## Step 2 — point the Gateway at the second username

In `~/ibc/config.ini` (mode `600`, backup exists as `config.ini.bak.20260804`),
once `agent-user` is active:

```ini
IbLoginId=agent-user
IbPassword=<agent-user password>
```

Do not make this edit while `agent-user` is still *Pending*: IBC would have
nothing to log into and every read would fail until IBKR activates the user.

### Attempt log 2026-08-14 — blocked on website first login

Config was switched to `agent-user` and the Gateway started. IBC typed the
credentials fine (`Logging in agent-user`, `Passed pwd authentication` in
`~/Jts/launcher.log`), but the session was then refused:

```
Authorization failed: You do not have access to this platform yet,
please first log into our website.    reason=DISCONNECT_AUTHORIZATION_FAILED
```

So a freshly activated username cannot use TWS/Gateway until it has logged in
to the IBKR website once. Prerequisite, in this order:

1. `./scripts/gateway-down.sh` (never hold a Gateway session on this username
   while logging in on the web).
2. Log in at interactivebrokers.com as `agent-user`, clear whatever the portal
   prompts for (details confirmation, password change, agreements).
3. `./scripts/gateway-up.sh` again.

**Second blocker: 2FA method.** `agent-user` is enrolled with *Mobile
Authenticator*, so the Gateway shows a `Second Factor Authentication` dialog
asking for a typed 6-digit code. IBC can auto-approve an IB Key *push* tap but
can never generate a TOTP, so this breaks unattended restarts and the weekly
IBKR server reset. Switch that username to IB Key in IBKR Mobile before relying
on `watch` or cron.

### Resolution 2026-08-21 — tooling around the code, not a fix for it

The website prerequisite is cleared: the Aug 17 and Aug 20 attempts no longer
show `You do not have access to this platform yet`, only the 2FA dialog. Attempts
on Aug 14, 17 and 20 all died the same way — the dialog opened, nobody typed a
code, IBC timed out after 180 s and re-logged in.

That is now automated as far as it can be. `ib-agent gateway up` starts Xvfb and
the Gateway, waits for the dialog, asks for a code *at the moment it is needed*,
types it on the headless display with XTEST and waits for the API port;
`ib-agent gateway code NNNNNN` answers a login already waiting, which is also how
a session dropped by the weekly reset is refreshed. Exit code `5` means "a code is
needed now", distinct from `3` so a scheduler asks a human instead of retrying.
Module `src/ib_agent/login.py`, memo `docs/GATEWAY-LOGIN.md`, tests
`tests/test_login.py` (offline: X display, port probe and clock are injected).

This makes one code per session workable by hand or by an agent that can message
the user. It does **not** make the login unattended — cron and `watch` across a
weekly reset still need IB Key on `agent-user`, so that switch remains the real
fix.

Operational note for driving that dialog on the headless display: `xdotool
type --window` does not work (Swing ignores XSendEvent). Use XTEST — click the
field by coordinate first, e.g. `DISPLAY=:99 xdotool mousemove 958 537 click 1`
then `xdotool type`. Screenshot with `DISPLAY=:99 import -window root out.png`.
Also note `scripts/gateway-down.sh` uses `pkill -f ibcalpha.ibc.IbcGateway`, so
never put that string in the command line that invokes it — the pattern matches
the invoking shell and kills it.

Restart: `./scripts/gateway-down.sh && ./scripts/gateway-up.sh` — one 2FA tap
for the new username. Verify with `uv run ib-agent status`, then log into
IBKR Mobile as `human-user` and confirm `ib-agent positions` still
works. That test is the goal's acceptance criterion.

## Step 3 — paper account first

The paper username is separate and listens on port 4002. Point `IB_PORT=4002`
at it and build the order path there before touching the live account.

## Step 4 — enable writes

Only after step 3 passes:

```ini
# ~/ibc/config.ini
ReadOnlyApi=no
```

```dotenv
# .env
IB_READONLY=false
```

Keep read-only as the default and require an explicit opt-in for order
commands, so a stray invocation cannot trade.

## Step 5 — order layer to build

- `orders.py`: place (limit/market/stop, options included), cancel, modify;
  every order round-tripped through `whatIfOrder` first for margin impact.
- `ib-agent orders list|place|cancel` with `--json`, plus a `--dry-run` default
  and a required confirmation flag for live submission.
- Persist submissions and fills to sqlite alongside snapshots, so the agent can
  reconcile its own intent against IBKR's record.
- Set `OverrideTwsMasterClientID` in `~/ibc/config.ini` so the agent also sees
  orders placed manually from mobile, not just its own.
- Account-level backstops in IBKR: precautionary order size/value limits, which
  the code cannot bypass.
- Kill switch: cancel-all command plus a hard cap on order count per run.

## Risk notes

- The agent's password sits in plaintext in `~/ibc/config.ini` on this WSL box.
  A write-capable username widens what that file exposes; this is the main
  argument for withholding Funding rights.
- Product trading permissions (options, futures) are account-level, so the
  second user trades the same instruments as the primary.
- `ExistingSessionDetectedAction=primaryoverride` currently lets a new login
  displace the Gateway. Once the usernames are split this should stop mattering,
  but if the Gateway is ever displaced unexpectedly, that setting is the reason.
