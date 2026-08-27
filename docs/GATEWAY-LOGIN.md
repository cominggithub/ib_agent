# Memo — logging the Gateway in, and why it takes a human

Written 2026-08-21, after three failed unattended login attempts (Aug 14, 17, 20).

## The situation in one paragraph

The agent's username `agent-user` is enrolled with IBKR **Mobile Authenticator**,
which issues a 6-digit code. IBC can auto-approve an IB Key *push* tap but can
never generate a code, so every Gateway login needs one number read off the
human's phone. Everything around that number is now automated: `ib-agent gateway
up` starts the display and the Gateway, waits for IBKR's dialog, takes a code,
types it on the headless display and waits for the API port. Once the port is
open, portfolio reads need nobody — the human cost is one code per session, not
one per read.

The permanent fix is still to switch that username to IB Key
(`docs/ROADMAP.md` §2). Until then this is the supported path.

## The two commands

```bash
# interactive: prompts for the code at the moment IBKR asks for it
ib-agent gateway up

# two-step, for an agent or script that gets the code from the human by message
ib-agent gateway up --no-prompt        # exit 5 == "a code is needed now"
ib-agent gateway code 123456           # submit it; waits for the API port
```

`gateway code` never launches a Gateway — it answers a login that is already
waiting. That is the session-refresh case after IBKR's weekly server reset drops
the connection.

Exit codes: `0` logged in, `3` gateway trouble (retry may help), `5` waiting for
a code only the human has. `5` is deliberately not `3`: a scheduler must not
read "needs a human" as "IB is down".

## Three things that cost a session each to learn

**A code lives about 30 seconds.** So the dialog is opened *first* and the code
requested only once it is on screen. Asking for a code up front, then spending
25 s launching a JVM, reliably submits an expired one. This is why `gateway up`
prompts late rather than taking `--code` and starting from scratch — the flag
exists, but it is only sound when a dialog is already up.

**Swing ignores synthetic XSendEvent input.** `xdotool type --window <id>` sends
exactly that: the keystrokes vanish, with no error and no clue. Input has to go
through XTEST — move the pointer onto the field, click to focus, then type to the
focused window. `tests/test_login.py` asserts `--window` never appears in any
command, so the fix cannot be undone by accident.

**A refused code looks like success at first.** The dialog closes either way.
What separates them is what happens next: the port opens, or IBC logs in again
and a *new* dialog appears. Submission is therefore judged by window id, not by
the dialog merely disappearing, and a rejection triggers a retry with a fresh
code instead of a false "logged in".

## Where the click lands

Measured on Gateway build 10.37.1q: the dialog is 292x139 at (814, 470), the
entry field at (958, 537), OK at (918, 583). The code derives both from the
window geometry as fractions (`0.48` and `0.81` of height) rather than hardcoding
pixels, so a different font size or build does not silently start clicking
somewhere harmless. `Geometry.field_point` and `.ok_point` in `login.py`.

## Debugging a login that will not go through

```bash
DISPLAY=:99 import -window root /tmp/gw.png    # see what IBKR is showing
DISPLAY=:99 xdotool search --name "." getwindowname %@   # list windows
tail -f ~/ibc/logs/ibc-3.23.0_GATEWAY-1037_$(date +%A).txt
tail -f logs/gateway-start.log
```

Messages seen and what they meant:

| Log line | Meaning |
|---|---|
| `Second Factor Authentication initiated` | the dialog is up; a code is needed now |
| `Re-login after second factor authentication timeout` | nobody answered within 180 s; IBC is retrying |
| `You do not have access to this platform yet, please first log into our website` | the username has never logged into interactivebrokers.com. Cleared for `agent-user` as of Aug 17 |
| `Authorization failed ... DISCONNECT_AUTHORIZATION_FAILED` | same cause as above |

Two traps in the surrounding scripts:

- `scripts/gateway-down.sh` runs `pkill -f ibcalpha.ibc.IbcGateway`. Never put
  that string in the command line that invokes it, and never use it as a `pgrep`
  pattern from a shell whose own command line contains it — the pattern matches
  the invoking shell. Use `pgrep -f 'ibc[a]lpha...'` when checking.
- `scripts/gateway-up.sh` waits for the port for 180 s by default. The login path
  needs control back while the dialog is still open, so it passes
  `GATEWAY_NO_WAIT=1` (also `--no-wait`), which returns as soon as the JVM is
  spawned.

## Rules that still hold

- Never log the Gateway in as `human-user`. That username is the human's, for
  IBKR Mobile and Client Portal. One trading session per username is why the
  second username exists at all.
- Never log into Client Portal as `agent-user` while the Gateway holds a
  session, or it will not auto-reconnect after the weekly server reset.
- The API session stays read-only: `ReadOnlyApi=yes` in `~/ibc/config.ini`,
  `IB_READONLY=true` in the installed wrapper, and no order-placing code in the
  package (enforced by a test).
