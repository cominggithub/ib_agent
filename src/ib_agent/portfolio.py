"""Read-only portfolio snapshots from the IB Gateway API.

A snapshot combines three IB requests:
  * portfolio items  -> per-position market value / unrealised PnL
  * positions        -> fallback when account updates have not arrived yet
  * account summary  -> NetLiquidation, cash, buying power, ...
"""

from __future__ import annotations

import asyncio
import datetime as dt
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator, Iterable, Mapping

from ib_async import IB, Stock

from .config import Settings

# Account summary tags worth persisting for a long-term investment tracker.
# The margin and RegT tags are what a consumer needs to reason about headroom;
# per-position margin is deliberately absent, since deriving it requires a
# what-if order and `ReadOnlyApi=yes` rejects order messages by design.
SUMMARY_TAGS = (
    "NetLiquidation",
    "TotalCashValue",
    "SettledCash",
    "AccruedCash",
    "GrossPositionValue",
    "AvailableFunds",
    "ExcessLiquidity",
    "BuyingPower",
    "UnrealizedPnL",
    "RealizedPnL",
    "Cushion",
    "FullInitMarginReq",
    "FullMaintMarginReq",
    "RegTEquity",
    "RegTMargin",
    "Leverage",
)

# Account ids IBKR uses for roll-up rows rather than a real account.
AGGREGATE_ACCOUNTS = frozenset({"", "All"})


class GatewayUnavailable(RuntimeError):
    """Raised when the Gateway API port cannot be reached."""


@dataclass
class PositionRow:
    account: str
    con_id: int
    symbol: str
    sec_type: str
    exchange: str
    currency: str
    quantity: float
    avg_cost: float
    market_price: float | None = None
    market_value: float | None = None
    unrealized_pnl: float | None = None
    realized_pnl: float | None = None
    # Derivative metadata; empty/None for plain stock positions.
    underlying: str = ""
    expiry: str = ""  # ISO date, e.g. "2026-09-04"
    strike: float | None = None
    right: str = ""  # "C" | "P"
    multiplier: float | None = None
    # "ETF" / "COMMON" / ... for the underlying, filled from the instrument cache
    asset_class: str = ""

    @property
    def cost_basis(self) -> float:
        return self.quantity * self.avg_cost

    @property
    def is_option(self) -> bool:
        return self.sec_type == "OPT"

    @property
    def is_etf(self) -> bool:
        return self.asset_class.upper() == "ETF"

    @property
    def expiry_date(self) -> dt.date | None:
        try:
            return dt.date.fromisoformat(self.expiry)
        except ValueError:
            return None

    def days_to_expiry(self, today: dt.date | None = None) -> int | None:
        exp = self.expiry_date
        if exp is None:
            return None
        return (exp - (today or dt.date.today())).days

    @property
    def side(self) -> str:
        """Long/short, which for short options is the interesting bit."""
        if self.quantity > 0:
            return "long"
        if self.quantity < 0:
            return "short"
        return "flat"


@dataclass
class AccountValue:
    tag: str
    value: str
    currency: str
    account: str = ""


@dataclass
class Instrument:
    """Static reference data for an underlying, cached between syncs."""

    symbol: str
    currency: str
    asset_class: str = ""  # IB stockType: ETF, COMMON, ADR, ...
    long_name: str = ""
    industry: str = ""
    category: str = ""

    @property
    def is_etf(self) -> bool:
        return self.asset_class.upper() == "ETF"


@dataclass
class Snapshot:
    taken_at: dt.datetime
    accounts: list[str]
    positions: list[PositionRow] = field(default_factory=list)
    values: list[AccountValue] = field(default_factory=list)
    instruments: list[Instrument] = field(default_factory=list)
    source: str = "tws-api"

    def value_of(self, tag: str, currency: str | None = None) -> float | None:
        """Numeric value for a tag.

        IB reports some tags per currency plus a "BASE" roll-up. With no
        currency given, prefer a concrete currency row over BASE.
        """
        matches = [v for v in self.values if v.tag == tag]
        if currency is not None:
            matches = [v for v in matches if v.currency == currency]
        else:
            matches.sort(key=lambda v: v.currency in ("", "BASE"))
        for item in matches:
            try:
                return float(item.value)
            except ValueError:
                continue
        return None

    @property
    def net_liquidation(self) -> float | None:
        return self.value_of("NetLiquidation")


@contextmanager
def connected(settings: Settings) -> Iterator[IB]:
    """Connect to an already-running Gateway; always disconnect afterwards."""
    ib = IB()
    try:
        ib.connect(
            host=settings.host,
            port=settings.port,
            clientId=settings.client_id,
            timeout=settings.connect_timeout,
            readonly=settings.readonly,
            account=settings.account,
        )
    except (OSError, TimeoutError, asyncio.TimeoutError) as exc:
        raise GatewayUnavailable(
            f"cannot reach IB Gateway at {settings.host}:{settings.port} ({exc.__class__.__name__}: {exc}). "
            "Start it with: scripts/gateway-up.sh"
        ) from exc
    try:
        yield ib
    finally:
        if ib.isConnected():
            ib.disconnect()


def merge_account_values(raw: Iterable[Any]) -> list[AccountValue]:
    """Filter and de-duplicate raw IB account summary rows.

    IBKR repeats values: once under the pseudo-account "All" (or a blank
    account) and once under the real account id. Keep the last value per
    (account, tag, currency), then drop aggregate rows that a real account
    already reports.
    """
    summary: dict[tuple[str, str, str], AccountValue] = {}
    for v in raw:
        if v.tag not in SUMMARY_TAGS:
            continue
        summary[(v.account, v.tag, v.currency)] = AccountValue(
            tag=v.tag, value=v.value, currency=v.currency, account=v.account
        )
    attributed = {
        (v.tag, v.currency)
        for v in summary.values()
        if v.account not in AGGREGATE_ACCOUNTS
    }
    return sorted(
        (
            v
            for v in summary.values()
            if v.account not in AGGREGATE_ACCOUNTS
            or (v.tag, v.currency) not in attributed
        ),
        key=lambda v: (v.account, v.tag, v.currency),
    )


def lookup_instruments(ib: IB, symbols: Iterable[tuple[str, str]]) -> list[Instrument]:
    """Ask IB what each underlying is (ETF vs common stock, name, industry).

    Reference data only, no market data subscription required. Failures are
    skipped rather than fatal: classification is a nice-to-have.
    """
    out: list[Instrument] = []
    for symbol, currency in symbols:
        if not symbol:
            continue
        try:
            details = ib.reqContractDetails(Stock(symbol, "SMART", currency or "USD"))
        except Exception:  # noqa: BLE001 - reference data must never break a sync
            continue
        if not details:
            out.append(Instrument(symbol=symbol, currency=currency, asset_class="UNKNOWN"))
            continue
        cd = details[0]
        out.append(
            Instrument(
                symbol=symbol,
                currency=currency,
                asset_class=(cd.stockType or "").upper(),
                long_name=cd.longName or "",
                industry=cd.industry or "",
                category=cd.category or "",
            )
        )
    return out


def normalize_expiry(raw: str) -> str:
    """IB gives 'YYYYMMDD' (or 'YYYYMM' for futures-style months) -> ISO date."""
    raw = (raw or "").strip()
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"
    if len(raw) == 6 and raw.isdigit():
        return f"{raw[0:4]}-{raw[4:6]}"
    return raw


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def row_from_contract(account: str, contract: Any, quantity: float, avg_cost: float) -> PositionRow:
    """Build a PositionRow from an IB contract plus size/cost."""
    return PositionRow(
        account=account,
        con_id=contract.conId,
        symbol=contract.localSymbol or contract.symbol,
        sec_type=contract.secType,
        exchange=contract.primaryExchange or contract.exchange or "",
        currency=contract.currency,
        quantity=float(quantity),
        avg_cost=float(avg_cost),
        underlying=contract.symbol or "",
        expiry=normalize_expiry(getattr(contract, "lastTradeDateOrContractMonth", "")),
        strike=_to_float(getattr(contract, "strike", None)) or None,
        right=(getattr(contract, "right", "") or "")[:1].upper(),
        multiplier=_to_float(getattr(contract, "multiplier", None)),
    )


def fetch_snapshot(
    settings: Settings,
    settle_seconds: float = 2.0,
    instrument_cache: Mapping[str, str] | None = None,
) -> Snapshot:
    """Pull a full portfolio snapshot from the Gateway.

    `instrument_cache` maps underlying symbol -> asset class already known from
    previous runs; only unseen underlyings cost an extra reference-data call.
    """
    cache = dict(instrument_cache or {})
    with connected(settings) as ib:
        # Give the account-update stream a moment; portfolio() is populated
        # from it and can be briefly empty right after connecting.
        ib.sleep(settle_seconds)

        accounts = [a for a in ib.managedAccounts() if a]
        wanted = settings.account or ""

        rows: dict[tuple[str, int], PositionRow] = {}
        for pos in ib.positions(account=wanted):
            row = row_from_contract(pos.account, pos.contract, pos.position, pos.avgCost)
            rows[(row.account, row.con_id)] = row

        for item in ib.portfolio(account=wanted):
            key = (item.account, item.contract.conId)
            row = rows.get(key)
            if row is None:
                row = row_from_contract(
                    item.account, item.contract, item.position, item.averageCost
                )
                rows[key] = row
            row.market_price = float(item.marketPrice)
            row.market_value = float(item.marketValue)
            row.unrealized_pnl = float(item.unrealizedPNL)
            row.realized_pnl = float(item.realizedPNL)

        values = merge_account_values(ib.accountSummary(account=wanted))

        # Classify underlyings we have not seen before, then label every row.
        unknown = sorted(
            {
                (r.underlying, r.currency)
                for r in rows.values()
                if r.underlying and r.underlying not in cache
            }
        )
        instruments = lookup_instruments(ib, unknown) if unknown else []
        cache.update({i.symbol: i.asset_class for i in instruments})
        for row in rows.values():
            row.asset_class = cache.get(row.underlying, "")

        return Snapshot(
            taken_at=dt.datetime.now(dt.UTC),
            accounts=accounts,
            positions=sorted(rows.values(), key=lambda r: (r.account, r.symbol)),
            values=values,
            instruments=instruments,
        )
