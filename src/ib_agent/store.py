"""SQLite persistence for portfolio snapshots, instrument reference data and
the watchlist.

One row per snapshot plus child rows for positions and account values, so the
history can be diffed over time (what changed since yesterday, cost basis
drift, etc.) without re-querying IBKR.
"""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
from pathlib import Path

from .portfolio import Instrument, PositionRow, Snapshot

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    taken_at    TEXT NOT NULL,
    accounts    TEXT NOT NULL,
    source      TEXT NOT NULL,
    net_liq     REAL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_taken_at ON snapshots(taken_at);
CREATE TABLE IF NOT EXISTS positions (
    snapshot_id     INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    account         TEXT NOT NULL,
    con_id          INTEGER NOT NULL,
    symbol          TEXT NOT NULL,
    sec_type        TEXT NOT NULL,
    exchange        TEXT,
    currency        TEXT,
    quantity        REAL NOT NULL,
    avg_cost        REAL NOT NULL,
    market_price    REAL,
    market_value    REAL,
    unrealized_pnl  REAL,
    realized_pnl    REAL,
    underlying      TEXT NOT NULL DEFAULT '',
    expiry          TEXT NOT NULL DEFAULT '',
    strike          REAL,
    right           TEXT NOT NULL DEFAULT '',
    multiplier      REAL,
    asset_class     TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (snapshot_id, account, con_id)
);

CREATE TABLE IF NOT EXISTS account_values (
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    account     TEXT NOT NULL DEFAULT '',
    tag         TEXT NOT NULL,
    value       TEXT NOT NULL,
    currency    TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, account, tag, currency)
);

-- Static reference data, cached so repeat syncs need no extra API calls.
CREATE TABLE IF NOT EXISTS instruments (
    symbol      TEXT NOT NULL,
    currency    TEXT NOT NULL DEFAULT 'USD',
    asset_class TEXT NOT NULL DEFAULT '',
    long_name   TEXT NOT NULL DEFAULT '',
    industry    TEXT NOT NULL DEFAULT '',
    category    TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (symbol, currency)
);

CREATE TABLE IF NOT EXISTS watchlist (
    symbol    TEXT NOT NULL,
    sec_type  TEXT NOT NULL DEFAULT 'STK',
    exchange  TEXT NOT NULL DEFAULT 'SMART',
    currency  TEXT NOT NULL DEFAULT 'USD',
    note      TEXT NOT NULL DEFAULT '',
    added_at  TEXT NOT NULL,
    PRIMARY KEY (symbol, sec_type, currency)
);
"""

# Indexes are created after migrations, since they can reference added columns.
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_positions_underlying ON positions(underlying);
CREATE INDEX IF NOT EXISTS idx_positions_expiry ON positions(expiry);
"""

# Columns added after the first release; applied to existing databases.
MIGRATIONS: dict[str, list[tuple[str, str]]] = {
    "positions": [
        ("underlying", "TEXT NOT NULL DEFAULT ''"),
        ("expiry", "TEXT NOT NULL DEFAULT ''"),
        ("strike", "REAL"),
        ("right", "TEXT NOT NULL DEFAULT ''"),
        ("multiplier", "REAL"),
        ("asset_class", "TEXT NOT NULL DEFAULT ''"),
    ],
    "account_values": [
        ("account", "TEXT NOT NULL DEFAULT ''"),
    ],
}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, columns in MIGRATIONS.items():
        existing = {
            row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if not existing:  # table not created yet
            continue
        for name, ddl in columns:
            if name not in existing:
                conn.execute(f'ALTER TABLE {table} ADD COLUMN "{name}" {ddl}')
    conn.commit()


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.executescript(INDEXES)
    return conn


POSITION_COLUMNS = (
    "snapshot_id",
    "account",
    "con_id",
    "symbol",
    "sec_type",
    "exchange",
    "currency",
    "quantity",
    "avg_cost",
    "market_price",
    "market_value",
    "unrealized_pnl",
    "realized_pnl",
    "underlying",
    "expiry",
    "strike",
    "right",
    "multiplier",
    "asset_class",
)


def save(conn: sqlite3.Connection, snapshot: Snapshot) -> int:
    """Insert a snapshot and its children; returns the snapshot id."""
    with conn:  # single transaction
        cur = conn.execute(
            "INSERT INTO snapshots (taken_at, accounts, source, net_liq) VALUES (?, ?, ?, ?)",
            (
                snapshot.taken_at.isoformat(),
                json.dumps(snapshot.accounts),
                snapshot.source,
                snapshot.net_liquidation,
            ),
        )
        snapshot_id = int(cur.lastrowid or 0)

        placeholders = ", ".join("?" * len(POSITION_COLUMNS))
        quoted = ", ".join(f'"{c}"' for c in POSITION_COLUMNS)
        conn.executemany(
            f"INSERT OR REPLACE INTO positions ({quoted}) VALUES ({placeholders})",
            [
                (
                    snapshot_id,
                    p.account,
                    p.con_id,
                    p.symbol,
                    p.sec_type,
                    p.exchange,
                    p.currency,
                    p.quantity,
                    p.avg_cost,
                    p.market_price,
                    p.market_value,
                    p.unrealized_pnl,
                    p.realized_pnl,
                    p.underlying,
                    p.expiry,
                    p.strike,
                    p.right,
                    p.multiplier,
                    p.asset_class,
                )
                for p in snapshot.positions
            ],
        )

        conn.executemany(
            """INSERT OR REPLACE INTO account_values
                   (snapshot_id, account, tag, value, currency)
               VALUES (?, ?, ?, ?, ?)""",
            [(snapshot_id, v.account, v.tag, v.value, v.currency) for v in snapshot.values],
        )

        save_instruments(conn, snapshot.instruments, commit=False)
    return snapshot_id


def save_instruments(
    conn: sqlite3.Connection, instruments: list[Instrument], commit: bool = True
) -> int:
    now = dt.datetime.now(dt.UTC).isoformat()
    conn.executemany(
        """INSERT OR REPLACE INTO instruments
               (symbol, currency, asset_class, long_name, industry, category, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        [
            (i.symbol, i.currency or "USD", i.asset_class, i.long_name, i.industry, i.category, now)
            for i in instruments
        ],
    )
    if commit:
        conn.commit()
    return len(instruments)


def instrument_cache(conn: sqlite3.Connection) -> dict[str, str]:
    """symbol -> asset_class for everything classified so far."""
    return {
        row["symbol"]: row["asset_class"]
        for row in conn.execute("SELECT symbol, asset_class FROM instruments")
    }


def instruments(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM instruments ORDER BY symbol"
    ).fetchall()


def write_json(snapshot: Snapshot, data_dir: Path) -> Path:
    """Also dump a human-readable JSON copy, handy for diffing by eye."""
    out_dir = data_dir / "snapshots"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = snapshot.taken_at.strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"portfolio-{stamp}.json"
    payload = {
        "taken_at": snapshot.taken_at.isoformat(),
        "accounts": snapshot.accounts,
        "source": snapshot.source,
        "net_liquidation": snapshot.net_liquidation,
        "positions": [vars(p) for p in snapshot.positions],
        "account_values": [vars(v) for v in snapshot.values],
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    return path


def latest(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM snapshots ORDER BY id DESC LIMIT 1"
    ).fetchone()


def positions_for(conn: sqlite3.Connection, snapshot_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM positions WHERE snapshot_id = ? ORDER BY account, symbol",
        (snapshot_id,),
    ).fetchall()


def position_rows_for(conn: sqlite3.Connection, snapshot_id: int) -> list[PositionRow]:
    """Stored positions rehydrated into PositionRow objects."""
    out: list[PositionRow] = []
    for r in positions_for(conn, snapshot_id):
        keys = r.keys()
        out.append(
            PositionRow(
                account=r["account"],
                con_id=r["con_id"],
                symbol=r["symbol"],
                sec_type=r["sec_type"],
                exchange=r["exchange"] or "",
                currency=r["currency"] or "",
                quantity=r["quantity"],
                avg_cost=r["avg_cost"],
                market_price=r["market_price"],
                market_value=r["market_value"],
                unrealized_pnl=r["unrealized_pnl"],
                realized_pnl=r["realized_pnl"],
                underlying=(r["underlying"] if "underlying" in keys else "") or "",
                expiry=(r["expiry"] if "expiry" in keys else "") or "",
                strike=r["strike"] if "strike" in keys else None,
                right=(r["right"] if "right" in keys else "") or "",
                multiplier=r["multiplier"] if "multiplier" in keys else None,
                asset_class=(r["asset_class"] if "asset_class" in keys else "") or "",
            )
        )
    return out


def account_values_for(conn: sqlite3.Connection, snapshot_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM account_values WHERE snapshot_id = ? ORDER BY account, tag, currency",
        (snapshot_id,),
    ).fetchall()


def history(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, taken_at, net_liq, source FROM snapshots ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()


# --- watchlist -------------------------------------------------------------


def watchlist_add(
    conn: sqlite3.Connection,
    symbol: str,
    sec_type: str = "STK",
    exchange: str = "SMART",
    currency: str = "USD",
    note: str = "",
) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO watchlist
               (symbol, sec_type, exchange, currency, note, added_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            symbol.upper(),
            sec_type.upper(),
            exchange.upper(),
            currency.upper(),
            note,
            dt.datetime.now(dt.UTC).isoformat(),
        ),
    )
    conn.commit()


def watchlist_remove(conn: sqlite3.Connection, symbol: str) -> int:
    cur = conn.execute("DELETE FROM watchlist WHERE symbol = ?", (symbol.upper(),))
    conn.commit()
    return cur.rowcount


def watchlist_all(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM watchlist ORDER BY symbol").fetchall()
