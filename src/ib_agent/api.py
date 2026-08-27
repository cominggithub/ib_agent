"""Programmatic interface — everything the CLI does, minus the CLI.

Rules for this module, so that a second adapter (a socket server, a scheduled
job, another Python program) needs nothing from `cli.py`:

* no argparse, no printing, no `sys.exit`;
* failures raise (`GatewayUnavailable`, `NoData`) instead of returning codes,
  and the caller decides what a failure means;
* payload builders return plain dicts matching the documented JSON contract, so
  the shape is defined in one place rather than per adapter.

`cli.py` is then only: parse arguments, call this, render or dump, translate the
exception into an exit code.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import activity, gateway, login, market, query, store
from .activity import ExecutionRow, OrderRow
from .config import Settings
from .contract import NoData
from .market import ChainParams, GreekRow, Resolved
from .portfolio import GatewayUnavailable, PositionRow, Snapshot, fetch_snapshot
from .query import PositionFilter
from .watchlist import MARKET_DATA_TYPE_NAMES, Quote, WatchEntry, fetch_quotes


@dataclass(frozen=True)
class PositionSet:
    """Positions plus the provenance every payload has to report."""

    rows: list[PositionRow]
    meta: dict[str, Any]

    @property
    def source(self) -> str:
        return str(self.meta.get("source", ""))


@dataclass(frozen=True)
class SyncResult:
    snapshot: Snapshot
    snapshot_id: int
    json_path: Path


# --- gateway ---------------------------------------------------------------


def status_payload(settings: Settings) -> dict[str, Any]:
    st = gateway.status(settings)
    return {
        "host": settings.host,
        "port": settings.port,
        "trading_mode": settings.trading_mode,
        "listening": st.listening,
        "process_running": st.process_running,
        "readonly": settings.readonly,
        "market_data_type": MARKET_DATA_TYPE_NAMES.get(
            settings.market_data_type, settings.market_data_type
        ),
        "db": str(settings.db_path),
        "ready": st.ready,
    }


def require_gateway(settings: Settings) -> None:
    """Raise unless the API port is reachable, starting the Gateway if allowed."""
    st = (
        gateway.ensure_running(settings)
        if settings.auto_start_gateway
        else gateway.status(settings)
    )
    if not st.ready:
        raise GatewayUnavailable(
            f"gateway not reachable on {settings.host}:{settings.port}; "
            "start it with 'ib-agent gateway up', or pass --stored to use the last snapshot"
        )


def gateway_login(
    settings: Settings,
    *,
    code_provider: login.CodeProvider,
    launch: bool = True,
    attempts: int = 3,
    dialog_timeout: float = 120.0,
    wait_for_fresh: bool = True,
) -> login.LoginResult:
    """Start the Gateway and answer IBKR's 2FA prompt with supplied codes.

    The caller owns *how* a code is obtained - prompt, flag, message from a
    phone - by passing `code_provider`. Returning None from it means "no code
    available", which ends the attempt cleanly instead of blocking on input that
    will never arrive.
    """
    return login.run_login(
        settings,
        code_provider=code_provider,
        launch=launch,
        attempts=attempts,
        dialog_timeout=dialog_timeout,
        wait_for_fresh=wait_for_fresh,
    )


def login_payload(
    settings: Settings, result: login.LoginResult, action: str
) -> dict[str, Any]:
    st = gateway.status(settings)
    remaining = login.dialog_remaining()
    return {
        "action": action,
        "ok": result.ok,
        "reason": result.reason,
        "detail": result.detail,
        "codes_submitted": result.attempts,
        # Seconds before IBKR's current code dialog closes; None when no IBC log
        # is readable. A caller about to ask a human for a code should check this
        # first - below ~25s the code will not arrive in time to be used.
        "dialog_expires_in": None if remaining is None else round(remaining, 1),
        "host": settings.host,
        "port": settings.port,
        "trading_mode": settings.trading_mode,
        "listening": st.listening,
        "process_running": st.process_running,
        "ready": st.ready,
    }


# --- snapshots -------------------------------------------------------------


def sync(settings: Settings) -> SyncResult:
    """Fetch a snapshot, store it, and write the JSON copy."""
    require_gateway(settings)
    conn = store.connect(settings.db_path)
    try:
        snapshot = fetch_snapshot(settings, instrument_cache=store.instrument_cache(conn))
        snapshot_id = store.save(conn, snapshot)
    finally:
        conn.close()
    return SyncResult(
        snapshot=snapshot,
        snapshot_id=snapshot_id,
        json_path=store.write_json(snapshot, settings.data_dir),
    )


def _live(settings: Settings, save: bool) -> PositionSet:
    conn = store.connect(settings.db_path)
    try:
        snapshot = fetch_snapshot(settings, instrument_cache=store.instrument_cache(conn))
        meta: dict[str, Any] = {
            "source": "live",
            "as_of": snapshot.taken_at.isoformat(timespec="seconds"),
            "accounts": snapshot.accounts,
            "net_liquidation": snapshot.net_liquidation,
        }
        if save:
            meta["snapshot_id"] = store.save(conn, snapshot)
        else:
            # Reference data is static; caching it avoids repeat lookups later.
            store.save_instruments(conn, snapshot.instruments)
    finally:
        conn.close()
    return PositionSet(rows=snapshot.positions, meta=meta)


def stored(settings: Settings) -> PositionSet:
    """The most recent stored snapshot. Raises NoData if nothing is stored."""
    conn = store.connect(settings.db_path)
    try:
        row = store.latest(conn)
        if row is None:
            raise NoData("no snapshots stored yet; run `ib-agent sync` first")
        rows = store.position_rows_for(conn, int(row["id"]))
        meta = {
            "source": "snapshot",
            "snapshot_id": int(row["id"]),
            "as_of": row["taken_at"],
            "accounts": _json_list(row["accounts"]),
            "net_liquidation": row["net_liq"],
        }
    finally:
        conn.close()
    return PositionSet(rows=rows, meta=meta)


def _json_list(raw: str) -> list[str]:
    import json

    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return list(value) if isinstance(value, list) else []


def positions(settings: Settings, *, use_stored: bool = False, save: bool = False) -> PositionSet:
    """Fresh from the Gateway by default, or the last stored snapshot.

    Raises GatewayUnavailable when a live read cannot happen and NoData when a
    stored read has nothing to read. The distinction matters to callers: the
    first may succeed later, the second will not until something syncs.
    """
    if use_stored:
        return stored(settings)
    require_gateway(settings)
    return _live(settings, save=save)


# --- selection and payloads ------------------------------------------------


def select(
    rows: Sequence[PositionRow],
    flt: PositionFilter,
    *,
    sort: str = "symbol",
    reverse: bool = False,
    limit: int | None = None,
    today: dt.date | None = None,
) -> list[PositionRow]:
    """Filter, sort and truncate, in the order the CLI documents."""
    selected = query.apply_filter(rows, flt, today or dt.date.today())
    selected = query.sort_rows(selected, sort)
    if reverse:
        selected.reverse()
    if limit:
        selected = selected[:limit]
    return selected


def positions_payload(
    result: PositionSet,
    selected: Sequence[PositionRow],
    flt: PositionFilter,
    *,
    sort: str = "symbol",
    group_by: str | None = None,
    totals_only: bool = False,
    today: dt.date | None = None,
) -> dict[str, Any]:
    day = today or dt.date.today()
    payload: dict[str, Any] = {
        **result.meta,
        "filters": flt.describe(),
        "sort": sort,
        "count": len(selected),
        "totals": query.summarize(selected).as_dict(),
    }
    if group_by:
        payload["group_by"] = group_by
        payload["groups"] = [
            {
                "key": key,
                "totals": query.summarize(members).as_dict(),
                **(
                    {}
                    if totals_only
                    else {"positions": [query.row_to_dict(r, day) for r in members]}
                ),
            }
            for key, members in query.group_rows(selected, group_by)
        ]
    elif not totals_only:
        payload["positions"] = [query.row_to_dict(r, day) for r in selected]
    return payload


def summary_payload(
    result: PositionSet,
    selected: Sequence[PositionRow],
    flt: PositionFilter,
    *,
    group_by: str,
) -> dict[str, Any]:
    """Payload for `expiries` / `underlyings`: one row per group, no positions."""
    return {
        **result.meta,
        "group_by": group_by,
        "filters": flt.describe(),
        "groups": [
            {"key": key, **query.summarize(members).as_dict()}
            for key, members in query.group_rows(selected, group_by)
        ],
        "totals": query.summarize(selected).as_dict(),
    }


def snapshot_payload(result: PositionSet) -> dict[str, Any]:
    return {**result.meta, "positions": [query.row_to_dict(r) for r in result.rows]}


def sync_payload(settings: Settings, result: SyncResult) -> dict[str, Any]:
    snapshot = result.snapshot
    return {
        "snapshot_id": result.snapshot_id,
        "as_of": snapshot.taken_at.isoformat(timespec="seconds"),
        "accounts": snapshot.accounts,
        "net_liquidation": snapshot.net_liquidation,
        "positions": len(snapshot.positions),
        "instruments_resolved": len(snapshot.instruments),
        "db": str(settings.db_path),
        "json_copy": str(result.json_path),
    }


# --- stored tables ---------------------------------------------------------


def history(settings: Settings, limit: int = 20) -> list[dict[str, Any]]:
    conn = store.connect(settings.db_path)
    try:
        return [dict(r) for r in store.history(conn, limit=limit)]
    finally:
        conn.close()


def instruments(settings: Settings) -> list[dict[str, Any]]:
    conn = store.connect(settings.db_path)
    try:
        return [dict(r) for r in store.instruments(conn)]
    finally:
        conn.close()


# --- watchlist -------------------------------------------------------------


def watchlist_entries(conn: sqlite3.Connection) -> list[WatchEntry]:
    return [
        WatchEntry(
            symbol=r["symbol"],
            sec_type=r["sec_type"],
            exchange=r["exchange"],
            currency=r["currency"],
            note=r["note"],
        )
        for r in store.watchlist_all(conn)
    ]


def watchlist(settings: Settings) -> list[WatchEntry]:
    conn = store.connect(settings.db_path)
    try:
        return watchlist_entries(conn)
    finally:
        conn.close()


def watchlist_add(
    settings: Settings,
    symbols: Iterable[str],
    *,
    sec_type: str = "STK",
    exchange: str = "SMART",
    currency: str = "USD",
    note: str = "",
) -> int:
    """Add symbols to the local watchlist; returns how many were written.

    This touches only this project's database. It cannot alter an IBKR account
    watchlist - the TWS API has no watchlist calls at all.
    """
    conn = store.connect(settings.db_path)
    try:
        count = 0
        for symbol in symbols:
            store.watchlist_add(
                conn, symbol, sec_type=sec_type, exchange=exchange, currency=currency, note=note
            )
            count += 1
        return count
    finally:
        conn.close()


def watchlist_remove(settings: Settings, symbols: Iterable[str]) -> int:
    """Remove symbols from the local watchlist; returns rows deleted."""
    conn = store.connect(settings.db_path)
    try:
        return sum(store.watchlist_remove(conn, s) for s in symbols)
    finally:
        conn.close()


def quotes(settings: Settings, entries: Sequence[WatchEntry]) -> list[Quote]:
    """Quote the given entries. Raises NoData for an empty list.

    An empty request is a caller mistake, not an IB failure, so it must not
    reach the Gateway.
    """
    if not entries:
        raise NoData("watchlist is empty; add symbols or pass them as arguments")
    require_gateway(settings)
    return fetch_quotes(settings, entries)


def quotes_payload(settings: Settings, values: Iterable[Quote]) -> dict[str, Any]:
    return {
        "as_of": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "market_data_type": MARKET_DATA_TYPE_NAMES.get(
            settings.market_data_type, settings.market_data_type
        ),
        "quotes": [q.as_dict() for q in values],
    }


# --- reference data: resolve / chain ---------------------------------------


def resolve(settings: Settings, specs: Sequence[str], *, currency: str = "USD") -> list[Resolved]:
    """Resolve contract specs to conids. Raises NoData for an empty request."""
    if not specs:
        raise NoData("nothing to resolve; pass symbols, conids or --from-positions")
    require_gateway(settings)
    return market.fetch_resolved(settings, specs, currency=currency)


def resolve_positions(
    settings: Settings, *, use_stored: bool = False, options_only: bool = False
) -> tuple[list[Resolved], dict[str, Any]]:
    """Resolve held positions, so each option reports its underlying conid.

    Returns the rows plus the provenance metadata of the position set they came
    from, because a consumer keying on conids needs to know how stale they are.
    """
    result = positions(settings, use_stored=use_stored)
    rows = market.option_rows(result.rows) if options_only else list(result.rows)
    if not rows:
        raise NoData("no positions to resolve")
    require_gateway(settings)
    return market.fetch_resolved_for_rows(settings, rows), dict(result.meta)


def resolve_payload(
    items: Sequence[Resolved], *, meta: dict[str, Any] | None = None
) -> dict[str, Any]:
    resolved = [r for r in items if not r.error]
    return {
        "as_of": market.now_iso(),
        **(meta or {}),
        "count": len(items),
        "resolved": len(resolved),
        "failed": len(items) - len(resolved),
        "contracts": [r.as_dict() for r in items],
    }


def chain(
    settings: Settings,
    symbol: str,
    *,
    sec_type: str = "STK",
    currency: str = "USD",
    exchange: str = "",
) -> list[ChainParams]:
    """Expirations and strikes for one underlying."""
    if not symbol:
        raise NoData("chain needs an underlying symbol")
    require_gateway(settings)
    return market.fetch_chain(
        settings, symbol, sec_type=sec_type, currency=currency, exchange=exchange
    )


def chain_payload(
    symbol: str,
    chains: Sequence[ChainParams],
    *,
    expiry_prefix: str = "",
    strike_min: float | None = None,
    strike_max: float | None = None,
) -> dict[str, Any]:
    """Chain payload, with the same prefix/range narrowing the CLI documents."""
    out: list[dict[str, Any]] = []
    for item in chains:
        data = item.as_dict()
        if expiry_prefix:
            data["expirations"] = [e for e in item.expirations if e.startswith(expiry_prefix)]
        if strike_min is not None or strike_max is not None:
            low = strike_min if strike_min is not None else float("-inf")
            high = strike_max if strike_max is not None else float("inf")
            data["strikes"] = [s for s in item.strikes if low <= s <= high]
        data["expiration_count"] = len(data["expirations"])
        data["strike_count"] = len(data["strikes"])
        out.append(data)
    return {
        "as_of": market.now_iso(),
        "underlying": symbol.upper(),
        "filters": {
            "expiry_prefix": expiry_prefix,
            "strike_min": strike_min,
            "strike_max": strike_max,
        },
        "count": len(out),
        "chains": out,
    }


# --- greeks ----------------------------------------------------------------


def greeks_for_positions(
    settings: Settings, *, use_stored: bool = False, flt: PositionFilter | None = None
) -> tuple[list[GreekRow], dict[str, Any]]:
    """Model greeks for every held option, in position order."""
    result = positions(settings, use_stored=use_stored)
    rows = market.option_rows(result.rows)
    if flt is not None:
        rows = query.apply_filter(rows, flt, dt.date.today())
        rows = market.option_rows(rows)
    if not rows:
        raise NoData("no option positions to price")
    require_gateway(settings)
    contracts = [market.contract_from_row(r) for r in rows]
    values = market.fetch_greeks(
        settings, contracts, quantities=[r.quantity for r in rows]
    )
    return values, dict(result.meta)


def greeks_for_specs(
    settings: Settings, specs: Sequence[str], *, currency: str = "USD"
) -> list[GreekRow]:
    """Model greeks for explicitly named option contracts."""
    if not specs:
        raise NoData("greeks needs contracts; pass specs or use --from-positions")
    require_gateway(settings)
    contracts = [market.parse_spec(s, currency) for s in specs]
    return market.fetch_greeks(settings, contracts)


def greeks_payload(
    settings: Settings, rows: Sequence[GreekRow], *, meta: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "as_of": market.now_iso(),
        **(meta or {}),
        "market_data_type": MARKET_DATA_TYPE_NAMES.get(
            settings.market_data_type, settings.market_data_type
        ),
        "count": len(rows),
        "totals": market.totals(rows),
        "greeks": [r.as_dict() for r in rows],
    }


# --- activity: orders / executions -----------------------------------------


def orders(settings: Settings, *, active_only: bool = True) -> list[OrderRow]:
    """Working orders, including ones placed from IBKR Mobile.

    Read-only: this asks IB what exists and can neither place nor cancel.
    """
    require_gateway(settings)
    return activity.fetch_open_orders(settings, active_only=active_only)


def orders_payload(settings: Settings, rows: Sequence[OrderRow]) -> dict[str, Any]:
    return {
        "as_of": market.now_iso(),
        "accounts": [settings.account] if settings.account else [],
        "count": len(rows),
        "totals": activity.order_totals(rows),
        # An empty list is ambiguous unless the caller knows about this setting.
        "master_client_id_hint": (
            "orders placed outside this client id appear only when "
            "OverrideTwsMasterClientID is set in ~/ibc/config.ini"
        ),
        "orders": [r.as_dict() for r in rows],
    }


def executions(
    settings: Settings,
    *,
    symbol: str = "",
    sec_type: str = "",
    side: str = "",
    since: dt.datetime | None = None,
) -> list[ExecutionRow]:
    """Today's fills. Older history needs the Flex Web Service."""
    require_gateway(settings)
    return activity.fetch_executions(
        settings, symbol=symbol, sec_type=sec_type, side=side, since=since
    )


def executions_payload(rows: Sequence[ExecutionRow], **filters: Any) -> dict[str, Any]:
    return {
        "as_of": market.now_iso(),
        "window": "today",  # what reqExecutions covers, stated rather than assumed
        "filters": {k: v for k, v in filters.items() if v},
        "count": len(rows),
        "totals": activity.execution_totals(rows),
        "executions": [r.as_dict() for r in rows],
    }
