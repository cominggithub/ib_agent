"""Complete the Gateway's two-factor login without a human at a screen.

IBC can auto-approve an IB Key *push* tap, but the agent's username
(`agent-user`) is enrolled with **Mobile Authenticator**, which issues a
6-digit code that only the human's phone can produce. IBKR asks for it in a
Swing dialog drawn on the headless X display, so nothing about that login is
unattended: someone has to read a number off a phone.

What this module removes is everything *else* that used to be manual. One
command - `ib-agent gateway up` - starts Xvfb, starts the Gateway, waits for the
dialog, asks for the code at the moment it is needed, types it, and waits for
the API port. The same machinery serves a mid-life session refresh (`ib-agent
gateway code 123456`) after IBKR's weekly server reset drops the login.

Three constraints shape the design, each learned the hard way:

* **The code lives about 30 seconds.** So the dialog is opened *first* and the
  code requested only once it is on screen. Asking up front, then spending 25 s
  launching a JVM, reliably submits an expired code.
* **Swing ignores synthetic XSendEvent input**, which is what `xdotool type
  --window` sends: the keystrokes vanish with no error. Input has to go through
  XTEST - move the pointer onto the field, click to focus, then type to whatever
  window holds focus.
* **A rejected code looks like success at first.** The dialog closes either way.
  What distinguishes them is what happens next: the port opens, or IBC logs in
  again and a *new* dialog appears. So submission is judged by window id, not by
  the dialog merely going away.

Everything that touches the outside world - xdotool, the port probe, the launch
script, the clock - is injected, so the state machine is testable with no X
server, no Gateway and no waiting. See tests/test_login.py.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import subprocess
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Protocol, Sequence

from . import gateway
from .config import Settings

DIALOG_TITLE = "Second Factor Authentication"

# Outcome reasons. These reach consumers through the `--json` payload, so treat
# them as part of the contract: add freely, rename nothing.
REASON_ALREADY_UP = "already_up"
REASON_LOGGED_IN = "logged_in"
REASON_NEEDS_CODE = "needs_code"
REASON_CODE_REJECTED = "code_rejected"
REASON_NO_DIALOG = "no_dialog"
REASON_NO_PROCESS = "no_process"
REASON_TIMEOUT = "timeout"
REASON_DIALOG_STALE = "dialog_stale"
REASON_LOST_RACE = "lost_race"

# IBC closes an unanswered dialog after `SecondFactorAuthenticationTimeout`
# seconds and logs in again. Typing into one that is about to close loses the
# code silently - observed twice on 2026-08-21 - so a dialog with less than this
# much life left is left alone in favour of the next one.
MIN_DIALOG_SECONDS = 25.0
DEFAULT_DIALOG_LIFETIME = 180.0

DIALOG_OPEN_MARKER = "Second Factor Authentication initiated"
IBC_LOG_DIR = Path(os.getenv("IBC_LOG_DIR", str(Path.home() / "ibc" / "logs")))
IBC_CONFIG = Path(os.getenv("IBC_INI", str(Path.home() / "ibc" / "config.ini")))
_LOG_TIMESTAMP = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}):(\d{3})")

# IBKR's Mobile Authenticator issues 6 digits; the range is loose so a future
# 8-digit token does not need a code change to be accepted.
CODE_PATTERN = re.compile(r"^\d{6,8}$")

_GEOMETRY_PATTERN = re.compile(
    r"Position:\s*(-?\d+),(-?\d+).*?Geometry:\s*(\d+)x(\d+)", re.DOTALL
)


class LoginError(RuntimeError):
    """The login could not even be attempted (bad code, missing xdotool)."""


# --- talking to the X display ----------------------------------------------


class Runner(Protocol):
    """Runs a command against a given X display."""

    def __call__(
        self, cmd: Sequence[str], display: str, timeout: float = ...
    ) -> subprocess.CompletedProcess[str]: ...


def run_command(
    cmd: Sequence[str], display: str, timeout: float = 15.0
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(cmd),
            env={**os.environ, "DISPLAY": display},
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:  # xdotool / import not installed
        raise LoginError(
            f"{cmd[0]} is not installed; install it with: sudo apt install xdotool imagemagick"
        ) from exc


@dataclass(frozen=True)
class Geometry:
    """Where the 2FA dialog sits on screen.

    The two click targets are derived as fractions of the dialog rather than
    hardcoded pixels, so a different Gateway build or font size does not silently
    start clicking the wrong place. Measured against build 10.37.1q, whose dialog
    is 292x139 at 814,470: the entry field centre lands on (960, 536) and the OK
    button on (917, 582), matching the observed (958, 537) and (918, 583).
    """

    x: int
    y: int
    width: int
    height: int

    @property
    def field_point(self) -> tuple[int, int]:
        return (self.x + self.width // 2, self.y + int(self.height * 0.48))

    @property
    def ok_point(self) -> tuple[int, int]:
        return (self.x + int(self.width * 0.355), self.y + int(self.height * 0.81))


@dataclass(frozen=True)
class Dialog:
    window_id: str
    geometry: Geometry


def parse_geometry(text: str) -> Geometry:
    match = _GEOMETRY_PATTERN.search(text)
    if not match:
        raise LoginError(f"could not parse window geometry from: {text!r}")
    x, y, width, height = (int(g) for g in match.groups())
    return Geometry(x=x, y=y, width=width, height=height)


def require_tools(display: str, runner: Runner = run_command) -> None:
    """Fail before launching anything if the display cannot be driven."""
    result = runner(["xdotool", "--version"], display, timeout=10)
    if result.returncode != 0:
        raise LoginError(f"xdotool is unusable: {result.stderr.strip()}")


def window_exists(window_id: str, display: str, runner: Runner = run_command) -> bool:
    """True if the window is still mapped.

    Checked immediately before typing: a dialog IBC has just closed lingers in
    the X tree long enough to be found by `search`, and typing into it throws the
    code away with no error anywhere. That cost two of the user's codes.
    """
    result = runner(["xdotool", "getwindowgeometry", window_id], display, timeout=10)
    return result.returncode == 0


# --- how much life the dialog has left -------------------------------------
#
# IBKR's code expires in ~30 s and IBC's dialog in 180 s, so a submission has to
# fit inside both windows. The dialog's age is not visible on screen, but IBC
# logs the moment it opened, which is enough to decide whether typing is worth
# it or whether to wait for the next one.


def ibc_log_path(log_dir: Path | None = None) -> Path | None:
    """The IBC diagnostics file currently being written (one per weekday)."""
    directory = log_dir or IBC_LOG_DIR
    candidates = sorted(
        directory.glob("ibc-*.txt"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    return candidates[0] if candidates else None


def dialog_lifetime(config_path: Path | None = None) -> float:
    """IBC's `SecondFactorAuthenticationTimeout`, the dialog's total lifetime."""
    path = config_path or IBC_CONFIG
    try:
        for line in path.read_text().splitlines():
            if line.startswith("SecondFactorAuthenticationTimeout="):
                value = line.split("=", 1)[1].strip()
                return float(value) if value else DEFAULT_DIALOG_LIFETIME
    except (OSError, ValueError):
        pass
    return DEFAULT_DIALOG_LIFETIME


def dialog_opened_at(log_dir: Path | None = None) -> dt.datetime | None:
    """When IBC last announced a 2FA prompt, from its log; None if unknown."""
    path = ibc_log_path(log_dir)
    if path is None:
        return None
    try:
        # The file is tens of KB; the tail is all that matters.
        lines = path.read_text(errors="replace").splitlines()[-4000:]
    except OSError:
        return None
    for line in reversed(lines):
        if DIALOG_OPEN_MARKER not in line:
            continue
        match = _LOG_TIMESTAMP.match(line)
        if not match:
            return None
        stamp = dt.datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
        return stamp.replace(microsecond=int(match.group(2)) * 1000)
    return None


def dialog_remaining(
    log_dir: Path | None = None,
    config_path: Path | None = None,
    now: Callable[[], dt.datetime] = dt.datetime.now,
) -> float | None:
    """Seconds before IBC closes the current dialog, or None if not knowable.

    None means "no opinion" - the caller should proceed rather than refuse, so a
    missing or unreadable IBC log degrades to the old behaviour instead of
    blocking every login.

    A *negative* result is also no opinion, not "already dead". Observed
    2026-08-21: a dialog opened at 17:36:48 was still accepting input at 17:42:46,
    six minutes past the configured 180 s, with no Closed event logged. IBC's
    timeout does not always fire, so the window's existence is the ground truth
    and this clock only warns about a close that is provably imminent.
    """
    opened = dialog_opened_at(log_dir)
    if opened is None:
        return None
    left = dialog_lifetime(config_path) - (now() - opened).total_seconds()
    return left if left > 0 else None


def find_dialog(display: str, runner: Runner = run_command) -> Dialog | None:
    """The 2FA dialog if IBKR is currently asking for a code, else None."""
    found = runner(["xdotool", "search", "--name", DIALOG_TITLE], display, timeout=10)
    window_ids = [line.strip() for line in found.stdout.splitlines() if line.strip()]
    if not window_ids:
        return None
    # Last match wins: after a re-login the newest window is the live one.
    window_id = window_ids[-1]
    geometry = runner(["xdotool", "getwindowgeometry", window_id], display, timeout=10)
    return Dialog(window_id=window_id, geometry=parse_geometry(geometry.stdout))


def validate_code(raw: str) -> str:
    """Normalise a human-supplied code, rejecting anything that cannot be one."""
    code = re.sub(r"[\s-]", "", raw or "")
    if not CODE_PATTERN.match(code):
        raise LoginError(f"not a 6-digit authenticator code: {raw!r}")
    return code


def submit_code(
    dialog: Dialog,
    code: str,
    display: str,
    runner: Runner = run_command,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Type `code` into the dialog and confirm it, via XTEST.

    The click is what makes this work: without it the keystrokes go to whatever
    had focus, and Swing will not accept the `--window` form at all.

    Returns False if the window vanished before the keystrokes could be sent -
    the caller must then treat the code as unused rather than as refused, so the
    human is asked for a fresh one instead of being told IBKR rejected it.
    """
    code = validate_code(code)
    if not window_exists(dialog.window_id, display, runner):
        return False

    field_x, field_y = dialog.geometry.field_point
    runner(["xdotool", "windowactivate", "--sync", dialog.window_id], display, timeout=10)
    runner(["xdotool", "mousemove", str(field_x), str(field_y), "click", "1"], display)
    # A short inter-key delay: the field is a formatted input and dropping a
    # digit yields a wrong-code retry, which costs a whole 30-second cycle.
    runner(["xdotool", "type", "--delay", "60", code], display)
    if not window_exists(dialog.window_id, display, runner):
        return False  # closed under us mid-type; the code never reached IBKR
    runner(["xdotool", "key", "--clearmodifiers", "Return"], display)

    # Return is the OK button's accelerator, but only while the field has focus.
    # If the dialog is still up a moment later, press the button itself.
    sleep(1.5)
    if window_exists(dialog.window_id, display, runner):
        ok_x, ok_y = dialog.geometry.ok_point
        runner(["xdotool", "mousemove", str(ok_x), str(ok_y), "click", "1"], display)
    return True


def screenshot(display: str, path: str, runner: Runner = run_command) -> str:
    """Capture the headless display; the only way to see what IBKR is showing."""
    runner(["import", "-window", "root", path], display, timeout=30)
    return path


# --- the login state machine ----------------------------------------------


@dataclass(frozen=True)
class LoginResult:
    ok: bool
    reason: str
    attempts: int
    detail: str = ""
    gateway_stopped: bool = False


# Outcomes after which a Gateway we started ourselves must not be left running.
# IBC retries a failed login for as long as it lives: on 2026-08-24 a Gateway
# launched on the 21st and left waiting for a code that never came reached 536
# login attempts over three days, was throttled 267 times with escalating
# backoff, and ended on "Unrecognized Username or Password" - IBKR had stopped
# accepting the credentials. A detached retry loop is worse than no session, so
# these reasons trigger a shutdown.
FATAL_REASONS = frozenset(
    {REASON_NO_DIALOG, REASON_TIMEOUT, REASON_CODE_REJECTED, REASON_NO_PROCESS}
)

# ... whereas these mean a human is about to send a code within seconds. Killing
# the Gateway here would throw away a launch that is about to succeed.
PENDING_REASONS = frozenset(
    {REASON_NEEDS_CODE, REASON_DIALOG_STALE, REASON_LOST_RACE}
)


# Given the attempt number, return a code - or None to say "I cannot supply
# one", which the CLI turns into EXIT_NEEDS_2FA rather than a hang.
CodeProvider = Callable[[int], str | None]


def _wait_for(
    predicates: dict[str, Callable[[], object]],
    timeout: float,
    poll: float,
    sleep: Callable[[float], None],
    now: Callable[[], float],
) -> tuple[str, object] | None:
    """Poll several conditions at once, returning the first that fires.

    Order matters: the port winning over the dialog means an IB Key push tap or
    a session that needed no second factor is reported as success, not as a
    missing dialog.
    """
    deadline = now() + timeout
    while True:
        for name, predicate in predicates.items():
            value = predicate()
            if value:
                return name, value
        if now() >= deadline:
            return None
        sleep(poll)


def run_login(
    settings: Settings,
    *,
    code_provider: CodeProvider,
    launch: bool = True,
    attempts: int = 3,
    dialog_timeout: float = 120.0,
    login_timeout: float = 180.0,
    settle_seconds: float = 4.0,
    poll: float = 2.0,
    min_dialog_seconds: float = MIN_DIALOG_SECONDS,
    wait_for_fresh: bool = True,
    stop_on_failure: bool = True,
    runner: Runner = run_command,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
    remaining: Callable[[], float | None] = dialog_remaining,
) -> LoginResult:
    """Bring the Gateway to a logged-in state, asking for codes as needed.

    `launch=False` refuses to start anything and only feeds a code to a dialog
    that is already up - the session-refresh case, where starting a second
    Gateway would be wrong.

    `wait_for_fresh=False` reports a nearly-expired dialog instead of waiting for
    the next one. That is right when the code is already in hand (`--code`): it
    would expire during the wait, so the honest move is to say so and let the
    human send another. When prompting interactively the default is to wait,
    because the code has not been read off the phone yet.
    """
    display = settings.xvfb_display
    launched = False

    def port_open() -> bool:
        return gateway.is_port_open(settings.host, settings.port)

    if port_open():
        return LoginResult(True, REASON_ALREADY_UP, 0, "API port already listening")

    require_tools(display, runner)

    if launch:
        if not gateway.gateway_process_running():
            result = gateway.launch()
            if result.returncode != 0:
                return LoginResult(
                    False,
                    REASON_NO_PROCESS,
                    0,
                    f"gateway-up.sh failed: {(result.stderr or result.stdout).strip()[-400:]}",
                )
            launched = True
    elif not gateway.gateway_process_running():
        return LoginResult(
            False,
            REASON_NO_PROCESS,
            0,
            "no Gateway process to answer; start one with 'ib-agent gateway up'",
        )

    outcome = _drive_login(
        settings,
        display=display,
        code_provider=code_provider,
        attempts=attempts,
        dialog_timeout=dialog_timeout,
        login_timeout=login_timeout,
        settle_seconds=settle_seconds,
        poll=poll,
        min_dialog_seconds=min_dialog_seconds,
        wait_for_fresh=wait_for_fresh,
        runner=runner,
        sleep=sleep,
        now=now,
        remaining=remaining,
        port_open=port_open,
    )

    # A Gateway we started and could not log in must not be left behind: IBC
    # retries for as long as it lives, and IBKR answers a long retry loop by
    # refusing the credentials outright. Only reasons where no code is coming
    # count - if a human is about to send one, the process is still useful.
    if launched and stop_on_failure and outcome.reason in FATAL_REASONS:
        gateway.stop()
        return replace(
            outcome,
            gateway_stopped=True,
            detail=f"{outcome.detail}; stopped the Gateway rather than leave it "
            "retrying (IBKR locks a username that retries too long)",
        )
    return outcome


def _drive_login(
    settings: Settings,
    *,
    display: str,
    code_provider: CodeProvider,
    attempts: int,
    dialog_timeout: float,
    login_timeout: float,
    settle_seconds: float,
    poll: float,
    min_dialog_seconds: float,
    wait_for_fresh: bool,
    runner: Runner,
    sleep: Callable[[float], None],
    now: Callable[[], float],
    remaining: Callable[[], float | None],
    port_open: Callable[[], bool],
) -> LoginResult:
    """Answer prompts until the port opens, the codes run out, or time does.

    Split from `run_login` so that the shutdown policy has exactly one place to
    inspect the outcome, rather than being repeated at every return.
    """

    def fresh_dialog() -> Dialog | None:
        """A dialog worth typing into: present, and not about to be closed."""
        dialog = find_dialog(display, runner)
        if dialog is None:
            return None
        left = remaining()
        if left is not None and left < min_dialog_seconds:
            return None
        return dialog

    # Holding a code that cannot be used is worth reporting at once rather than
    # after a minute of polling: it expires long before the next dialog opens.
    if not wait_for_fresh:
        current = find_dialog(display, runner)
        left = remaining()
        if current is not None and left is not None and left < min_dialog_seconds:
            return LoginResult(
                False,
                REASON_DIALOG_STALE,
                0,
                f"IBKR's dialog closes in {left:.0f}s, too soon to use a code; "
                "a fresh one opens within about a minute - send a new code then",
            )

    submitted = 0
    for attempt in range(1, attempts + 1):
        waited = _wait_for(
            {"port": port_open, "dialog": fresh_dialog},
            timeout=dialog_timeout,
            poll=poll,
            sleep=sleep,
            now=now,
        )
        if waited is None:
            return LoginResult(
                False,
                REASON_NO_DIALOG if submitted == 0 else REASON_TIMEOUT,
                submitted,
                f"no usable 2FA dialog and no API port within {dialog_timeout:.0f}s; "
                f"check logs/gateway-start.log",
            )
        kind, value = waited
        if kind == "port":
            return LoginResult(
                True,
                REASON_LOGGED_IN if submitted else REASON_ALREADY_UP,
                submitted,
                "API port is listening",
            )

        dialog = value
        assert isinstance(dialog, Dialog)
        code = code_provider(attempt)
        if code is None:
            return LoginResult(
                False,
                REASON_NEEDS_CODE,
                submitted,
                "IBKR is asking for a Mobile Authenticator code; supply one with "
                "'ib-agent gateway code <CODE>'",
            )

        if not submit_code(dialog, code, display, runner, sleep):
            return LoginResult(
                False,
                REASON_LOST_RACE,
                submitted,
                "the dialog closed before the code could be typed, so IBKR never "
                "saw it; wait for the next prompt and send a fresh code",
            )
        submitted += 1

        # Let the old dialog tear down before treating a visible dialog as a
        # fresh prompt, or the just-answered one is misread as a rejection.
        sleep(settle_seconds)
        outcome = _wait_for(
            {
                "port": port_open,
                "reprompt": lambda: _new_dialog(display, dialog.window_id, runner),
            },
            timeout=login_timeout,
            poll=poll,
            sleep=sleep,
            now=now,
        )
        if outcome is None:
            return LoginResult(
                False,
                REASON_TIMEOUT,
                submitted,
                f"code went in but no API port within {login_timeout:.0f}s and no new "
                "prompt; check the IBC log for what IBKR said",
            )
        if outcome[0] == "port":
            return LoginResult(True, REASON_LOGGED_IN, submitted, "API port is listening")
        # A new dialog means IBKR refused the code: expired, mistyped, or a
        # digit dropped on the way in. Worth one more try with a fresh code.

    return LoginResult(
        False,
        REASON_CODE_REJECTED,
        submitted,
        f"IBKR asked again after {submitted} code(s); codes expire in ~30s, so read "
        "one that has just refreshed",
    )


def _new_dialog(display: str, previous_id: str, runner: Runner) -> Dialog | None:
    dialog = find_dialog(display, runner)
    if dialog is None or dialog.window_id == previous_id:
        return None
    return dialog
