"""Watchlist storage, quote formatting, and legacy-database migration."""

from __future__ import annotations

import sqlite3

from ib_agent import store
from ib_agent.watchlist import (
    Quote,
    WatchEntry,
    _clean,
    contract_for,
    entries_from_symbols,
    render_quotes,
)


# --- watchlist table -------------------------------------------------------


def test_watchlist_round_trip(tmp_path) -> None:
    conn = store.connect(tmp_path / "p.sqlite3")
    store.watchlist_add(conn, "spy", note="benchmark")
    store.watchlist_add(conn, "gdx")
    rows = store.watchlist_all(conn)
    assert [r["symbol"] for r in rows] == ["GDX", "SPY"]
    assert dict(rows[1])["note"] == "benchmark"
    assert rows[0]["sec_type"] == "STK" and rows[0]["exchange"] == "SMART"
    conn.close()


def test_watchlist_add_is_idempotent(tmp_path) -> None:
    conn = store.connect(tmp_path / "p.sqlite3")
    store.watchlist_add(conn, "SPY", note="first")
    store.watchlist_add(conn, "SPY", note="second")
    rows = store.watchlist_all(conn)
    assert len(rows) == 1
    assert rows[0]["note"] == "second"
    conn.close()


def test_watchlist_remove(tmp_path) -> None:
    conn = store.connect(tmp_path / "p.sqlite3")
    store.watchlist_add(conn, "SPY")
    assert store.watchlist_remove(conn, "spy") == 1
    assert store.watchlist_all(conn) == []
    assert store.watchlist_remove(conn, "NOPE") == 0
    conn.close()


# --- contracts and quotes --------------------------------------------------


def test_contract_for_types() -> None:
    assert contract_for(WatchEntry("spy")).secType == "STK"
    assert contract_for(WatchEntry("SPX", sec_type="IND")).secType == "IND"
    assert contract_for(WatchEntry("EURUSD", sec_type="CASH")).secType == "CASH"
    assert contract_for(WatchEntry("VIX", sec_type="WAR")).secType == "WAR"


def test_contract_symbols_are_upper_cased() -> None:
    assert contract_for(WatchEntry("gdx")).symbol == "GDX"


def test_entries_from_symbols() -> None:
    entries = entries_from_symbols(["spy", "gdx"])
    assert [e.symbol for e in entries] == ["SPY", "GDX"]
    assert all(e.sec_type == "STK" for e in entries)


def test_clean_handles_nan_and_none() -> None:
    assert _clean(float("nan")) is None
    assert _clean(None) is None
    assert _clean(3) == 3.0


def test_quote_change_math() -> None:
    q = Quote(symbol="SPY", sec_type="STK", currency="USD", market_price=759.23, close=757.67)
    assert round(q.change, 2) == 1.56
    assert round(q.change_pct, 3) == 0.206


def test_quote_change_none_without_close() -> None:
    q = Quote(symbol="SPY", sec_type="STK", currency="USD", market_price=759.23)
    assert q.change is None and q.change_pct is None


def test_quote_as_dict_renames_open() -> None:
    data = Quote(symbol="SPY", sec_type="STK", currency="USD", open_=750.0).as_dict()
    assert data["open"] == 750.0
    assert "open_" not in data


def test_render_quotes_includes_errors() -> None:
    quotes = [
        Quote(symbol="SPY", sec_type="STK", currency="USD", last=759.2, close=757.6),
        Quote(symbol="NOPE", sec_type="STK", currency="USD", error="contract not resolved"),
    ]
    text = render_quotes(quotes)
    assert "SPY" in text and "759.20" in text
    assert "contract not resolved" in text


# --- migration -------------------------------------------------------------

LEGACY_SCHEMA = """
CREATE TABLE snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    taken_at TEXT NOT NULL,
    accounts TEXT NOT NULL,
    source TEXT NOT NULL,
    net_liq REAL
);
CREATE TABLE positions (
    snapshot_id INTEGER NOT NULL,
    account TEXT NOT NULL,
    con_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    sec_type TEXT NOT NULL,
    exchange TEXT,
    currency TEXT,
    quantity REAL NOT NULL,
    avg_cost REAL NOT NULL,
    market_price REAL,
    market_value REAL,
    unrealized_pnl REAL,
    realized_pnl REAL,
    PRIMARY KEY (snapshot_id, account, con_id)
);
CREATE TABLE account_values (
    snapshot_id INTEGER NOT NULL,
    tag TEXT NOT NULL,
    value TEXT NOT NULL,
    currency TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, tag, currency)
);
"""


def test_legacy_database_is_migrated(tmp_path) -> None:
    """A database written before option metadata existed must keep working."""
    db = tmp_path / "old.sqlite3"
    raw = sqlite3.connect(db)
    raw.executescript(LEGACY_SCHEMA)
    raw.execute(
        "INSERT INTO snapshots (taken_at, accounts, source, net_liq) VALUES (?,?,?,?)",
        ("2026-08-04T08:00:00+00:00", '["U1"]', "tws-api", 1000.0),
    )
    raw.execute(
        """INSERT INTO positions (snapshot_id, account, con_id, symbol, sec_type,
                                  exchange, currency, quantity, avg_cost)
           VALUES (1,'U1',1,'AAPL','STK','NASDAQ','USD',10,150.0)""",
    )
    raw.commit()
    raw.close()

    conn = store.connect(db)
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(positions)")}
    assert {"underlying", "expiry", "strike", "right", "multiplier", "asset_class"} <= columns
    assert "account" in {r["name"] for r in conn.execute("PRAGMA table_info(account_values)")}

    rows = store.position_rows_for(conn, 1)
    assert len(rows) == 1
    assert rows[0].symbol == "AAPL"
    assert rows[0].expiry == "" and rows[0].strike is None
    conn.close()


def test_instrument_cache_round_trip(tmp_path) -> None:
    from ib_agent.portfolio import Instrument

    conn = store.connect(tmp_path / "p.sqlite3")
    store.save_instruments(
        conn,
        [
            Instrument(symbol="GDX", currency="USD", asset_class="ETF", long_name="VanEck Gold"),
            Instrument(symbol="AAPL", currency="USD", asset_class="COMMON"),
        ],
    )
    assert store.instrument_cache(conn) == {"GDX": "ETF", "AAPL": "COMMON"}
    assert [r["symbol"] for r in store.instruments(conn)] == ["AAPL", "GDX"]
    conn.close()
