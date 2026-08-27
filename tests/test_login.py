"""Tests for the 2FA login path.

The login is the one place where a human is unavoidably in the loop, which makes
it the easiest place to build something that only works when someone is watching.
So everything here runs offline: no X server, no Gateway, no 30-second waits.
The X display, the port probe, the launcher and the clock are all injected, and
what is asserted is the behaviour that actually bit us in production:

* the code is asked for *after* the dialog is up, never before (codes expire);
* keystrokes go through XTEST, never `xdotool type --window` (Swing drops it);
* a refused code is detected and retried rather than reported as success;
* no human at the terminal produces a distinct exit code, not a hang.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess

import pytest

from ib_agent import api, cli, gateway, login
from ib_agent.config import load_settings
from ib_agent.contract import EXIT_GATEWAY, EXIT_NEEDS_2FA, EXIT_OK, EXIT_USAGE

GEOMETRY_OUTPUT = """Window 2097310
  Position: 814,470 (screen: 0)
  Geometry: 292x139
"""


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("IB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("IB_DB_PATH", str(tmp_path / "test.sqlite3"))
    monkeypatch.setenv("IB_AUTO_START_GATEWAY", "false")
    monkeypatch.setenv("XVFB_DISPLAY", ":99")
    return load_settings()


class FakeX:
    """A scriptable stand-in for the X display.

    `dialogs` is a list of window ids to report on successive `xdotool search`
    calls, so a test can say "no dialog, then dialog 1, then dialog 2 (a
    reprompt)" without any timing. Ids in `dead` are still *found* by search but
    report a failed geometry query - that is exactly the zombie window IBC leaves
    behind for a moment after closing a dialog, and typing into it loses the code.
    """

    def __init__(self, dialogs: list[str] | None = None, dead: set[str] | None = None):
        self.dialogs = dialogs if dialogs is not None else []
        self.dead = dead or set()
        self.calls: list[list[str]] = []
        self.searches = 0

    def __call__(self, cmd, display, timeout=15.0):
        self.calls.append(list(cmd))
        if cmd[:2] == ["xdotool", "search"]:
            found = self.dialogs[min(self.searches, len(self.dialogs) - 1)] if self.dialogs else ""
            self.searches += 1
            return self._done(stdout=found)
        if cmd[:2] == ["xdotool", "getwindowgeometry"]:
            gone = cmd[2] in self.dead
            return self._done(stdout=GEOMETRY_OUTPUT, returncode=1 if gone else 0)
        return self._done()

    @staticmethod
    def _done(stdout: str = "", returncode: int = 0):
        return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")

    @property
    def typed(self) -> list[str]:
        return [c[-1] for c in self.calls if c[:2] == ["xdotool", "type"]]


@pytest.fixture
def no_waiting(monkeypatch):
    """Make sleeps free and the clock fast, so timeouts are logic not latency."""
    clock = {"t": 0.0}
    monkeypatch.setattr(login.time, "sleep", lambda s: clock.__setitem__("t", clock["t"] + s))
    return clock


def stub_gateway(monkeypatch, *, ports: list[bool], process: bool = True, launch_rc: int = 0):
    """Program the port probe: one boolean per call, last value repeating."""
    state = {"i": 0}

    def is_port_open(host, port, timeout=2.0):
        value = ports[min(state["i"], len(ports) - 1)]
        state["i"] += 1
        return value

    monkeypatch.setattr(gateway, "is_port_open", is_port_open)
    monkeypatch.setattr(gateway, "gateway_process_running", lambda: process)
    monkeypatch.setattr(
        gateway,
        "launch",
        lambda **kw: subprocess.CompletedProcess([], launch_rc, "launched", ""),
    )
    return state


def run(settings, runner, provider, clock, *, remaining=lambda: None, **kwargs):
    """Drive the state machine with every external dependency injected.

    `remaining` defaults to "no opinion" so a test never reads the real IBC log.
    """
    return login.run_login(
        settings,
        code_provider=provider,
        runner=runner,
        sleep=lambda s: clock.__setitem__("t", clock["t"] + s),
        now=lambda: clock["t"],
        poll=1.0,
        remaining=remaining,
        **kwargs,
    )


# --- reading the dialog off the screen -------------------------------------


def test_geometry_parses_and_targets_the_measured_pixels():
    geometry = login.parse_geometry(GEOMETRY_OUTPUT)
    assert (geometry.x, geometry.y, geometry.width, geometry.height) == (814, 470, 292, 139)
    # Observed on build 10.37.1q: field at (958, 537), OK at (918, 583). The
    # derived points must land inside those widgets, not merely near them.
    assert geometry.field_point == (960, 536)
    assert geometry.ok_point == (917, 582)


def test_unparseable_geometry_is_an_error_not_a_wild_click():
    with pytest.raises(login.LoginError):
        login.parse_geometry("Window 1\n  no position here\n")


def test_find_dialog_is_none_when_ibkr_is_not_asking():
    assert login.find_dialog(":99", FakeX()) is None


def test_find_dialog_prefers_the_newest_window():
    """After a re-login two windows share the title; the live one is the last."""
    fake = FakeX(dialogs=["111\n222\n"])
    dialog = login.find_dialog(":99", fake)
    assert dialog is not None and dialog.window_id == "222"


@pytest.mark.parametrize(
    "raw,expected",
    [("123456", "123456"), (" 123 456 ", "123456"), ("123-456", "123456"), ("12345678", "12345678")],
)
def test_codes_are_normalised(raw, expected):
    assert login.validate_code(raw) == expected


@pytest.mark.parametrize("raw", ["", "12345", "abcdef", "1234567890", "12 34 5x"])
def test_junk_is_rejected_before_it_reaches_ibkr(raw):
    with pytest.raises(login.LoginError):
        login.validate_code(raw)


def test_submit_uses_xtest_click_then_type_never_the_window_flag():
    """`xdotool type --window` is silently dropped by Swing. Guard the fix."""
    fake = FakeX(dialogs=["4242"])
    dialog = login.find_dialog(":99", fake)
    assert login.submit_code(dialog, "123456", ":99", fake, sleep=lambda s: None)

    verbs = [c[1] for c in fake.calls if c[0] == "xdotool"]
    assert verbs.index("mousemove") < verbs.index("type"), "must focus the field first"
    assert "key" in verbs, "must confirm with Return"
    assert fake.typed == ["123456"]
    assert not any("--window" in c for c in fake.calls)
    mousemove = next(c for c in fake.calls if c[1] == "mousemove")
    assert mousemove[2:4] == ["960", "536"]


def test_submitting_into_a_zombie_window_is_refused_not_silently_lost():
    """The 2026-08-21 defect: two codes typed into dialogs IBC had just closed."""
    fake = FakeX(dialogs=["4242"], dead={"4242"})
    dialog = login.find_dialog(":99", fake)
    assert login.submit_code(dialog, "123456", ":99", fake, sleep=lambda s: None) is False
    assert fake.typed == [], "must not type into a window that is already gone"


# --- how much life the dialog has left -------------------------------------


def write_ibc_log(directory, lines: str):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "ibc-3.23.0_GATEWAY-1037_Friday.txt"
    path.write_text(lines)
    return path


def test_dialog_open_time_comes_from_the_ibc_log(tmp_path):
    write_ibc_log(
        tmp_path,
        "2026-08-21 17:27:21:050 IBC: Second Factor Authentication initiated\n"
        "2026-08-21 17:31:37:022 IBC: Second Factor Authentication initiated\n"
        "2026-08-21 17:31:38:000 IBC: something else\n",
    )
    opened = login.dialog_opened_at(tmp_path)
    assert opened == dt.datetime(2026, 8, 21, 17, 31, 37, 22000), "must take the latest prompt"


def test_remaining_life_counts_down_from_the_configured_timeout(tmp_path):
    write_ibc_log(tmp_path, "2026-08-21 17:31:37:000 IBC: Second Factor Authentication initiated\n")
    config = tmp_path / "config.ini"
    config.write_text("SecondFactorAuthenticationTimeout=180\nIbLoginId=x\n")
    remaining = login.dialog_remaining(
        tmp_path, config, now=lambda: dt.datetime(2026, 8, 21, 17, 33, 37)
    )
    assert remaining == pytest.approx(60.0, abs=1)


def test_an_unreadable_log_yields_no_opinion_rather_than_a_block(tmp_path):
    """Losing the log must degrade to the old behaviour, not refuse every login."""
    assert login.dialog_opened_at(tmp_path / "nope") is None
    assert login.dialog_remaining(tmp_path / "nope") is None


def test_an_overdue_dialog_is_no_opinion_not_a_refusal(tmp_path):
    """Observed 2026-08-21: a dialog outlived its 180 s and still took input.

    IBC's timeout does not always fire, so the clock must not veto a window that
    demonstrably exists - otherwise a usable prompt is skipped forever.
    """
    write_ibc_log(tmp_path, "2026-08-21 17:36:48:000 IBC: Second Factor Authentication initiated\n")
    config = tmp_path / "config.ini"
    config.write_text("SecondFactorAuthenticationTimeout=180\n")
    overdue = login.dialog_remaining(
        tmp_path, config, now=lambda: dt.datetime(2026, 8, 21, 17, 42, 46)
    )
    assert overdue is None


def test_missing_config_falls_back_to_the_documented_default(tmp_path):
    assert login.dialog_lifetime(tmp_path / "nope.ini") == login.DEFAULT_DIALOG_LIFETIME


# --- the state machine -----------------------------------------------------


def test_a_listening_port_short_circuits_everything(isolated_settings, monkeypatch, no_waiting):
    stub_gateway(monkeypatch, ports=[True])
    fake = FakeX()
    result = run(isolated_settings, fake, lambda a: pytest.fail("must not ask"), no_waiting)
    assert result.ok and result.reason == login.REASON_ALREADY_UP
    assert fake.calls == [], "no need to touch the display when the port answers"


def test_code_is_requested_only_once_the_dialog_is_on_screen(
    isolated_settings, monkeypatch, no_waiting
):
    """The ordering that matters: a code fetched early is a code expired early."""
    stub_gateway(monkeypatch, ports=[False, False, False, True])
    fake = FakeX(dialogs=["", "555"])
    asked_after: list[int] = []

    def provider(attempt):
        asked_after.append(fake.searches)
        return "123456"

    result = run(isolated_settings, fake, provider, no_waiting)
    assert result.ok and result.reason == login.REASON_LOGGED_IN
    assert result.attempts == 1
    assert asked_after and asked_after[0] >= 2, "asked before the dialog appeared"
    assert fake.typed == ["123456"]


def test_push_approval_counts_as_success_without_a_code(
    isolated_settings, monkeypatch, no_waiting
):
    """If IB Key is ever enabled the port opens with no dialog; not a failure."""
    stub_gateway(monkeypatch, ports=[False, True])
    result = run(isolated_settings, FakeX(), lambda a: pytest.fail("must not ask"), no_waiting)
    assert result.ok


def test_a_refused_code_is_retried_with_a_fresh_one(isolated_settings, monkeypatch, no_waiting):
    # port stays shut through the first submission; a *new* window id is the
    # tell-tale of a rejection, then the second code gets in.
    stub_gateway(monkeypatch, ports=[False, False, False, False, False, False, True])
    fake = FakeX(dialogs=["100", "100", "200", "200"])
    codes = iter(["111111", "222222"])
    result = run(isolated_settings, fake, lambda a: next(codes), no_waiting)
    assert result.ok
    assert result.attempts == 2, "the first code must not be reported as accepted"
    assert fake.typed == ["111111", "222222"]


def test_exhausting_the_attempts_reports_rejection(isolated_settings, monkeypatch, no_waiting):
    stub_gateway(monkeypatch, ports=[False])
    fake = FakeX(dialogs=["1", "2", "3", "4", "5", "6", "7", "8"])
    result = run(isolated_settings, fake, lambda a: "123456", no_waiting, attempts=2)
    assert not result.ok and result.reason == login.REASON_CODE_REJECTED
    assert result.attempts == 2
    assert "30s" in result.detail, "tell the human why it keeps failing"


def test_no_code_available_ends_cleanly_rather_than_hanging(
    isolated_settings, monkeypatch, no_waiting
):
    stub_gateway(monkeypatch, ports=[False])
    result = run(isolated_settings, FakeX(dialogs=["7"]), lambda a: None, no_waiting)
    assert not result.ok and result.reason == login.REASON_NEEDS_CODE
    assert "gateway code" in result.detail


def test_neither_dialog_nor_port_is_reported_as_no_dialog(
    isolated_settings, monkeypatch, no_waiting
):
    stub_gateway(monkeypatch, ports=[False])
    result = run(
        isolated_settings, FakeX(), lambda a: pytest.fail("must not ask"), no_waiting,
        dialog_timeout=5.0,
    )
    assert not result.ok and result.reason == login.REASON_NO_DIALOG
    assert "gateway-start.log" in result.detail


def test_code_refresh_refuses_to_start_a_second_gateway(
    isolated_settings, monkeypatch, no_waiting
):
    """`gateway code` answers an existing prompt; it must never launch one."""
    stub_gateway(monkeypatch, ports=[False], process=False)
    monkeypatch.setattr(gateway, "launch", lambda **kw: pytest.fail("must not launch"))
    result = run(isolated_settings, FakeX(), lambda a: "123456", no_waiting, launch=False)
    assert not result.ok and result.reason == login.REASON_NO_PROCESS
    assert "gateway up" in result.detail


def test_a_code_in_hand_is_not_spent_on_a_dying_dialog(
    isolated_settings, monkeypatch, no_waiting
):
    """The defect that cost two codes, now a refusal instead of a silent loss.

    With `wait_for_fresh=False` the caller already holds a code, so waiting for
    the next dialog is pointless - it would expire first. Say so at once.
    """
    stub_gateway(monkeypatch, ports=[False])
    fake = FakeX(dialogs=["900"])
    result = run(
        isolated_settings, fake, lambda a: "123456", no_waiting,
        wait_for_fresh=False, remaining=lambda: 8.0,
    )
    assert not result.ok and result.reason == login.REASON_DIALOG_STALE
    assert fake.typed == [], "the code must survive for the next prompt"
    assert "send a new code" in result.detail


def test_a_stale_dialog_is_skipped_until_a_fresh_one_opens(
    isolated_settings, monkeypatch, no_waiting
):
    """Interactively there is no code yet, so waiting for a fresh box is right."""
    stub_gateway(monkeypatch, ports=[False, False, False, False, True])
    fake = FakeX(dialogs=["900"])
    lifetimes = iter([5.0, 5.0, 170.0, 170.0, 170.0, 170.0])
    result = run(
        isolated_settings, fake, lambda a: "123456", no_waiting,
        remaining=lambda: next(lifetimes, 170.0),
    )
    assert result.ok, "must eventually type once the dialog is fresh"
    assert fake.typed == ["123456"]


def test_a_dialog_that_closes_mid_submission_reports_a_lost_race(
    isolated_settings, monkeypatch, no_waiting
):
    stub_gateway(monkeypatch, ports=[False])
    fake = FakeX(dialogs=["901"], dead={"901"})
    result = run(isolated_settings, fake, lambda a: "123456", no_waiting)
    assert not result.ok and result.reason == login.REASON_LOST_RACE
    assert "never saw it" in result.detail
    assert result.attempts == 0, "an unsent code must not count as an attempt"


def test_a_failing_launch_script_is_reported_not_waited_out(
    isolated_settings, monkeypatch, no_waiting
):
    stub_gateway(monkeypatch, ports=[False], process=False, launch_rc=1)
    result = run(isolated_settings, FakeX(), lambda a: "123456", no_waiting)
    assert not result.ok and result.reason == login.REASON_NO_PROCESS


def test_missing_xdotool_fails_before_launching_anything(
    isolated_settings, monkeypatch, no_waiting
):
    stub_gateway(monkeypatch, ports=[False], process=False)
    monkeypatch.setattr(gateway, "launch", lambda **kw: pytest.fail("must not launch"))

    def broken(cmd, display, timeout=15.0):
        raise login.LoginError("xdotool is not installed")

    with pytest.raises(login.LoginError):
        run(isolated_settings, broken, lambda a: "123456", no_waiting)


# --- the CLI surface -------------------------------------------------------


def cli_run(capsys, argv):
    rc = cli.main(argv)
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


@pytest.fixture
def stub_login(monkeypatch):
    """Replace the state machine; these tests are about the adapter."""
    recorded: dict[str, object] = {}

    def fake(settings, *, code_provider, launch=True, attempts=3, dialog_timeout=120.0,
             wait_for_fresh=True):
        recorded.update(
            launch=launch,
            attempts=attempts,
            dialog_timeout=dialog_timeout,
            wait_for_fresh=wait_for_fresh,
        )
        recorded["code"] = code_provider(1)
        return login.LoginResult(
            ok=recorded["code"] is not None,
            reason=login.REASON_LOGGED_IN if recorded["code"] else login.REASON_NEEDS_CODE,
            attempts=1 if recorded["code"] else 0,
            detail="test",
        )

    monkeypatch.setattr(api, "gateway_login", fake)
    # Never read the real IBC log from a test.
    monkeypatch.setattr(login, "dialog_remaining", lambda *a, **kw: None)
    monkeypatch.setattr(
        gateway,
        "status",
        lambda settings: gateway.GatewayStatus(
            host="127.0.0.1", port=4001, listening=True, process_running=True
        ),
    )
    return recorded


def test_gateway_up_passes_a_flag_code_through(capsys, stub_login):
    rc, out, _ = cli_run(capsys, ["gateway", "up", "--code", "123456", "--json"])
    assert rc == EXIT_OK
    assert stub_login["code"] == "123456"
    assert stub_login["launch"] is True
    payload = json.loads(out)
    assert payload["action"] == "up" and payload["ok"] is True
    assert payload["schema"] == 1, "the login payload is part of the contract too"


def test_gateway_code_takes_a_positional_and_does_not_launch(capsys, stub_login):
    rc, _, _ = cli_run(capsys, ["gateway", "code", "654321", "--json"])
    assert rc == EXIT_OK
    assert stub_login["code"] == "654321"
    assert stub_login["launch"] is False
    assert stub_login["wait_for_fresh"] is False, "a supplied code cannot outlive a wait"


def test_interactive_up_waits_for_a_fresh_dialog(capsys, stub_login, monkeypatch):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli, "prompt_for_code", lambda attempt, total: "111222")
    rc, _, _ = cli_run(capsys, ["gateway", "up", "--json"])
    assert rc == EXIT_OK
    assert stub_login["wait_for_fresh"] is True
    assert stub_login["code"] == "111222"


@pytest.mark.parametrize(
    "reason", [login.REASON_NEEDS_CODE, login.REASON_DIALOG_STALE, login.REASON_LOST_RACE]
)
def test_every_send_me_a_code_outcome_exits_five(capsys, monkeypatch, reason):
    """A caller must not have to read prose to know a human is needed."""
    monkeypatch.setattr(
        api, "gateway_login", lambda settings, **kw: login.LoginResult(False, reason, 0, "d")
    )
    monkeypatch.setattr(login, "dialog_remaining", lambda *a, **kw: None)
    monkeypatch.setattr(
        gateway,
        "status",
        lambda settings: gateway.GatewayStatus(
            host="127.0.0.1", port=4001, listening=False, process_running=True
        ),
    )
    rc, _, _ = cli_run(capsys, ["gateway", "code", "123456", "--json"])
    assert rc == EXIT_NEEDS_2FA


def test_gateway_status_reports_the_dialog_clock(capsys, monkeypatch):
    """Before asking a human for a code, know whether it can still be used."""
    monkeypatch.setattr(login, "dialog_remaining", lambda *a, **kw: 42.4242)
    monkeypatch.setattr(
        gateway,
        "status",
        lambda settings: gateway.GatewayStatus(
            host="127.0.0.1", port=4001, listening=False, process_running=True
        ),
    )
    rc, out, _ = cli_run(capsys, ["gateway", "status", "--json"])
    assert rc == EXIT_GATEWAY
    assert json.loads(out)["dialog_expires_in"] == 42.4


def test_no_prompt_yields_the_2fa_exit_code(capsys, stub_login, monkeypatch):
    """Cron needs to learn "a human is required" without blocking on stdin."""
    rc, out, _ = cli_run(capsys, ["gateway", "up", "--no-prompt", "--json"])
    assert rc == EXIT_NEEDS_2FA
    assert json.loads(out)["reason"] == login.REASON_NEEDS_CODE


def test_a_malformed_code_is_a_usage_error(capsys, stub_login):
    rc, _, err = cli_run(capsys, ["gateway", "up", "--code", "nope", "--json"])
    assert rc == EXIT_USAGE
    assert "authenticator code" in err


def test_a_failed_login_exits_with_the_gateway_code(capsys, monkeypatch):
    monkeypatch.setattr(
        api,
        "gateway_login",
        lambda settings, **kw: login.LoginResult(False, login.REASON_TIMEOUT, 1, "timed out"),
    )
    monkeypatch.setattr(
        gateway,
        "status",
        lambda settings: gateway.GatewayStatus(
            host="127.0.0.1", port=4001, listening=False, process_running=True
        ),
    )
    rc, out, _ = cli_run(capsys, ["gateway", "up", "--code", "123456", "--json"])
    assert rc == EXIT_GATEWAY
    assert json.loads(out)["ok"] is False
