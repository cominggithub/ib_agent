"""Tests for `market.py`: resolve, chain and greeks.

Everything here runs offline. The IB objects are replaced by stubs shaped like
the real ones (`ContractDetails`, `OptionChain`, `Ticker`), which is the point of
keeping the mappers pure: the field-by-field translation is where mistakes
actually happen, and it needs no socket to test.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from ib_agent import market
from ib_agent.portfolio import PositionRow


# --- stubs shaped like the IB objects --------------------------------------


@dataclass
class StubContract:
    conId: int = 0
    symbol: str = ""
    secType: str = ""
    currency: str = "USD"
    exchange: str = ""
    primaryExchange: str = ""
    localSymbol: str = ""
    tradingClass: str = ""
    lastTradeDateOrContractMonth: str = ""
    strike: float = 0.0
    right: str = ""
    multiplier: str = ""


@dataclass
class StubDetails:
    contract: StubContract
    longName: str = ""
    stockType: str = ""
    industry: str = ""
    category: str = ""
    minTick: float = 0.01
    underSymbol: str = ""
    underConId: int = 0
    underSecType: str = ""


@dataclass
class StubChain:
    exchange: str = "SMART"
    underlyingConId: int = 0
    tradingClass: str = ""
    multiplier: str = "100"
    expirations: set = field(default_factory=set)
    strikes: set = field(default_factory=set)


@dataclass
class StubGreeks:
    impliedVol: float | None = None
    delta: float | None = None
    optPrice: float | None = None
    pvDividend: float | None = None
    gamma: float | None = None
    vega: float | None = None
    theta: float | None = None
    undPrice: float | None = None


@dataclass
class StubTicker:
    contract: StubContract
    bid: float = float("nan")
    ask: float = float("nan")
    last: float = float("nan")
    modelGreeks: StubGreeks | None = None
    lastGreeks: StubGreeks | None = None
    bidGreeks: StubGreeks | None = None
    askGreeks: StubGreeks | None = None


def option_contract() -> StubContract:
    return StubContract(
        conId=778899,
        symbol="GDX",
        secType="OPT",
        exchange="SMART",
        localSymbol="GDX   260918P00045000",
        tradingClass="GDX",
        lastTradeDateOrContractMonth="20260918",
        strike=45.0,
        right="P",
        multiplier="100",
    )


# --- parse_spec ------------------------------------------------------------


def test_parse_spec_accepts_a_bare_conid():
    contract = market.parse_spec("12345")
    assert contract.conId == 12345


def test_parse_spec_accepts_symbol_and_sec_type():
    assert market.parse_spec("GDX").secType == "STK"
    assert market.parse_spec("GDX:STK").secType == "STK"
    assert market.parse_spec("EUR:CASH").secType == "CASH"


@pytest.mark.parametrize("spec", ["GDX 2026-09-18 P 45", "GDX/20260918/P/45"])
def test_parse_spec_accepts_an_option_in_either_separator(spec):
    contract = market.parse_spec(spec)
    assert contract.secType == "OPT"
    assert contract.lastTradeDateOrContractMonth == "20260918"
    assert contract.strike == 45.0
    assert contract.right == "P"


def test_parse_spec_rejects_an_empty_spec():
    with pytest.raises(ValueError):
        market.parse_spec("   ")


# --- resolve ---------------------------------------------------------------


def test_resolved_from_details_carries_the_underlying_conid():
    """The whole point of `resolve --from-positions`: IB states the link."""
    details = StubDetails(
        contract=option_contract(),
        longName="VanEck Gold Miners ETF",
        underSymbol="GDX",
        underConId=257793,
        underSecType="STK",
    )
    row = market.resolved_from_details(details)
    assert row.con_id == 778899
    assert row.underlying_con_id == 257793
    assert row.expiry == "2026-09-18"  # normalised out of IB's YYYYMMDD
    assert row.right == "P"
    assert row.multiplier == 100.0


def test_resolved_as_dict_omits_option_fields_for_a_stock():
    """A null expiry on a stock is ambiguous; absence is not."""
    details = StubDetails(
        contract=StubContract(conId=257793, symbol="GDX", secType="STK", exchange="SMART"),
        stockType="ETF",
    )
    data = market.resolved_from_details(details).as_dict()
    assert data["asset_class"] == "ETF"
    assert "strike" not in data and "expiry" not in data

    option = market.resolved_from_details(StubDetails(contract=option_contract())).as_dict()
    assert option["strike"] == 45.0


# --- chain -----------------------------------------------------------------


def test_chain_from_params_normalises_and_sorts():
    chain = market.chain_from_params(
        StubChain(
            underlyingConId=257793,
            tradingClass="GDX",
            expirations={"20261016", "20260918"},
            strikes={45.0, 40.0, 50.0},
        ),
        underlying_symbol="GDX",
    )
    assert chain.expirations == ["2026-09-18", "2026-10-16"]
    assert chain.strikes == [40.0, 45.0, 50.0]
    assert chain.as_dict()["expiration_count"] == 2
    assert chain.as_dict()["strike_count"] == 3


# --- greeks ----------------------------------------------------------------


def test_greeks_prefers_model_and_reports_its_source():
    ticker = StubTicker(
        contract=option_contract(),
        bid=1.50,
        ask=1.60,
        modelGreeks=StubGreeks(
            impliedVol=0.31, delta=-0.28, gamma=0.04, vega=0.09, theta=-0.02,
            optPrice=1.55, undPrice=48.2,
        ),
    )
    row = market.greeks_from_ticker(ticker, quantity=-2)
    assert row.source == "model"
    assert row.delta == -0.28
    assert row.bid == 1.50
    # Short 2 puts at delta -0.28 on a x100 multiplier: +56 of position delta.
    assert row.position_delta == pytest.approx(56.0)
    assert row.as_dict()["position_delta"] == pytest.approx(56.0)


def test_greeks_falls_back_when_model_is_empty():
    ticker = StubTicker(
        contract=option_contract(),
        modelGreeks=StubGreeks(),  # present but unpopulated, as on a cold feed
        lastGreeks=StubGreeks(delta=-0.30, impliedVol=0.29),
    )
    row = market.greeks_from_ticker(ticker)
    assert row.source == "last"
    assert row.delta == -0.30


def test_greeks_reports_a_missing_feed_instead_of_zeros():
    row = market.greeks_from_ticker(StubTicker(contract=option_contract()))
    assert row.delta is None
    assert "no greeks" in row.error
    assert row.position_delta is None


def test_nan_becomes_none_so_the_payload_stays_valid_json():
    row = market.greeks_from_ticker(
        StubTicker(contract=option_contract(), bid=float("nan"), ask=2.0)
    )
    assert row.bid is None and row.ask == 2.0


def test_totals_scale_by_size_and_multiplier_and_count_gaps():
    priced = market.greeks_from_ticker(
        StubTicker(
            contract=option_contract(),
            modelGreeks=StubGreeks(delta=-0.25, theta=-0.03, vega=0.10, gamma=0.01),
        ),
        quantity=-4,
    )
    unpriced = market.greeks_from_ticker(StubTicker(contract=option_contract()), quantity=-1)
    totals = market.totals([priced, unpriced])
    assert totals == {
        "contracts": 2,
        "priced": 1,
        "missing_greeks": 1,
        "delta": pytest.approx(100.0),
        "gamma": pytest.approx(-4.0),
        "theta": pytest.approx(12.0),
        "vega": pytest.approx(-40.0),
    }


# --- position -> contract --------------------------------------------------


def held_option() -> PositionRow:
    return PositionRow(
        account="U1",
        con_id=778899,
        symbol="GDX   260918P00045000",
        sec_type="OPT",
        exchange="SMART",
        currency="USD",
        quantity=-2,
        avg_cost=310.0,
        underlying="GDX",
        expiry="2026-09-18",
        strike=45.0,
        right="P",
        multiplier=100,
    )


def test_contract_from_row_prefers_the_conid():
    contract = market.contract_from_row(held_option())
    assert contract.conId == 778899


def test_contract_from_row_rebuilds_an_option_without_a_conid():
    row = held_option()
    row.con_id = 0
    contract = market.contract_from_row(row)
    assert contract.secType == "OPT"
    assert contract.lastTradeDateOrContractMonth == "20260918"
    assert contract.strike == 45.0
    assert contract.right == "P"
    assert contract.multiplier == "100"


def test_option_rows_keeps_only_options():
    stock = PositionRow(
        account="U1",
        con_id=1,
        symbol="GDX",
        sec_type="STK",
        exchange="SMART",
        currency="USD",
        quantity=100,
        avg_cost=40.0,
        underlying="GDX",
    )
    assert [r.sec_type for r in market.option_rows([stock, held_option()])] == ["OPT"]


# --- rendering -------------------------------------------------------------


def test_renderers_produce_a_header_and_survive_error_rows():
    resolved = [
        market.resolved_from_details(StubDetails(contract=option_contract())),
        market.Resolved(symbol="NOPE", error="not found"),
    ]
    text = market.render_resolved(resolved)
    assert "CONID" in text and "not found" in text

    chains = market.render_chains(
        [market.chain_from_params(StubChain(expirations={"20260918"}, strikes={45.0}), "GDX")]
    )
    assert "expirations" in chains and "2026-09-18" in chains

    greeks = market.render_greeks(
        [market.greeks_from_ticker(StubTicker(contract=option_contract()), quantity=-2)]
    )
    assert "DELTA" in greeks
