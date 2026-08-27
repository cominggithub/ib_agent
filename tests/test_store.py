"""Tests that exercise storage/formatting without touching IBKR."""

from __future__ import annotations

import datetime as dt

from ib_agent import store
from ib_agent.cli import print_snapshot
from ib_agent.portfolio import AccountValue, PositionRow, Snapshot


def make_snapshot() -> Snapshot:
    return Snapshot(
        taken_at=dt.datetime(2026, 8, 4, 7, 30, tzinfo=dt.UTC),
        accounts=["U1234567"],
        positions=[
            PositionRow(
                account="U1234567",
                con_id=265598,
                symbol="AAPL",
                sec_type="STK",
                exchange="NASDAQ",
                currency="USD",
                quantity=100,
                avg_cost=150.0,
                market_price=210.5,
                market_value=21050.0,
                unrealized_pnl=6050.0,
                realized_pnl=0.0,
            ),
            PositionRow(
                account="U1234567",
                con_id=76792991,
                symbol="TSM",
                sec_type="STK",
                exchange="NYSE",
                currency="USD",
                quantity=50,
                avg_cost=90.0,
            ),
        ],
        values=[
            AccountValue(tag="NetLiquidation", value="45000.75", currency="USD"),
            AccountValue(tag="TotalCashValue", value="12000.00", currency="USD"),
        ],
    )


def test_cost_basis() -> None:
    snap = make_snapshot()
    assert snap.positions[0].cost_basis == 15000.0


def test_net_liquidation_parsed() -> None:
    assert make_snapshot().net_liquidation == 45000.75


def test_missing_tag_returns_none() -> None:
    assert make_snapshot().value_of("BuyingPower") is None


def test_save_and_read_back(tmp_path) -> None:
    db = tmp_path / "portfolio.sqlite3"
    conn = store.connect(db)
    snapshot_id = store.save(conn, make_snapshot())

    latest = store.latest(conn)
    assert latest is not None
    assert latest["id"] == snapshot_id
    assert latest["net_liq"] == 45000.75

    rows = store.positions_for(conn, snapshot_id)
    assert [r["symbol"] for r in rows] == ["AAPL", "TSM"]
    # position without market data keeps NULLs rather than fabricating zeros
    assert rows[1]["market_value"] is None
    assert len(store.history(conn)) == 1
    conn.close()


def test_two_snapshots_are_independent(tmp_path) -> None:
    conn = store.connect(tmp_path / "p.sqlite3")
    first = store.save(conn, make_snapshot())
    second = store.save(conn, make_snapshot())
    assert first != second
    assert len(store.history(conn)) == 2
    conn.close()


def test_json_dump(tmp_path) -> None:
    path = store.write_json(make_snapshot(), tmp_path)
    assert path.exists()
    assert "AAPL" in path.read_text()


def test_print_snapshot_handles_missing_values(capsys) -> None:
    snap = make_snapshot()
    print_snapshot(snap)
    out = capsys.readouterr().out
    assert "AAPL" in out and "TSM" in out
    assert "NetLiquidation" in out


def test_print_snapshot_empty_portfolio(capsys) -> None:
    snap = Snapshot(taken_at=dt.datetime.now(dt.UTC), accounts=[])
    print_snapshot(snap)
    assert "no open positions" in capsys.readouterr().out


def test_duplicate_account_value_rows_do_not_break_save(tmp_path) -> None:
    """IBKR really does repeat tags such as AccruedCash/USD."""
    snap = make_snapshot()
    snap.values.extend(
        [
            AccountValue(tag="AccruedCash", value="100.00", currency="USD"),
            AccountValue(tag="AccruedCash", value="100.00", currency="USD"),
        ]
    )
    conn = store.connect(tmp_path / "p.sqlite3")
    snapshot_id = store.save(conn, snap)
    rows = conn.execute(
        "SELECT COUNT(*) c FROM account_values WHERE snapshot_id = ? AND tag = 'AccruedCash'",
        (snapshot_id,),
    ).fetchone()
    assert rows["c"] == 1
    conn.close()


def test_value_of_prefers_concrete_currency_over_base() -> None:
    snap = make_snapshot()
    snap.values.extend(
        [
            AccountValue(tag="UnrealizedPnL", value="1.00", currency="BASE"),
            AccountValue(tag="UnrealizedPnL", value="1000.00", currency="USD"),
        ]
    )
    assert snap.value_of("UnrealizedPnL") == 1000.00
    assert snap.value_of("UnrealizedPnL", currency="BASE") == 1.00


def test_multi_account_values_are_kept_separate(tmp_path) -> None:
    snap = make_snapshot()
    snap.values = [
        AccountValue(tag="NetLiquidation", value="100.0", currency="USD", account="U1"),
        AccountValue(tag="NetLiquidation", value="200.0", currency="USD", account="U2"),
    ]
    conn = store.connect(tmp_path / "p.sqlite3")
    snapshot_id = store.save(conn, snap)
    rows = conn.execute(
        "SELECT account, value FROM account_values WHERE snapshot_id = ? ORDER BY account",
        (snapshot_id,),
    ).fetchall()
    assert [(r["account"], r["value"]) for r in rows] == [("U1", "100.0"), ("U2", "200.0")]
    conn.close()
