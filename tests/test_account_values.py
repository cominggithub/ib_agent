"""Unit tests for account-summary merging, using the exact row *shape* that
IB Gateway 10.37 returns from `accountSummary` (captured 2026-08-04).

The figures are fabricated. Only the structure matters here - duplicate
aggregate rows, tags outside SUMMARY_TAGS, repeated tags - so there is no reason
for a real balance to live in a repository.
"""

from __future__ import annotations

from dataclasses import dataclass

from ib_agent.portfolio import merge_account_values


@dataclass
class RawValue:
    """Stand-in for ib_async.objects.AccountValue."""

    account: str
    tag: str
    value: str
    currency: str


LIVE_ROWS = [
    RawValue("All", "AccruedCash", "100.00", "BASE"),
    RawValue("All", "AccruedCash", "100.00", "USD"),
    RawValue("All", "RealizedPnL", "0.00", "BASE"),
    RawValue("All", "RealizedPnL", "0.00", "USD"),
    RawValue("All", "UnrealizedPnL", "1000.00", "BASE"),
    RawValue("All", "UnrealizedPnL", "1000.00", "USD"),
    RawValue("U1234567", "AccruedCash", "100.00", "USD"),
    RawValue("U1234567", "AvailableFunds", "2000.00", "USD"),
    RawValue("U1234567", "BuyingPower", "8000.00", "USD"),
    RawValue("U1234567", "Cushion", "0.100000", ""),
    RawValue("U1234567", "ExcessLiquidity", "12000.00", "USD"),
    RawValue("U1234567", "GrossPositionValue", "20000.00", "USD"),
    RawValue("U1234567", "NetLiquidation", "100000.00", "USD"),
    RawValue("U1234567", "TotalCashValue", "120000.00", "USD"),
    # tags outside SUMMARY_TAGS must be dropped
    RawValue("U1234567", "AccountType", "INDIVIDUAL", ""),
    RawValue("U1234567", "LookAheadNextChange", "1785850200", ""),
]


def test_aggregate_duplicate_is_dropped() -> None:
    merged = merge_account_values(LIVE_ROWS)
    accrued_usd = [v for v in merged if v.tag == "AccruedCash" and v.currency == "USD"]
    assert len(accrued_usd) == 1
    assert accrued_usd[0].account == "U1234567"


def test_aggregate_only_tags_are_kept() -> None:
    merged = merge_account_values(LIVE_ROWS)
    unrealized = {(v.account, v.currency) for v in merged if v.tag == "UnrealizedPnL"}
    # no real-account row exists for UnrealizedPnL, so the roll-up survives
    assert unrealized == {("All", "BASE"), ("All", "USD")}


def test_unknown_tags_filtered() -> None:
    tags = {v.tag for v in merge_account_values(LIVE_ROWS)}
    assert "AccountType" not in tags
    assert "LookAheadNextChange" not in tags


def test_repeated_identical_rows_collapse() -> None:
    rows = LIVE_ROWS + [RawValue("U1234567", "NetLiquidation", "100999.99", "USD")]
    merged = merge_account_values(rows)
    netliq = [v for v in merged if v.tag == "NetLiquidation"]
    assert len(netliq) == 1
    # last value wins
    assert netliq[0].value == "100999.99"


def test_blank_account_treated_as_aggregate() -> None:
    rows = [
        RawValue("", "NetLiquidation", "1.00", "USD"),
        RawValue("U1", "NetLiquidation", "2.00", "USD"),
    ]
    merged = merge_account_values(rows)
    assert [(v.account, v.value) for v in merged] == [("U1", "2.00")]


def test_multi_account_rows_both_kept() -> None:
    rows = [
        RawValue("U1", "NetLiquidation", "1.00", "USD"),
        RawValue("U2", "NetLiquidation", "2.00", "USD"),
        RawValue("All", "NetLiquidation", "3.00", "USD"),
    ]
    merged = merge_account_values(rows)
    assert [(v.account, v.value) for v in merged] == [("U1", "1.00"), ("U2", "2.00")]
