"""Filtering, grouping and rendering of position rows.

Deliberately free of IB and sqlite imports so all of it is unit-testable:
`positions in -> rows/groups/totals out`.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

from .portfolio import PositionRow

# Friendly aliases accepted on the command line for IB security types.
SEC_TYPE_ALIASES = {
    "stock": "STK",
    "stocks": "STK",
    "stk": "STK",
    "equity": "STK",
    "share": "STK",
    "shares": "STK",
    "etf": "STK",  # ETFs are STK to IB; asset_class separates them
    "option": "OPT",
    "options": "OPT",
    "opt": "OPT",
    "future": "FUT",
    "futures": "FUT",
    "fut": "FUT",
    "fop": "FOP",
    "cash": "CASH",
    "forex": "CASH",
    "fx": "CASH",
    "bond": "BOND",
    "crypto": "CRYPTO",
    "fund": "FUND",
}

RIGHT_ALIASES = {
    "c": "C",
    "call": "C",
    "calls": "C",
    "p": "P",
    "put": "P",
    "puts": "P",
}

GROUP_KEYS = ("expiry", "right", "underlying", "sec_type", "asset_class", "account", "side")


def parse_csv(value: str | None) -> list[str]:
    """"a, b ,c" -> ["a", "b", "c"]"""
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def normalize_sec_types(values: Iterable[str]) -> list[str]:
    out: list[str] = []
    for v in values:
        key = v.strip().lower()
        mapped = SEC_TYPE_ALIASES.get(key, v.strip().upper())
        if mapped not in out:
            out.append(mapped)
    return out


def normalize_rights(values: Iterable[str]) -> list[str]:
    out: list[str] = []
    for v in values:
        mapped = RIGHT_ALIASES.get(v.strip().lower(), v.strip().upper()[:1])
        if mapped and mapped not in out:
            out.append(mapped)
    return out


def normalize_expiry_filter(value: str) -> str:
    """Accept 2026-09, 202609, 20260904, 2026-09-04 -> ISO prefix to match on."""
    raw = value.strip()
    if raw.isdigit():
        if len(raw) == 8:
            return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"
        if len(raw) == 6:
            return f"{raw[0:4]}-{raw[4:6]}"
        if len(raw) == 4:
            return raw
    return raw


@dataclass
class PositionFilter:
    """All filters are AND-ed; empty fields mean "no restriction"."""

    sec_types: list[str] = field(default_factory=list)
    rights: list[str] = field(default_factory=list)
    underlyings: list[str] = field(default_factory=list)
    asset_classes: list[str] = field(default_factory=list)
    accounts: list[str] = field(default_factory=list)
    expiry_prefixes: list[str] = field(default_factory=list)
    expiry_from: str = ""
    expiry_to: str = ""
    dte_max: int | None = None
    dte_min: int | None = None
    side: str = ""  # "long" | "short"
    contains: str = ""
    options_only: bool = False
    equities_only: bool = False

    def matches(self, row: PositionRow, today: dt.date | None = None) -> bool:
        if self.sec_types and row.sec_type not in self.sec_types:
            return False
        if self.options_only and not row.is_option:
            return False
        if self.equities_only and row.sec_type != "STK":
            return False
        if self.rights and row.right not in self.rights:
            return False
        if self.underlyings and row.underlying.upper() not in self.underlyings:
            return False
        if self.asset_classes and row.asset_class.upper() not in self.asset_classes:
            return False
        if self.accounts and row.account not in self.accounts:
            return False
        if self.expiry_prefixes and not any(
            row.expiry.startswith(p) for p in self.expiry_prefixes
        ):
            return False
        if self.expiry_from and (not row.expiry or row.expiry < self.expiry_from):
            return False
        if self.expiry_to and (not row.expiry or row.expiry > self.expiry_to):
            return False
        if self.dte_max is not None or self.dte_min is not None:
            dte = row.days_to_expiry(today)
            if dte is None:
                return False
            if self.dte_max is not None and dte > self.dte_max:
                return False
            if self.dte_min is not None and dte < self.dte_min:
                return False
        if self.side and row.side != self.side:
            return False
        if self.contains and self.contains.upper() not in row.symbol.upper():
            return False
        return True

    def describe(self) -> dict[str, object]:
        """Non-empty filters, for JSON output / reproducibility."""
        out: dict[str, object] = {}
        for key, value in vars(self).items():
            if value in ([], "", None, False):
                continue
            out[key] = value
        return out


def apply_filter(
    rows: Iterable[PositionRow], flt: PositionFilter, today: dt.date | None = None
) -> list[PositionRow]:
    return [r for r in rows if flt.matches(r, today)]


SORT_KEYS: dict[str, Callable[[PositionRow], object]] = {
    "symbol": lambda r: r.symbol,
    "underlying": lambda r: (r.underlying, r.expiry, r.strike or 0.0),
    "expiry": lambda r: (r.expiry or "9999", r.underlying, r.strike or 0.0),
    "strike": lambda r: (r.strike if r.strike is not None else 0.0),
    "quantity": lambda r: r.quantity,
    "value": lambda r: -(r.market_value or 0.0),
    "pnl": lambda r: -(r.unrealized_pnl or 0.0),
    "dte": lambda r: (r.days_to_expiry() if r.expiry else 10**6),
}


def sort_rows(rows: Sequence[PositionRow], key: str) -> list[PositionRow]:
    return sorted(rows, key=SORT_KEYS.get(key, SORT_KEYS["symbol"]))


GROUP_GETTERS: dict[str, Callable[[PositionRow], str]] = {
    "expiry": lambda r: r.expiry or "(none)",
    "right": lambda r: {"C": "CALL", "P": "PUT"}.get(r.right, "(none)"),
    "underlying": lambda r: r.underlying or "(none)",
    "sec_type": lambda r: r.sec_type,
    "asset_class": lambda r: r.asset_class or "(unclassified)",
    "account": lambda r: r.account,
    "side": lambda r: r.side,
}


def group_rows(rows: Iterable[PositionRow], by: str) -> list[tuple[str, list[PositionRow]]]:
    getter = GROUP_GETTERS[by]
    buckets: dict[str, list[PositionRow]] = {}
    for row in rows:
        buckets.setdefault(getter(row), []).append(row)
    return sorted(buckets.items(), key=lambda kv: kv[0])


@dataclass
class Totals:
    count: int = 0
    contracts: float = 0.0
    market_value: float = 0.0
    unrealized_pnl: float = 0.0
    cost_basis: float = 0.0

    def as_dict(self) -> dict[str, float | int]:
        return {
            "count": self.count,
            "contracts": round(self.contracts, 4),
            "market_value": round(self.market_value, 2),
            "unrealized_pnl": round(self.unrealized_pnl, 2),
            "cost_basis": round(self.cost_basis, 2),
        }


def summarize(rows: Iterable[PositionRow]) -> Totals:
    totals = Totals()
    for row in rows:
        totals.count += 1
        totals.contracts += row.quantity
        totals.market_value += row.market_value or 0.0
        totals.unrealized_pnl += row.unrealized_pnl or 0.0
        totals.cost_basis += row.cost_basis
    return totals


def row_to_dict(row: PositionRow, today: dt.date | None = None) -> dict[str, object]:
    data = dict(vars(row))
    data["days_to_expiry"] = row.days_to_expiry(today)
    data["side"] = row.side
    data["cost_basis"] = round(row.cost_basis, 2)
    return data


# --- table rendering -------------------------------------------------------


@dataclass
class Column:
    header: str
    get: Callable[[PositionRow], object]
    width: int
    align: str = ">"
    digits: int | None = None


def _cell(value: object, col: Column) -> str:
    if value is None or value == "":
        text = "-"
    elif col.digits is not None and isinstance(value, (int, float)):
        text = f"{value:,.{col.digits}f}"
    else:
        text = str(value)
    if len(text) > col.width:
        text = text[: col.width]
    return f"{text:{col.align}{col.width}}"


DEFAULT_COLUMNS: list[Column] = [
    Column("SYMBOL", lambda r: r.symbol, 22, "<"),
    Column("UND", lambda r: r.underlying, 6, "<"),
    Column("T", lambda r: "O" if r.is_option else r.sec_type[:1], 2, "<"),
    Column("R", lambda r: r.right, 2, "<"),
    Column("EXPIRY", lambda r: r.expiry, 11, "<"),
    Column("DTE", lambda r: r.days_to_expiry(), 5),
    Column("STRIKE", lambda r: r.strike, 9, ">", 2),
    Column("QTY", lambda r: r.quantity, 8, ">", 0),
    Column("AVG COST", lambda r: r.avg_cost, 11, ">", 2),
    Column("PRICE", lambda r: r.market_price, 9, ">", 2),
    Column("VALUE", lambda r: r.market_value, 12, ">", 2),
    Column("UNREAL", lambda r: r.unrealized_pnl, 11, ">", 2),
]

EQUITY_COLUMNS: list[Column] = [
    Column("SYMBOL", lambda r: r.symbol, 12, "<"),
    Column("CLASS", lambda r: r.asset_class, 8, "<"),
    Column("CCY", lambda r: r.currency, 4, "<"),
    Column("QTY", lambda r: r.quantity, 10, ">", 2),
    Column("AVG COST", lambda r: r.avg_cost, 11, ">", 2),
    Column("PRICE", lambda r: r.market_price, 10, ">", 2),
    Column("VALUE", lambda r: r.market_value, 13, ">", 2),
    Column("UNREAL", lambda r: r.unrealized_pnl, 12, ">", 2),
]


def render_table(rows: Sequence[PositionRow], columns: Sequence[Column] | None = None) -> str:
    cols = list(columns or DEFAULT_COLUMNS)
    header = " ".join(f"{c.header:{c.align}{c.width}}" for c in cols)
    lines = [header, "-" * len(header)]
    for row in rows:
        lines.append(" ".join(_cell(c.get(row), c) for c in cols))
    return "\n".join(lines)


def render_totals(totals: Totals, label: str = "TOTAL") -> str:
    return (
        f"{label}: {totals.count} positions  "
        f"contracts {totals.contracts:,.0f}  "
        f"value {totals.market_value:,.2f}  "
        f"unrealized {totals.unrealized_pnl:,.2f}"
    )


def pick_columns(rows: Sequence[PositionRow]) -> list[Column]:
    """Show the option columns only when options are actually present."""
    return DEFAULT_COLUMNS if any(r.is_option for r in rows) else EQUITY_COLUMNS
