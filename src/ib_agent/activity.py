"""Open orders and executions — read-only views of account activity.

Both are *reads*, which is the only reason they belong in this package while
`ReadOnlyApi=yes` holds: `reqAllOpenOrders` and `reqExecutions` ask IB what
exists, they never submit anything. No function here constructs an `Order` to
transmit, and the test suite greps the package to keep it that way
(docs/ROADMAP.md step 5 is where placement gets built, deliberately and behind
its own opt-in).

Two IBKR facts shape the API:

* Orders placed from IBKR Mobile belong to a different client id, so the
  Gateway only relays them when it is configured with a master client id
  (`OverrideTwsMasterClientID` in `~/ibc/config.ini`). Without it, `orders`
  legitimately returns an empty list while the phone shows a working order —
  hence `master_client_id_hint` in the payload.
* `reqExecutions` covers the current trading day only. Anything older needs the
  Flex Web Service (`IB_FLEX_QUERY_ID`), so the payload states its own window
  rather than letting a caller assume "all history".
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Sequence

from ib_async import ExecutionFilter

from .config import Settings
from .portfolio import connected, normalize_expiry

# Statuses IB considers still working. `reqAllOpenOrders` should only return
# these, but it occasionally includes a just-filled order, and a consumer
# reconciling intent against reality needs the distinction to be explicit.
ACTIVE_STATUSES = frozenset(
    {"ApiPending", "PendingSubmit", "PreSubmitted", "Submitted", "PendingCancel"}
)


def _clean(value: Any) -> float | None:
    import math

    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or number >= 1.7e308:  # IB's "unset price" sentinel
        return None
    return number


def _int_or_none(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number or None


@dataclass
class OrderRow:
    """One working order, flattened from Trade(contract, order, orderStatus)."""

    order_id: int | None
    perm_id: int | None
    client_id: int | None
    account: str
    con_id: int | None
    symbol: str  # IB local symbol
    underlying: str
    sec_type: str
    currency: str
    action: str  # BUY / SELL
    quantity: float | None
    order_type: str  # LMT / MKT / STP / ...
    limit_price: float | None = None
    stop_price: float | None = None
    tif: str = ""
    status: str = ""
    filled: float | None = None
    remaining: float | None = None
    avg_fill_price: float | None = None
    why_held: str = ""
    order_ref: str = ""
    # Derivatives only.
    expiry: str = ""
    strike: float | None = None
    right: str = ""
    multiplier: float | None = None

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    def as_dict(self) -> dict[str, Any]:
        return {**vars(self), "is_active": self.is_active}


@dataclass
class ExecutionRow:
    """One fill, with its commission report when IB has already sent it."""

    exec_id: str
    time: str  # ISO 8601
    account: str
    con_id: int | None
    symbol: str
    underlying: str
    sec_type: str
    currency: str
    side: str  # BOT / SLD
    shares: float | None
    price: float | None
    exchange: str = ""
    order_id: int | None = None
    perm_id: int | None = None
    cum_qty: float | None = None
    avg_price: float | None = None
    last_liquidity: int | None = None
    commission: float | None = None
    commission_currency: str = ""
    realized_pnl: float | None = None
    # Derivatives only.
    expiry: str = ""
    strike: float | None = None
    right: str = ""
    multiplier: float | None = None

    @property
    def proceeds(self) -> float | None:
        """Signed cash effect before commission: negative when buying."""
        if self.shares is None or self.price is None:
            return None
        gross = self.shares * self.price * (self.multiplier or 1.0)
        return -gross if self.side.upper().startswith("B") else gross

    def as_dict(self) -> dict[str, Any]:
        proceeds = self.proceeds
        return {**vars(self), "proceeds": None if proceeds is None else round(proceeds, 2)}


# --- mappers ---------------------------------------------------------------


def _contract_bits(contract: Any) -> dict[str, Any]:
    return {
        "con_id": _int_or_none(getattr(contract, "conId", None)),
        "symbol": (getattr(contract, "localSymbol", "") or getattr(contract, "symbol", "") or ""),
        "underlying": getattr(contract, "symbol", "") or "",
        "sec_type": getattr(contract, "secType", "") or "",
        "currency": getattr(contract, "currency", "") or "",
        "expiry": normalize_expiry(getattr(contract, "lastTradeDateOrContractMonth", "") or ""),
        "strike": _clean(getattr(contract, "strike", None)) or None,
        "right": (getattr(contract, "right", "") or "")[:1].upper(),
        "multiplier": _clean(getattr(contract, "multiplier", None)),
    }


def order_row_from_trade(trade: Any) -> OrderRow:
    """Trade -> OrderRow."""
    order = getattr(trade, "order", None)
    status = getattr(trade, "orderStatus", None)
    bits = _contract_bits(getattr(trade, "contract", None))
    return OrderRow(
        order_id=_int_or_none(getattr(order, "orderId", None)),
        perm_id=_int_or_none(getattr(order, "permId", None))
        or _int_or_none(getattr(status, "permId", None)),
        client_id=_int_or_none(getattr(order, "clientId", None)),
        account=getattr(order, "account", "") or "",
        action=(getattr(order, "action", "") or "").upper(),
        quantity=_clean(getattr(order, "totalQuantity", None)),
        order_type=(getattr(order, "orderType", "") or "").upper(),
        limit_price=_clean(getattr(order, "lmtPrice", None)),
        stop_price=_clean(getattr(order, "auxPrice", None)),
        tif=(getattr(order, "tif", "") or "").upper(),
        order_ref=getattr(order, "orderRef", "") or "",
        status=getattr(status, "status", "") or "",
        filled=_clean(getattr(status, "filled", None)),
        remaining=_clean(getattr(status, "remaining", None)),
        avg_fill_price=_clean(getattr(status, "avgFillPrice", None)),
        why_held=getattr(status, "whyHeld", "") or "",
        **bits,
    )


def execution_row_from_fill(fill: Any) -> ExecutionRow:
    """Fill(contract, execution, commissionReport, time) -> ExecutionRow."""
    ex = getattr(fill, "execution", None)
    report = getattr(fill, "commissionReport", None)
    bits = _contract_bits(getattr(fill, "contract", None))
    when = getattr(ex, "time", None) or getattr(fill, "time", None)
    return ExecutionRow(
        exec_id=getattr(ex, "execId", "") or "",
        time=when.isoformat() if hasattr(when, "isoformat") else str(when or ""),
        account=getattr(ex, "acctNumber", "") or "",
        side=(getattr(ex, "side", "") or "").upper(),
        shares=_clean(getattr(ex, "shares", None)),
        price=_clean(getattr(ex, "price", None)),
        exchange=getattr(ex, "exchange", "") or "",
        order_id=_int_or_none(getattr(ex, "orderId", None)),
        perm_id=_int_or_none(getattr(ex, "permId", None)),
        cum_qty=_clean(getattr(ex, "cumQty", None)),
        avg_price=_clean(getattr(ex, "avgPrice", None)),
        last_liquidity=_int_or_none(getattr(ex, "lastLiquidity", None)),
        commission=_clean(getattr(report, "commission", None)),
        commission_currency=getattr(report, "currency", "") or "",
        realized_pnl=_clean(getattr(report, "realizedPNL", None)),
        **bits,
    )


# --- IB glue ---------------------------------------------------------------


def fetch_open_orders(settings: Settings, *, active_only: bool = True) -> list[OrderRow]:
    """Working orders across every client id the Gateway will relay."""
    with connected(settings) as ib:
        trades = ib.reqAllOpenOrders()
        # `openTrades()` reads the local cache, which also holds orders this
        # client id learned about while connected; merging costs nothing and
        # avoids a race right after connecting.
        merged: dict[tuple[int | None, int | None], Any] = {}
        for trade in list(trades) + list(ib.openTrades()):
            order = getattr(trade, "order", None)
            key = (
                _int_or_none(getattr(order, "permId", None)),
                _int_or_none(getattr(order, "orderId", None)),
            )
            merged[key] = trade
        rows = [order_row_from_trade(t) for t in merged.values()]

    wanted = settings.account or ""
    if wanted:
        rows = [r for r in rows if not r.account or r.account == wanted]
    if active_only:
        rows = [r for r in rows if r.is_active]
    return sorted(rows, key=lambda r: (r.symbol, r.order_id or 0))


def fetch_executions(
    settings: Settings,
    *,
    symbol: str = "",
    sec_type: str = "",
    side: str = "",
    since: dt.datetime | None = None,
) -> list[ExecutionRow]:
    """Today's fills, optionally narrowed by IB's own execution filter.

    IB's `time` filter is a lower bound formatted `yyyymmdd hh:mm:ss`; it cannot
    reach back beyond the current trading day, whatever value is passed.
    """
    flt = ExecutionFilter(
        acctCode=settings.account or "",
        symbol=(symbol or "").upper(),
        secType=(sec_type or "").upper(),
        side=(side or "").upper(),
        time=since.strftime("%Y%m%d %H:%M:%S") if since else "",
    )
    with connected(settings) as ib:
        fills = ib.reqExecutions(flt)
        rows = [execution_row_from_fill(f) for f in fills]
    return sorted(rows, key=lambda r: (r.time, r.symbol))


# --- rendering -------------------------------------------------------------


def render_orders(rows: Sequence[OrderRow]) -> str:
    header = (
        f"{'SYMBOL':<24}{'ACT':<5}{'QTY':>7}  {'TYPE':<6}{'LMT':>10}{'STOP':>10}  "
        f"{'TIF':<5}{'FILLED':>8}{'REMAIN':>8}  {'STATUS':<12}WHY"
    )
    lines = [header, "-" * len(header)]

    def num(value: float | None, width: int, digits: int = 2) -> str:
        return "-".rjust(width) if value is None else f"{value:>{width},.{digits}f}"

    for r in rows:
        lines.append(
            f"{r.symbol[:23]:<24}{r.action[:4]:<5}{num(r.quantity, 7, 0)}  {r.order_type[:5]:<6}"
            f"{num(r.limit_price, 10)}{num(r.stop_price, 10)}  {r.tif[:4]:<5}"
            f"{num(r.filled, 8, 0)}{num(r.remaining, 8, 0)}  {r.status[:11]:<12}{r.why_held}"
        )
    return "\n".join(lines)


def render_executions(rows: Sequence[ExecutionRow]) -> str:
    header = (
        f"{'TIME':<21}{'SYMBOL':<24}{'SIDE':<5}{'SHARES':>9}{'PRICE':>11}"
        f"{'PROCEEDS':>13}{'COMM':>9}{'PNL':>11}  EXCH"
    )
    lines = [header, "-" * len(header)]

    def num(value: float | None, width: int, digits: int = 2) -> str:
        return "-".rjust(width) if value is None else f"{value:>{width},.{digits}f}"

    for r in rows:
        lines.append(
            f"{r.time[:20]:<21}{r.symbol[:23]:<24}{r.side[:4]:<5}{num(r.shares, 9, 0)}"
            f"{num(r.price, 11, 4)}{num(r.proceeds, 13)}{num(r.commission, 9)}"
            f"{num(r.realized_pnl, 11)}  {r.exchange}"
        )
    return "\n".join(lines)


def execution_totals(rows: Sequence[ExecutionRow]) -> dict[str, Any]:
    def total(values: list[float]) -> float | None:
        return round(sum(values), 2) if values else None

    return {
        "count": len(rows),
        "bought": sum(1 for r in rows if r.side.startswith("B")),
        "sold": sum(1 for r in rows if r.side.startswith("S")),
        "proceeds": total([r.proceeds for r in rows if r.proceeds is not None]),
        "commission": total([r.commission for r in rows if r.commission is not None]),
        "realized_pnl": total([r.realized_pnl for r in rows if r.realized_pnl is not None]),
    }


def order_totals(rows: Sequence[OrderRow]) -> dict[str, Any]:
    return {
        "count": len(rows),
        "active": sum(1 for r in rows if r.is_active),
        "buy": sum(1 for r in rows if r.action.startswith("B")),
        "sell": sum(1 for r in rows if r.action.startswith("S")),
        "options": sum(1 for r in rows if r.sec_type == "OPT"),
    }
