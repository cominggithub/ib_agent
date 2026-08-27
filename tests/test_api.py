"""Tests for the programmatic interface.

The point of `api.py` is that another adapter — a server, a scheduled job,
another Python program — can use it without argparse, without printing and
without exit codes. These tests call it that way, so a regression that
re-entangles the two shows up here rather than in a future integration.
"""

from __future__ import annotations

import datetime as dt

import pytest

from ib_agent import api, gateway, store
from ib_agent.config import load_settings
from ib_agent.contract import NoData
from ib_agent.portfolio import AccountValue, GatewayUnavailable, PositionRow, Snapshot
from ib_agent.query import PositionFilter
from ib_agent.gateway import GatewayStatus


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("IB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("IB_DB_PATH", str(tmp_path / "test.sqlite3"))
    monkeypatch.setenv("IB_AUTO_START_GATEWAY", "false")
    monkeypatch.setattr(
        gateway,
        "status",
        lambda settings: GatewayStatus(
            host="127.0.0.1", port=4001, listening=False, process_running=False
        ),
    )
    return load_settings()


def two_positions() -> list[PositionRow]:
    return [
        PositionRow(
            account="U1",
            con_id=1,
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
        ),
        PositionRow(
            account="U1",
            con_id=2,
            symbol="TSM   261016C00300000",
            sec_type="OPT",
            exchange="SMART",
            currency="USD",
            quantity=-1,
            avg_cost=500.0,
            market_price=3.10,
            market_value=-310.0,
            unrealized_pnl=190.0,
            realized_pnl=0.0,
            underlying="TSM",
            expiry="2026-10-16",
            strike=300.0,
            right="C",
            multiplier=100,
            asset_class="ADR",
        ),
    ]


def store_snapshot(settings) -> None:
    conn = store.connect(settings.db_path)
    store.save(
        conn,
        Snapshot(
            taken_at=dt.datetime(2026, 8, 11, 3, 30, tzinfo=dt.UTC),
            accounts=["U1"],
            positions=two_positions(),
            values=[AccountValue(tag="NetLiquidation", value="1000", currency="USD", account="U1")],
        ),
    )
    conn.close()


def test_stored_raises_no_data_before_any_sync(isolated_settings):
    with pytest.raises(NoData):
        api.stored(isolated_settings)


def test_live_read_raises_gateway_unavailable_when_port_is_shut(isolated_settings):
    with pytest.raises(GatewayUnavailable):
        api.positions(isolated_settings, use_stored=False)


def test_stored_round_trip_reports_provenance(isolated_settings):
    store_snapshot(isolated_settings)
    result = api.positions(isolated_settings, use_stored=True)
    assert result.source == "snapshot"
    assert result.meta["accounts"] == ["U1"]
    assert len(result.rows) == 2


def test_select_filters_sorts_and_limits(isolated_settings):
    rows = two_positions()
    puts = api.select(rows, PositionFilter(rights=["P"]))
    assert [r.underlying for r in puts] == ["GDX"]

    # `--sort pnl` leads with the best unrealized P&L; `--reverse` is what the
    # docs call "worst first", which is the direction a risk review wants.
    best_first = api.select(rows, PositionFilter(), sort="pnl")
    assert [r.unrealized_pnl for r in best_first] == [190.0, 45.0]
    worst_first = api.select(rows, PositionFilter(), sort="pnl", reverse=True)
    assert [r.unrealized_pnl for r in worst_first] == [45.0, 190.0]

    assert len(api.select(rows, PositionFilter(), limit=1)) == 1


def test_payload_builders_need_no_cli(isolated_settings):
    store_snapshot(isolated_settings)
    result = api.positions(isolated_settings, use_stored=True)
    flt = PositionFilter()
    selected = api.select(result.rows, flt)

    flat = api.positions_payload(result, selected, flt)
    assert flat["count"] == 2
    assert len(flat["positions"]) == 2
    assert "groups" not in flat

    grouped = api.positions_payload(result, selected, flt, group_by="underlying")
    assert {g["key"] for g in grouped["groups"]} == {"GDX", "TSM"}

    summary = api.summary_payload(result, selected, flt, group_by="expiry")
    assert [g["key"] for g in summary["groups"]] == ["2026-09-18", "2026-10-16"]

    assert len(api.snapshot_payload(result)["positions"]) == 2


def test_totals_only_payload_drops_rows_at_both_levels(isolated_settings):
    store_snapshot(isolated_settings)
    result = api.positions(isolated_settings, use_stored=True)
    flt = PositionFilter()
    selected = api.select(result.rows, flt)

    assert "positions" not in api.positions_payload(result, selected, flt, totals_only=True)
    grouped = api.positions_payload(
        result, selected, flt, group_by="right", totals_only=True
    )
    assert all("positions" not in g for g in grouped["groups"])


def test_quotes_refuses_an_empty_request_without_dialling_ib(isolated_settings):
    """An empty list is a caller mistake, so it must fail before the socket."""
    with pytest.raises(NoData):
        api.quotes(isolated_settings, [])


def test_api_stays_free_of_cli_concerns():
    """A second adapter must not inherit argument parsing, printing or exits.

    Checked against the parsed module rather than its text, so a docstring
    mentioning argparse does not trip it.
    """
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path(api.__file__).read_text())

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "argparse" not in imported

    calls = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "print" not in calls, "api.py must return data, not print it"

    attribute_calls = {
        f"{node.func.value.id}.{node.func.attr}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
    }
    assert "sys.exit" not in attribute_calls
