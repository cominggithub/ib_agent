"""Tests for the published interface contract.

Other projects parse this CLI, so these tests guard promises rather than
internals: every `--json` payload is a JSON object carrying `schema`, stdout
stays free of anything but that object, and each failure reason maps to its own
exit code so a caller can tell "IB is down, retry later" from "nothing has
synced yet, retrying will not help".

Everything here runs offline. No test may need a Gateway.
"""

from __future__ import annotations

import datetime as dt
import json

import pytest

from ib_agent import cli, gateway, portfolio, store
from ib_agent.contract import (
    EXIT_ERROR,
    EXIT_GATEWAY,
    EXIT_NEEDS_2FA,
    EXIT_NO_DATA,
    EXIT_OK,
    EXIT_USAGE,
    SCHEMA_VERSION,
)
from ib_agent.gateway import GatewayStatus
from ib_agent.portfolio import AccountValue, PositionRow, Snapshot


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Point settings at a throwaway database and never start a Gateway.

    Both IB_DATA_DIR and IB_DB_PATH are set: .env may define either, and a test
    that silently read the real portfolio database would be worse than useless.
    """
    monkeypatch.setenv("IB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("IB_DB_PATH", str(tmp_path / "test.sqlite3"))
    monkeypatch.setenv("IB_AUTO_START_GATEWAY", "false")
    monkeypatch.setenv("IB_ACCOUNT", "")
    return tmp_path


def fake_status(*, listening: bool) -> GatewayStatus:
    return GatewayStatus(
        host="127.0.0.1", port=4001, listening=listening, process_running=listening
    )


@pytest.fixture
def gateway_down(monkeypatch):
    monkeypatch.setattr(gateway, "status", lambda settings: fake_status(listening=False))
    monkeypatch.setattr(
        gateway, "ensure_running", lambda settings, **kw: fake_status(listening=False)
    )
    # cli imported the module, so patching the module attribute is enough; guard
    # against a future direct import by failing loudly if one appears.
    monkeypatch.setattr(
        portfolio, "fetch_snapshot", lambda *a, **kw: pytest.fail("must not reach IB")
    )


@pytest.fixture
def gateway_up(monkeypatch):
    monkeypatch.setattr(gateway, "status", lambda settings: fake_status(listening=True))


def snapshot_fixture() -> Snapshot:
    return Snapshot(
        taken_at=dt.datetime(2026, 8, 11, 3, 30, tzinfo=dt.UTC),
        accounts=["U1234567"],
        positions=[
            PositionRow(
                account="U1234567",
                con_id=265598,
                symbol="GDX   260918P00045000",
                sec_type="OPT",
                exchange="SMART",
                currency="USD",
                quantity=-2,
                avg_cost=310.0,
                market_price=1.55,
                market_value=-310.0,
                unrealized_pnl=45.0,
                realized_pnl=0.0,
                underlying="GDX",
                expiry="2026-09-18",
                strike=45.0,
                right="P",
                multiplier=100,
                asset_class="ETF",
            )
        ],
        values=[AccountValue(tag="NetLiquidation", value="123456.78", currency="USD", account="U1234567")],
    )


@pytest.fixture
def stored_snapshot(isolated_env):
    settings_db = isolated_env / "test.sqlite3"
    conn = store.connect(settings_db)
    store.save(conn, snapshot_fixture())
    conn.close()
    return settings_db


def run(capsys, argv: list[str]) -> tuple[int, str, str]:
    rc = cli.main(argv)
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


# --- payload shape ---------------------------------------------------------


OFFLINE_JSON_COMMANDS = [
    ["status", "--json"],
    ["history", "--json"],
    ["instruments", "--json"],
    ["watchlist", "list", "--json"],
    ["gateway", "status", "--json"],
]


@pytest.mark.parametrize("argv", OFFLINE_JSON_COMMANDS, ids=lambda a: " ".join(a))
def test_json_output_is_an_object_stamped_with_schema(capsys, gateway_down, argv):
    _, out, _ = run(capsys, argv)
    payload = json.loads(out)  # raises if stdout carries anything but JSON
    assert isinstance(payload, dict), "payloads must be objects; an array cannot carry schema"
    assert payload["schema"] == SCHEMA_VERSION


def test_stored_positions_payload_has_the_documented_keys(capsys, stored_snapshot, gateway_down):
    rc, out, _ = run(capsys, ["positions", "--stored", "--json"])
    assert rc == EXIT_OK
    payload = json.loads(out)
    for key in ("schema", "source", "as_of", "accounts", "filters", "totals", "positions"):
        assert key in payload, f"missing contract key: {key}"
    assert payload["source"] == "snapshot"
    position = payload["positions"][0]
    assert position["quantity"] == -2, "short positions stay negative"
    assert position["asset_class"] == "ETF"


def test_grouped_payload_carries_key_and_totals(capsys, stored_snapshot, gateway_down):
    rc, out, _ = run(capsys, ["positions", "--stored", "--group-by", "expiry", "--json"])
    assert rc == EXIT_OK
    group = json.loads(out)["groups"][0]
    assert set(group) >= {"key", "totals", "positions"}


def test_totals_only_omits_positions(capsys, stored_snapshot, gateway_down):
    _, out, _ = run(capsys, ["positions", "--stored", "--totals-only", "--json"])
    assert "positions" not in json.loads(out)


def test_diagnostics_never_land_on_stdout(capsys, gateway_down):
    """A consumer parses stdout only, so a failure must leave it empty."""
    rc, out, err = run(capsys, ["positions", "--stored", "--json"])
    assert rc == EXIT_NO_DATA
    assert out == ""
    assert "sync" in err


# --- exit codes ------------------------------------------------------------


def test_status_reports_gateway_down_with_its_own_code(capsys, gateway_down):
    rc, out, _ = run(capsys, ["status", "--json"])
    assert rc == EXIT_GATEWAY
    assert json.loads(out)["ready"] is False


def test_status_ok_when_port_listens(capsys, gateway_up):
    rc, _, _ = run(capsys, ["status", "--json"])
    assert rc == EXIT_OK


def test_live_read_with_no_gateway_is_distinguishable_from_no_data(capsys, gateway_down):
    live_rc, _, _ = run(capsys, ["positions", "--json"])
    stored_rc, _, _ = run(capsys, ["positions", "--stored", "--json"])
    assert live_rc == EXIT_GATEWAY
    assert stored_rc == EXIT_NO_DATA
    assert live_rc != stored_rc, "the two failures must not collapse to one code"


def test_show_without_a_snapshot_is_no_data(capsys, gateway_down):
    rc, out, _ = run(capsys, ["show", "--json"])
    assert rc == EXIT_NO_DATA
    assert out == ""


def test_every_failure_reason_has_its_own_exit_code():
    """A caller branches on these: down-retry, no-data-give-up, needs-a-human."""
    codes = [EXIT_OK, EXIT_ERROR, EXIT_USAGE, EXIT_GATEWAY, EXIT_NO_DATA, EXIT_NEEDS_2FA]
    assert len(set(codes)) == len(codes)
    # 2FA is its own case: the Gateway is starting fine, it just needs a code
    # off someone's phone, so a scheduler must not treat it as "IB is down".
    assert EXIT_NEEDS_2FA not in {EXIT_GATEWAY, EXIT_NO_DATA, EXIT_ERROR}


def test_empty_history_is_no_data_but_still_valid_json(capsys, gateway_down):
    rc, out, _ = run(capsys, ["history", "--json"])
    assert rc == EXIT_NO_DATA
    assert json.loads(out)["snapshots"] == []


def test_history_with_a_snapshot_succeeds(capsys, stored_snapshot, gateway_down):
    rc, out, _ = run(capsys, ["history", "--json"])
    assert rc == EXIT_OK
    assert json.loads(out)["count"] == 1


def test_quotes_without_symbols_is_no_data_and_never_dials_ib(capsys, gateway_down):
    rc, _, err = run(capsys, ["watchlist", "quotes", "--json"])
    assert rc == EXIT_NO_DATA
    assert "empty" in err


def test_bad_arguments_exit_two(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["positions", "--right", "put", "--group-by", "nonsense"])
    assert excinfo.value.code == EXIT_USAGE


def test_expiries_and_underlyings_share_the_failure_mapping(capsys, gateway_down):
    for command in ("expiries", "underlyings"):
        rc, out, _ = run(capsys, [command, "--stored", "--json"])
        assert rc == EXIT_NO_DATA, command
        assert out == "", command


# --- guarantees other projects are told to rely on -------------------------


def test_account_summary_covers_the_margin_tags_consumers_need():
    """option_harvester's balances need margin headroom, not just net liq."""
    for tag in ("FullInitMarginReq", "FullMaintMarginReq", "RegTEquity", "BuyingPower"):
        assert tag in portfolio.SUMMARY_TAGS


def test_package_contains_no_order_placing_code():
    """Layer 4 of the safety model, enforced rather than asserted in prose.

    If an order layer is ever added it must arrive with its own deliberate,
    documented opt-in - and this test must be updated in the same change.
    """
    import pathlib

    package = pathlib.Path(portfolio.__file__).parent
    offenders: list[str] = []
    for path in sorted(package.rglob("*.py")):
        text = path.read_text()
        for forbidden in ("placeOrder", "whatIfOrder", "cancelOrder", "reqGlobalCancel"):
            if forbidden in text:
                offenders.append(f"{path.name}: {forbidden}")
    assert not offenders, f"order-placing calls found in a read-only package: {offenders}"
