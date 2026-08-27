"""Tests for `activity.py`: working orders and executions.

Offline, against stubs shaped like `Trade` and `Fill`. Two behaviours matter
beyond field mapping, and both are asserted here: an order's active/inactive
split (a consumer reconciling intent must not treat a filled order as working)
and the sign of an execution's cash effect.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import pytest

from ib_agent import activity


@dataclass
class StubContract:
    conId: int = 0
    symbol: str = ""
    secType: str = ""
    currency: str = "USD"
    localSymbol: str = ""
    lastTradeDateOrContractMonth: str = ""
    strike: float = 0.0
    right: str = ""
    multiplier: str = ""


@dataclass
class StubOrder:
    orderId: int = 0
    clientId: int = 0
    permId: int = 0
    action: str = ""
    totalQuantity: float = 0.0
    orderType: str = ""
    lmtPrice: float = 0.0
    auxPrice: float = 0.0
    tif: str = ""
    account: str = ""
    orderRef: str = ""


@dataclass
class StubStatus:
    status: str = ""
    filled: float = 0.0
    remaining: float = 0.0
    avgFillPrice: float = 0.0
    permId: int = 0
    clientId: int = 0
    whyHeld: str = ""


@dataclass
class StubTrade:
    contract: StubContract
    order: StubOrder
    orderStatus: StubStatus


@dataclass
class StubExecution:
    execId: str = ""
    time: dt.datetime | None = None
    acctNumber: str = ""
    exchange: str = ""
    side: str = ""
    shares: float = 0.0
    price: float = 0.0
    permId: int = 0
    clientId: int = 0
    orderId: int = 0
    cumQty: float = 0.0
    avgPrice: float = 0.0
    lastLiquidity: int = 0


@dataclass
class StubReport:
    execId: str = ""
    commission: float = 0.0
    currency: str = "USD"
    realizedPNL: float = 0.0


@dataclass
class StubFill:
    contract: StubContract
    execution: StubExecution
    commissionReport: StubReport
    time: dt.datetime | None = None


def option() -> StubContract:
    return StubContract(
        conId=778899,
        symbol="GDX",
        secType="OPT",
        localSymbol="GDX   260918P00045000",
        lastTradeDateOrContractMonth="20260918",
        strike=45.0,
        right="P",
        multiplier="100",
    )


def working_order(status: str = "Submitted") -> StubTrade:
    return StubTrade(
        contract=option(),
        order=StubOrder(
            orderId=41,
            clientId=17,
            permId=900001,
            action="SELL",
            totalQuantity=2,
            orderType="LMT",
            lmtPrice=1.75,
            tif="GTC",
            account="U1",
        ),
        orderStatus=StubStatus(status=status, filled=0, remaining=2, permId=900001),
    )


# --- orders ----------------------------------------------------------------


def test_order_row_flattens_trade_order_and_status():
    row = activity.order_row_from_trade(working_order())
    assert (row.order_id, row.perm_id, row.client_id) == (41, 900001, 17)
    assert row.action == "SELL"
    assert row.order_type == "LMT"
    assert row.limit_price == 1.75
    assert row.expiry == "2026-09-18"  # normalised from YYYYMMDD
    assert row.is_active
    assert row.as_dict()["is_active"] is True


@pytest.mark.parametrize(
    "status,active",
    [
        ("Submitted", True),
        ("PreSubmitted", True),
        ("PendingSubmit", True),
        ("PendingCancel", True),
        ("Filled", False),
        ("Cancelled", False),
        ("Inactive", False),
    ],
)
def test_active_split_matches_ibs_status_vocabulary(status, active):
    assert activity.order_row_from_trade(working_order(status)).is_active is active


def test_unset_prices_do_not_leak_ibs_sentinel():
    """IB sends 1.7976931348623157e+308 for "no price"; JSON should say null."""
    trade = working_order()
    trade.order.lmtPrice = 1.7976931348623157e308
    trade.order.auxPrice = float("nan")
    row = activity.order_row_from_trade(trade)
    assert row.limit_price is None and row.stop_price is None


def test_order_totals_counts_sides_and_options():
    buy = working_order()
    buy.order.action = "BUY"
    totals = activity.order_totals(
        [activity.order_row_from_trade(working_order()), activity.order_row_from_trade(buy)]
    )
    assert totals == {"count": 2, "active": 2, "buy": 1, "sell": 1, "options": 2}


# --- executions ------------------------------------------------------------


def sold_fill() -> StubFill:
    return StubFill(
        contract=option(),
        execution=StubExecution(
            execId="0001.abc",
            time=dt.datetime(2026, 8, 17, 13, 45, tzinfo=dt.UTC),
            acctNumber="U1",
            exchange="CBOE",
            side="SLD",
            shares=2,
            price=1.55,
            orderId=41,
            permId=900001,
            cumQty=2,
            avgPrice=1.55,
        ),
        commissionReport=StubReport(commission=1.30, realizedPNL=42.0),
    )


def test_execution_row_maps_fill_execution_and_commission():
    row = activity.execution_row_from_fill(sold_fill())
    assert row.exec_id == "0001.abc"
    assert row.time.startswith("2026-08-17T13:45")
    assert row.side == "SLD"
    assert row.commission == 1.30
    assert row.realized_pnl == 42.0
    assert row.expiry == "2026-09-18"


def test_proceeds_sign_follows_the_side_and_scales_by_multiplier():
    sold = activity.execution_row_from_fill(sold_fill())
    assert sold.proceeds == pytest.approx(310.0)  # 2 x 1.55 x 100, cash in

    bought = sold_fill()
    bought.execution.side = "BOT"
    assert activity.execution_row_from_fill(bought).proceeds == pytest.approx(-310.0)


def test_execution_totals_sum_the_day():
    bought = sold_fill()
    bought.execution.side = "BOT"
    bought.execution.price = 1.00
    bought.commissionReport = StubReport(commission=1.10, realizedPNL=0.0)
    totals = activity.execution_totals(
        [
            activity.execution_row_from_fill(sold_fill()),
            activity.execution_row_from_fill(bought),
        ]
    )
    assert totals["count"] == 2
    assert totals["bought"] == 1 and totals["sold"] == 1
    assert totals["proceeds"] == pytest.approx(110.0)  # +310 sold, -200 bought
    assert totals["commission"] == pytest.approx(2.40)
    assert totals["realized_pnl"] == pytest.approx(42.0)


def test_missing_commission_report_is_not_zero():
    """IB sends the report separately; absent must not read as "free"."""
    fill = sold_fill()
    fill.commissionReport = StubReport(commission=float("nan"), realizedPNL=float("nan"))
    row = activity.execution_row_from_fill(fill)
    assert row.commission is None and row.realized_pnl is None


# --- rendering -------------------------------------------------------------


def test_renderers_emit_headers():
    orders = activity.render_orders([activity.order_row_from_trade(working_order())])
    assert "STATUS" in orders and "LMT" in orders
    fills = activity.render_executions([activity.execution_row_from_fill(sold_fill())])
    assert "PROCEEDS" in fills


def test_no_order_placing_calls_anywhere_in_the_package():
    """The read-only guarantee, enforced as a test rather than a promise.

    `activity.py` reads orders; nothing in the package may transmit one. If a
    future change adds order placement outside ROADMAP step 5, this fails.
    """
    import pathlib

    import ib_agent

    package = pathlib.Path(ib_agent.__file__).parent
    offenders: list[str] = []
    for path in package.glob("*.py"):
        text = path.read_text()
        for needle in ("placeOrder", "whatIfOrder", "cancelOrder", "reqGlobalCancel"):
            if needle in text:
                offenders.append(f"{path.name}: {needle}")
    assert not offenders, f"order-placing calls found: {offenders}"
