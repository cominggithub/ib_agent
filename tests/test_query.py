"""Tests for the position query layer, shaped like the real portfolio
(short option premium on a mix of common stocks and ETFs)."""

from __future__ import annotations

import datetime as dt

import pytest

from ib_agent import query
from ib_agent.portfolio import PositionRow, normalize_expiry

TODAY = dt.date(2026, 8, 4)


def osi_symbol(underlying: str, expiry: str, right: str, strike: float) -> str:
    """IB local symbol, e.g. 'GDX   260821P00065000'."""
    yymmdd = expiry.replace("-", "")[2:]
    return f"{underlying:<6}{yymmdd}{right}{int(strike * 1000):08d}"


def opt(
    underlying: str,
    expiry: str,
    right: str,
    strike: float,
    qty: float = -1,
    asset_class: str = "COMMON",
    value: float = -100.0,
    pnl: float = 25.0,
) -> PositionRow:
    return PositionRow(
        account="U1234567",
        con_id=abs(hash((underlying, expiry, right, strike))) % 10**9,
        symbol=osi_symbol(underlying, expiry, right, strike),
        sec_type="OPT",
        exchange="SMART",
        currency="USD",
        quantity=qty,
        avg_cost=300.0,
        market_price=1.0,
        market_value=value,
        unrealized_pnl=pnl,
        realized_pnl=0.0,
        underlying=underlying,
        expiry=expiry,
        strike=strike,
        right=right,
        multiplier=100.0,
        asset_class=asset_class,
    )


def stk(symbol: str, qty: float = 100, asset_class: str = "ETF") -> PositionRow:
    return PositionRow(
        account="U1234567",
        con_id=abs(hash(symbol)) % 10**9,
        symbol=symbol,
        sec_type="STK",
        exchange="ARCA",
        currency="USD",
        quantity=qty,
        avg_cost=200.0,
        market_price=210.0,
        market_value=qty * 210.0,
        unrealized_pnl=qty * 10.0,
        realized_pnl=0.0,
        underlying=symbol,
        asset_class=asset_class,
    )


@pytest.fixture
def rows() -> list[PositionRow]:
    return [
        opt("GDX", "2026-08-21", "P", 65.0, asset_class="ETF", pnl=107.6),
        opt("GDX", "2026-08-21", "C", 80.0, asset_class="ETF", pnl=-40.0),
        opt("SOXL", "2026-09-18", "P", 57.0, asset_class="ETF", value=-342.9, pnl=64.3),
        opt("AAPL", "2026-09-04", "C", 300.0, qty=-2, value=-800.0, pnl=-435.5),
        opt("BIDU", "2026-09-18", "C", 150.0, asset_class="ADR", pnl=287.9),
        opt("SOXX", "2026-12-18", "P", 420.0, asset_class="ETF", value=-2739.7, pnl=-14.0),
        stk("SPY", qty=50),
    ]


# --- input normalisation ---------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [("20260904", "2026-09-04"), ("202609", "2026-09"), ("", ""), ("2026-09-04", "2026-09-04")],
)
def test_normalize_expiry(raw: str, expected: str) -> None:
    assert normalize_expiry(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2026-09", "2026-09"),
        ("202609", "2026-09"),
        ("20260904", "2026-09-04"),
        ("2026", "2026"),
    ],
)
def test_normalize_expiry_filter(raw: str, expected: str) -> None:
    assert query.normalize_expiry_filter(raw) == expected


def test_sec_type_aliases() -> None:
    assert query.normalize_sec_types(["stock", "option"]) == ["STK", "OPT"]
    assert query.normalize_sec_types(["etf"]) == ["STK"]  # ETFs are STK to IB
    assert query.normalize_sec_types(["WAR"]) == ["WAR"]  # unknown passes through


def test_right_aliases() -> None:
    assert query.normalize_rights(["put", "calls"]) == ["P", "C"]
    assert query.normalize_rights(["c"]) == ["C"]


def test_parse_csv() -> None:
    assert query.parse_csv(" a, b ,c ") == ["a", "b", "c"]
    assert query.parse_csv(None) == []
    assert query.parse_csv("") == []


# --- filtering -------------------------------------------------------------


def test_no_filter_returns_everything(rows: list[PositionRow]) -> None:
    assert len(query.apply_filter(rows, query.PositionFilter(), TODAY)) == len(rows)


def test_options_only(rows: list[PositionRow]) -> None:
    got = query.apply_filter(rows, query.PositionFilter(options_only=True), TODAY)
    assert len(got) == 6
    assert all(r.sec_type == "OPT" for r in got)


def test_equities_only(rows: list[PositionRow]) -> None:
    got = query.apply_filter(rows, query.PositionFilter(equities_only=True), TODAY)
    assert [r.symbol for r in got] == ["SPY"]


def test_filter_by_right(rows: list[PositionRow]) -> None:
    puts = query.apply_filter(rows, query.PositionFilter(rights=["P"]), TODAY)
    assert {r.underlying for r in puts} == {"GDX", "SOXL", "SOXX"}


def test_filter_by_asset_class(rows: list[PositionRow]) -> None:
    etfs = query.apply_filter(rows, query.PositionFilter(asset_classes=["ETF"]), TODAY)
    assert {r.underlying for r in etfs} == {"GDX", "SOXL", "SOXX", "SPY"}


def test_filter_by_underlying(rows: list[PositionRow]) -> None:
    got = query.apply_filter(rows, query.PositionFilter(underlyings=["GDX"]), TODAY)
    assert len(got) == 2


def test_filter_by_expiry_prefix(rows: list[PositionRow]) -> None:
    flt = query.PositionFilter(expiry_prefixes=["2026-09"])
    got = query.apply_filter(rows, flt, TODAY)
    assert {r.underlying for r in got} == {"SOXL", "AAPL", "BIDU"}


def test_filter_by_exact_expiry(rows: list[PositionRow]) -> None:
    flt = query.PositionFilter(expiry_prefixes=["2026-08-21"])
    assert len(query.apply_filter(rows, flt, TODAY)) == 2


def test_filter_expiry_range_excludes_non_dated(rows: list[PositionRow]) -> None:
    flt = query.PositionFilter(expiry_from="2026-09-01", expiry_to="2026-09-30")
    got = query.apply_filter(rows, flt, TODAY)
    assert len(got) == 3
    assert all(r.sec_type == "OPT" for r in got)  # the stock has no expiry


def test_filter_by_dte(rows: list[PositionRow]) -> None:
    # 2026-08-21 is 17 days out from TODAY
    got = query.apply_filter(rows, query.PositionFilter(dte_max=20), TODAY)
    assert {r.expiry for r in got} == {"2026-08-21"}
    assert query.apply_filter(rows, query.PositionFilter(dte_min=200), TODAY) == []


def test_filter_by_side(rows: list[PositionRow]) -> None:
    shorts = query.apply_filter(rows, query.PositionFilter(side="short"), TODAY)
    longs = query.apply_filter(rows, query.PositionFilter(side="long"), TODAY)
    assert len(shorts) == 6
    assert [r.symbol for r in longs] == ["SPY"]


def test_filter_contains_is_case_insensitive(rows: list[PositionRow]) -> None:
    got = query.apply_filter(rows, query.PositionFilter(contains="soxl"), TODAY)
    assert len(got) == 1


def test_filters_combine(rows: list[PositionRow]) -> None:
    flt = query.PositionFilter(options_only=True, rights=["P"], asset_classes=["ETF"], dte_max=60)
    got = query.apply_filter(rows, flt, TODAY)
    assert {r.underlying for r in got} == {"GDX", "SOXL"}


def test_describe_only_lists_active_filters() -> None:
    flt = query.PositionFilter(rights=["P"], dte_max=30, options_only=True)
    assert flt.describe() == {"rights": ["P"], "dte_max": 30, "options_only": True}


# --- grouping, sorting, totals --------------------------------------------


def test_group_by_expiry(rows: list[PositionRow]) -> None:
    groups = dict(query.group_rows(rows, "expiry"))
    assert groups["2026-08-21"] and len(groups["2026-08-21"]) == 2
    assert "(none)" in groups  # the stock position


def test_group_by_right_labels(rows: list[PositionRow]) -> None:
    keys = [k for k, _ in query.group_rows(rows, "right")]
    assert keys == ["(none)", "CALL", "PUT"]


def test_group_keys_all_supported(rows: list[PositionRow]) -> None:
    for key in query.GROUP_KEYS:
        assert query.group_rows(rows, key)


def test_sort_by_expiry_then_strike(rows: list[PositionRow]) -> None:
    sorted_rows = query.sort_rows([r for r in rows if r.is_option], "expiry")
    assert [r.expiry for r in sorted_rows] == [
        "2026-08-21",
        "2026-08-21",
        "2026-09-04",
        "2026-09-18",
        "2026-09-18",
        "2026-12-18",
    ]


def test_sort_by_value_is_descending(rows: list[PositionRow]) -> None:
    values = [r.market_value for r in query.sort_rows(rows, "value")]
    assert values == sorted(values, reverse=True)


def test_summarize(rows: list[PositionRow]) -> None:
    totals = query.summarize(rows)
    assert totals.count == 7
    assert totals.contracts == pytest.approx(43.0)  # 50 long shares - 7 short contracts
    # options -4,182.60 plus 50 SPY shares at 210
    assert totals.market_value == pytest.approx(6317.4, abs=0.1)
    assert totals.unrealized_pnl == pytest.approx(470.3, abs=0.1)


def test_summarize_ignores_missing_market_data() -> None:
    row = opt("GDX", "2026-08-21", "P", 65.0)
    row.market_value = None
    row.unrealized_pnl = None
    totals = query.summarize([row])
    assert totals.market_value == 0.0
    assert totals.count == 1


def test_totals_as_dict_rounds() -> None:
    totals = query.Totals(count=1, contracts=1, market_value=1.23456, unrealized_pnl=2.5)
    assert totals.as_dict()["market_value"] == 1.23


# --- rendering -------------------------------------------------------------


def test_render_table_contains_data(rows: list[PositionRow]) -> None:
    text = query.render_table(rows)
    assert "SYMBOL" in text and "EXPIRY" in text
    assert "GDX" in text
    assert len(text.splitlines()) == len(rows) + 2  # header + rule


def test_render_table_dashes_missing_values() -> None:
    row = opt("GDX", "2026-08-21", "P", 65.0)
    row.market_price = None
    assert "-" in query.render_table([row]).splitlines()[-1]


def test_pick_columns_switches_on_content(rows: list[PositionRow]) -> None:
    assert query.pick_columns(rows) is query.DEFAULT_COLUMNS
    assert query.pick_columns([stk("SPY")]) is query.EQUITY_COLUMNS


def test_row_to_dict_adds_derived_fields() -> None:
    data = query.row_to_dict(opt("GDX", "2026-08-21", "P", 65.0), TODAY)
    assert data["days_to_expiry"] == 17
    assert data["side"] == "short"
    assert data["cost_basis"] == -300.0


def test_days_to_expiry_none_without_expiry() -> None:
    assert stk("SPY").days_to_expiry(TODAY) is None
