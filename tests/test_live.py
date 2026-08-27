"""The live acceptance checks for phase 3 — skipped unless explicitly enabled.

The rule that no test may need a Gateway still holds: every test here skips
unless `IB_LIVE_TESTS=1` *and* the API port answers, so `uv run pytest` stays
offline and green on a machine with no IB session.

Run them during a live window with:

    IB_LIVE_TESTS=1 uv run pytest tests/test_live.py -v -s

They answer the three questions `docs/OH-INTEGRATION-PLAN.md` §3 leaves open,
which no amount of stubbing can settle:

1. does the delayed feed carry `modelGreeks`, or are real-time options data a
   per-username purchase (§7.1);
2. does `resolve --from-positions` reproduce the conid corrections OH pins by
   hand (`tests/fixtures/underlying_conids.json`);
3. does `orders` see orders placed outside this client id, i.e. is
   `OverrideTwsMasterClientID` actually needed.

A skip is not a pass. Until this file has been run against a live Gateway, phase
3 stays "built, unverified".
"""

from __future__ import annotations

import json
import os
import pathlib

import pytest

from ib_agent import api, gateway, market
from ib_agent.config import load_settings

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def load_expected() -> dict[str, dict[str, int]]:
    data = json.loads((FIXTURES / "underlying_conids.json").read_text())
    return {"pinned": data["pinned"], "mirrored": data["mirrored"]}


@pytest.fixture(scope="module")
def live_settings():
    if os.environ.get("IB_LIVE_TESTS") != "1":
        pytest.skip("live tests are opt-in: set IB_LIVE_TESTS=1")
    settings = load_settings()
    if not gateway.status(settings).ready:
        pytest.skip(f"gateway not listening on {settings.host}:{settings.port}")
    return settings


@pytest.fixture(scope="module")
def held_options(live_settings):
    result = api.positions(live_settings)
    options = market.option_rows(result.rows)
    if not options:
        pytest.skip("no option positions in the account")
    return options


# --- 1. greeks on whatever feed this username has --------------------------


def test_every_held_option_gets_a_delta(live_settings, held_options):
    """Phase 3 acceptance criterion, and the §7.1 subscription question.

    Prints the per-contract source so a partial result is diagnosable: if rows
    come back with `source == ""` and an error, the delayed feed is not enough
    and options market data is a per-username purchase.
    """
    rows, _meta = api.greeks_for_positions(live_settings)
    missing = [r for r in rows if r.delta is None]
    sources = sorted({r.source for r in rows if r.source})

    print(f"\ncontracts={len(rows)} priced={len(rows) - len(missing)} sources={sources}")
    for r in missing[:10]:
        print(f"  no greeks: {r.symbol} ({r.error})")

    assert rows, "greeks returned no rows for a book that holds options"
    assert not missing, (
        f"{len(missing)}/{len(rows)} contracts returned no greeks on market data type "
        f"{live_settings.market_data_type}; see docs/OH-INTEGRATION-PLAN.md §7.1"
    )


def test_book_totals_are_populated(live_settings):
    rows, _meta = api.greeks_for_positions(live_settings)
    totals = market.totals(rows)
    print(f"\nbook totals: {totals}")
    assert totals["delta"] is not None
    assert totals["theta"] is not None


# --- 2. the conid corrections OH pins by hand ------------------------------


def test_resolve_from_positions_reproduces_the_pinned_conids(live_settings):
    """If this passes, OH's pin registry becomes redundant (its whole point)."""
    expected = load_expected()
    items, meta = api.resolve_positions(live_settings, options_only=True)
    print(f"\nresolved {len(items)} option positions from {meta.get('source')}")

    # IB may list several option series per underlying; collapse to one conid.
    got: dict[str, set[int]] = {}
    for item in items:
        symbol = (item.underlying_symbol or item.symbol.split()[0]).upper()
        if item.underlying_con_id:
            got.setdefault(symbol, set()).add(item.underlying_con_id)

    print("resolved underlying conids:")
    for symbol in sorted(got):
        print(f"  {symbol:<6} {sorted(got[symbol])}")

    checked = 0
    mismatches: list[str] = []
    for symbol, conid in expected["pinned"].items():
        if symbol not in got:
            continue  # not held right now; nothing to compare
        checked += 1
        if conid not in got[symbol]:
            mismatches.append(f"{symbol}: OH pins {conid}, IB says {sorted(got[symbol])}")

    for symbol, conid in expected["mirrored"].items():
        if symbol in got and conid not in got[symbol]:
            print(f"  note: {symbol} mirrored conid {conid} != IB {sorted(got[symbol])}")

    if not checked:
        pytest.skip("none of the pinned underlyings are currently held")
    assert not mismatches, "\n".join(mismatches)


def test_every_resolved_option_reports_an_underlying_conid(live_settings, held_options):
    """The consumer keys on this field, so a null is a defect, not a gap."""
    items, _meta = api.resolve_positions(live_settings, options_only=True)
    without = [i.symbol for i in items if not i.underlying_con_id and not i.error]
    print(f"\n{len(items)} resolved, {len(without)} without an underlying conid")
    assert not without, f"no underlying_con_id for: {without[:10]}"


# --- 3. orders visibility --------------------------------------------------


def test_orders_read_is_answered_not_refused(live_settings):
    """Read-only must still be able to *read*.

    An empty list is a legitimate answer, so this asserts the call succeeds and
    prints what came back; the master-client-id question is answered by placing
    a working order on mobile and re-running this.
    """
    rows = api.orders(live_settings)
    print(f"\n{len(rows)} working order(s)")
    for r in rows[:10]:
        print(f"  {r.symbol} {r.action} {r.quantity} {r.order_type} {r.status} client={r.client_id}")
    assert isinstance(rows, list)


def test_executions_read_is_answered(live_settings):
    rows = api.executions(live_settings)
    print(f"\n{len(rows)} fill(s) today")
    assert isinstance(rows, list)


# --- chain, on a name that is definitely optionable ------------------------


def test_chain_returns_expirations_and_strikes(live_settings):
    chains = api.chain(live_settings, "GDX", exchange="SMART")
    assert chains, "no option parameters for GDX"
    first = chains[0]
    print(
        f"\nGDX @ {first.exchange}: {len(first.expirations)} expirations, "
        f"{len(first.strikes)} strikes, first={first.expirations[:3]}"
    )
    assert first.expirations and first.strikes
    assert all(len(e) == 10 and e[4] == "-" for e in first.expirations), "expiries not ISO"
