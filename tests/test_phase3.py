"""Tests for the phase-3 payloads and their CLI wiring.

Two contracts are checked here. First, the payload shapes other projects will
parse (`docs/OH-INTEGRATION-PLAN.md` §3), built without a Gateway. Second, that
the new commands refuse impossible requests *before* dialling IB, so a caller
mistake surfaces as `NoData` rather than a connection error.
"""

from __future__ import annotations

import datetime as dt

import pytest

from ib_agent import api, cli, gateway, market, store
from ib_agent.activity import ExecutionRow, OrderRow
from ib_agent.config import load_settings
from ib_agent.contract import EXIT_NO_DATA, SCHEMA_VERSION, NoData
from ib_agent.gateway import GatewayStatus
from ib_agent.portfolio import AccountValue, GatewayUnavailable, PositionRow, Snapshot


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("IB_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("IB_DB_PATH", str(tmp_path / "test.sqlite3"))
    monkeypatch.setenv("IB_AUTO_START_GATEWAY", "false")
    monkeypatch.setattr(
        gateway,
        "status",
        lambda settings: GatewayStatus(
            host="127.0.0.1", port=4001, listening=False, process_running=False
        ),
    )
    return load_settings()


def held_option(con_id: int = 778899, underlying: str = "GDX") -> PositionRow:
    return PositionRow(
        account="U1",
        con_id=con_id,
        symbol=f"{underlying}   260918P00045000",
        sec_type="OPT",
        exchange="SMART",
        currency="USD",
        quantity=-2,
        avg_cost=310.0,
        underlying=underlying,
        expiry="2026-09-18",
        strike=45.0,
        right="P",
        multiplier=100,
        asset_class="ETF",
    )


def store_snapshot(settings, rows=None) -> None:
    conn = store.connect(settings.db_path)
    store.save(
        conn,
        Snapshot(
            taken_at=dt.datetime(2026, 8, 17, 3, 30, tzinfo=dt.UTC),
            accounts=["U1"],
            positions=list(rows if rows is not None else [held_option()]),
            values=[AccountValue(tag="NetLiquidation", value="1000", currency="USD", account="U1")],
        ),
    )
    conn.close()


# --- guards before the socket ----------------------------------------------


def test_resolve_refuses_an_empty_request(isolated_settings):
    with pytest.raises(NoData):
        api.resolve(isolated_settings, [])


def test_chain_refuses_a_missing_symbol(isolated_settings):
    with pytest.raises(NoData):
        api.chain(isolated_settings, "")


def test_greeks_refuses_an_empty_spec_list(isolated_settings):
    with pytest.raises(NoData):
        api.greeks_for_specs(isolated_settings, [])


def test_greeks_from_positions_reports_no_data_when_nothing_is_held(isolated_settings):
    """An equities-only book has nothing to price; that is NoData, not a failure."""
    store_snapshot(
        isolated_settings,
        rows=[
            PositionRow(
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
        ],
    )
    with pytest.raises(NoData):
        api.greeks_for_positions(isolated_settings, use_stored=True)


def test_live_reads_still_raise_gateway_unavailable(isolated_settings):
    """The port is shut in this fixture, so every live read must say so."""
    store_snapshot(isolated_settings)
    for call in (
        lambda: api.resolve(isolated_settings, ["GDX"]),
        lambda: api.chain(isolated_settings, "GDX"),
        lambda: api.orders(isolated_settings),
        lambda: api.executions(isolated_settings),
        lambda: api.greeks_for_positions(isolated_settings, use_stored=True),
    ):
        with pytest.raises(GatewayUnavailable):
            call()


# --- payload shapes --------------------------------------------------------


def test_resolve_payload_counts_failures_separately():
    items = [
        market.Resolved(symbol="GDX", con_id=257793, sec_type="STK", asset_class="ETF"),
        market.Resolved(symbol="NOPE", error="not found"),
    ]
    payload = api.resolve_payload(items, meta={"source": "snapshot"})
    assert (payload["count"], payload["resolved"], payload["failed"]) == (2, 1, 1)
    assert payload["source"] == "snapshot"
    assert len(payload["contracts"]) == 2


def test_chain_payload_narrows_expirations_and_strikes():
    chain = market.ChainParams(
        underlying_symbol="GDX",
        underlying_con_id=257793,
        exchange="SMART",
        trading_class="GDX",
        multiplier=100.0,
        expirations=["2026-09-18", "2026-10-16", "2026-11-20"],
        strikes=[35.0, 40.0, 45.0, 50.0],
    )
    payload = api.chain_payload(
        "gdx", [chain], expiry_prefix="2026-10", strike_min=40.0, strike_max=45.0
    )
    assert payload["underlying"] == "GDX"
    got = payload["chains"][0]
    assert got["expirations"] == ["2026-10-16"]
    assert got["strikes"] == [40.0, 45.0]
    assert (got["expiration_count"], got["strike_count"]) == (1, 2)


def test_chain_payload_without_filters_keeps_everything():
    chain = market.ChainParams(
        underlying_symbol="GDX",
        underlying_con_id=1,
        exchange="SMART",
        trading_class="GDX",
        multiplier=100.0,
        expirations=["2026-09-18"],
        strikes=[45.0],
    )
    payload = api.chain_payload("GDX", [chain])
    assert payload["chains"][0]["expirations"] == ["2026-09-18"]
    assert payload["filters"] == {"expiry_prefix": "", "strike_min": None, "strike_max": None}


def test_greeks_payload_reports_market_data_type_and_totals(isolated_settings):
    row = market.GreekRow(
        con_id=778899,
        symbol="GDX   260918P00045000",
        underlying="GDX",
        expiry="2026-09-18",
        strike=45.0,
        right="P",
        multiplier=100.0,
        quantity=-2,
        delta=-0.28,
        source="model",
    )
    payload = api.greeks_payload(isolated_settings, [row], meta={"source": "live"})
    assert payload["market_data_type"] == "delayed"  # IB_MARKET_DATA_TYPE default
    assert payload["count"] == 1
    assert payload["totals"]["priced"] == 1
    assert payload["greeks"][0]["position_delta"] == pytest.approx(56.0)


def test_orders_payload_explains_an_empty_list(isolated_settings):
    """Empty is ambiguous without the master-client-id caveat, so state it."""
    payload = api.orders_payload(isolated_settings, [])
    assert payload["count"] == 0
    assert "OverrideTwsMasterClientID" in payload["master_client_id_hint"]


def test_orders_payload_carries_rows_and_totals(isolated_settings):
    row = OrderRow(
        order_id=41,
        perm_id=900001,
        client_id=17,
        account="U1",
        con_id=778899,
        symbol="GDX   260918P00045000",
        underlying="GDX",
        sec_type="OPT",
        currency="USD",
        action="SELL",
        quantity=2,
        order_type="LMT",
        limit_price=1.75,
        status="Submitted",
    )
    payload = api.orders_payload(isolated_settings, [row])
    assert payload["totals"] == {"count": 1, "active": 1, "buy": 0, "sell": 1, "options": 1}
    assert payload["orders"][0]["is_active"] is True


def test_executions_payload_states_its_window():
    """`reqExecutions` is today-only; a consumer must not assume otherwise."""
    row = ExecutionRow(
        exec_id="0001.abc",
        time="2026-08-17T13:45:00+00:00",
        account="U1",
        con_id=778899,
        symbol="GDX   260918P00045000",
        underlying="GDX",
        sec_type="OPT",
        currency="USD",
        side="SLD",
        shares=2,
        price=1.55,
        multiplier=100.0,
        commission=1.30,
        realized_pnl=42.0,
    )
    payload = api.executions_payload([row], symbol="GDX", side="")
    assert payload["window"] == "today"
    assert payload["filters"] == {"symbol": "GDX"}  # empty filters omitted
    assert payload["executions"][0]["proceeds"] == pytest.approx(310.0)
    assert payload["totals"]["realized_pnl"] == pytest.approx(42.0)


def test_every_new_payload_is_json_serialisable(isolated_settings):
    """`emit_json` must never hit a NaN or a stray dataclass."""
    import json

    payloads = [
        api.resolve_payload([market.Resolved(symbol="GDX", con_id=1)]),
        api.chain_payload("GDX", []),
        api.greeks_payload(isolated_settings, []),
        api.orders_payload(isolated_settings, []),
        api.executions_payload([]),
    ]
    for payload in payloads:
        text = json.dumps({"schema": SCHEMA_VERSION, **payload}, sort_keys=True, default=str)
        assert "NaN" not in text


# --- CLI wiring ------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["resolve", "GDX"],
        ["resolve", "--from-positions", "--stored"],
        ["chain", "GDX", "-e", "2026-09"],
        ["greeks", "--stored", "--right", "put"],
        ["greeks", "GDX 2026-09-18 P 45"],
        ["orders", "--json"],
        ["executions", "--symbol", "GDX"],
        ["fills"],
    ],
)
def test_new_commands_parse_and_dispatch(argv):
    args = cli.build_parser().parse_args(argv)
    assert args.command in cli.HANDLERS


def test_fills_is_an_alias_of_executions():
    assert cli.HANDLERS["fills"] is cli.HANDLERS["executions"]


def test_gateway_down_exit_code_is_reported_not_raised(isolated_settings, capsys):
    """The CLI translates a shut port into EXIT_GATEWAY, never a traceback."""
    from ib_agent.contract import EXIT_GATEWAY

    assert cli.main(["orders"]) == EXIT_GATEWAY
    assert "gateway not reachable" in capsys.readouterr().err


def test_empty_resolve_request_exits_no_data(isolated_settings, capsys):
    assert cli.main(["resolve"]) == EXIT_NO_DATA
    assert "nothing to resolve" in capsys.readouterr().err
