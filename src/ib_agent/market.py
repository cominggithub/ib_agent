"""Contract reference data and option analytics: `resolve`, `chain`, `greeks`.

All three are reads the TWS socket API does better than the Client Portal
endpoints option_harvester scrapes today (docs/OH-INTEGRATION-PLAN.md §3):

* `resolve`  - symbol -> conid, and an option's conid -> its *underlying* conid,
  which removes the hand-maintained pin table on the consumer side;
* `chain`    - expirations and strikes from `reqSecDefOptParams`, one call per
  underlying instead of a walk over `secdef/*`;
* `greeks`   - `Ticker.modelGreeks`, IB's own model output, rather than a
  snapshot of five separate tick fields.

Structure follows the rest of the package: pure mapper functions that turn IB
objects into dataclasses, plus a thin `fetch_*` layer that opens a connection.
The mappers are what the tests exercise, so none of this needs a Gateway to
verify.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from ib_async import Contract, Option, Stock

from .config import Settings
from .portfolio import PositionRow, connected, normalize_expiry

# Fields IB fills only for derivatives; kept out of the stock payload entirely
# rather than reported as nulls, so a consumer can tell "not an option" from
# "unknown".
OPTION_FIELDS = ("expiry", "strike", "right", "multiplier")


def _clean(value: Any) -> float | None:
    """IB uses NaN for "no value"; JSON has no NaN, so normalise to None."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) else number


def _int_or_none(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number or None


@dataclass
class Resolved:
    """One contract IB recognised, with the ids a consumer needs to key on."""

    symbol: str
    con_id: int | None = None
    sec_type: str = ""
    currency: str = ""
    exchange: str = ""
    primary_exchange: str = ""
    local_symbol: str = ""
    trading_class: str = ""
    long_name: str = ""
    asset_class: str = ""  # IB stockType: ETF / COMMON / ADR
    industry: str = ""
    category: str = ""
    min_tick: float | None = None
    # Derivatives only.
    expiry: str = ""
    strike: float | None = None
    right: str = ""
    multiplier: float | None = None
    # For an option: the underlying's conid, straight from IB.
    underlying_symbol: str = ""
    underlying_con_id: int | None = None
    underlying_sec_type: str = ""
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        data = {k: v for k, v in vars(self).items()}
        if self.sec_type != "OPT":
            for key in OPTION_FIELDS:
                data.pop(key, None)
        return data


@dataclass
class ChainParams:
    """`reqSecDefOptParams` output for one exchange/trading class."""

    underlying_symbol: str
    underlying_con_id: int | None
    exchange: str
    trading_class: str
    multiplier: float | None
    expirations: list[str] = field(default_factory=list)  # ISO dates
    strikes: list[float] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            **vars(self),
            "expiration_count": len(self.expirations),
            "strike_count": len(self.strikes),
        }


@dataclass
class GreekRow:
    """Model greeks for one option contract."""

    con_id: int | None
    symbol: str  # IB local symbol, e.g. "GDX   260918P00045000"
    underlying: str
    sec_type: str = "OPT"
    currency: str = ""
    expiry: str = ""
    strike: float | None = None
    right: str = ""
    multiplier: float | None = None
    quantity: float | None = None  # filled when the row came from a position
    implied_vol: float | None = None
    delta: float | None = None
    gamma: float | None = None
    vega: float | None = None
    theta: float | None = None
    option_price: float | None = None
    underlying_price: float | None = None
    pv_dividend: float | None = None
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    source: str = ""  # which greek set IB gave us: model / last / bid / ask
    error: str = ""

    @property
    def position_delta(self) -> float | None:
        """Delta scaled by size and multiplier — the number a book cares about."""
        if self.delta is None or self.quantity is None:
            return None
        return self.delta * self.quantity * (self.multiplier or 1.0)

    def as_dict(self) -> dict[str, Any]:
        data = dict(vars(self))
        pd = self.position_delta
        data["position_delta"] = None if pd is None else round(pd, 4)
        return data


# --- mappers ---------------------------------------------------------------


def resolved_from_details(details: Any) -> Resolved:
    """ContractDetails -> Resolved."""
    c = details.contract
    return Resolved(
        symbol=getattr(c, "symbol", "") or "",
        con_id=_int_or_none(getattr(c, "conId", None)),
        sec_type=getattr(c, "secType", "") or "",
        currency=getattr(c, "currency", "") or "",
        exchange=getattr(c, "exchange", "") or "",
        primary_exchange=getattr(c, "primaryExchange", "") or "",
        local_symbol=getattr(c, "localSymbol", "") or "",
        trading_class=getattr(c, "tradingClass", "") or "",
        long_name=getattr(details, "longName", "") or "",
        asset_class=(getattr(details, "stockType", "") or "").upper(),
        industry=getattr(details, "industry", "") or "",
        category=getattr(details, "category", "") or "",
        min_tick=_clean(getattr(details, "minTick", None)),
        expiry=normalize_expiry(getattr(c, "lastTradeDateOrContractMonth", "") or ""),
        strike=_clean(getattr(c, "strike", None)) or None,
        right=(getattr(c, "right", "") or "")[:1].upper(),
        multiplier=_clean(getattr(c, "multiplier", None)),
        underlying_symbol=getattr(details, "underSymbol", "") or "",
        underlying_con_id=_int_or_none(getattr(details, "underConId", None)),
        underlying_sec_type=getattr(details, "underSecType", "") or "",
    )


def chain_from_params(params: Any, underlying_symbol: str = "") -> ChainParams:
    """OptionChain -> ChainParams, with IB's YYYYMMDD expirations normalised."""
    return ChainParams(
        underlying_symbol=underlying_symbol,
        underlying_con_id=_int_or_none(getattr(params, "underlyingConId", None)),
        exchange=getattr(params, "exchange", "") or "",
        trading_class=getattr(params, "tradingClass", "") or "",
        multiplier=_clean(getattr(params, "multiplier", None)),
        expirations=sorted(
            normalize_expiry(e) for e in (getattr(params, "expirations", None) or [])
        ),
        strikes=sorted(
            v for v in (_clean(s) for s in (getattr(params, "strikes", None) or [])) if v
        ),
    )


def greeks_from_ticker(ticker: Any, quantity: float | None = None) -> GreekRow:
    """Ticker -> GreekRow, preferring model greeks over the tick-derived sets.

    IB populates `modelGreeks` from its own pricing model and the other three
    only when a trade or quote arrived. Delayed feeds often carry `modelGreeks`
    alone, which is why it is tried first.
    """
    c = getattr(ticker, "contract", None)
    row = GreekRow(
        con_id=_int_or_none(getattr(c, "conId", None)),
        symbol=(getattr(c, "localSymbol", "") or getattr(c, "symbol", "") or ""),
        underlying=getattr(c, "symbol", "") or "",
        sec_type=getattr(c, "secType", "OPT") or "OPT",
        currency=getattr(c, "currency", "") or "",
        expiry=normalize_expiry(getattr(c, "lastTradeDateOrContractMonth", "") or ""),
        strike=_clean(getattr(c, "strike", None)) or None,
        right=(getattr(c, "right", "") or "")[:1].upper(),
        multiplier=_clean(getattr(c, "multiplier", None)),
        quantity=quantity,
        bid=_clean(getattr(ticker, "bid", None)),
        ask=_clean(getattr(ticker, "ask", None)),
        last=_clean(getattr(ticker, "last", None)),
    )

    for name in ("modelGreeks", "lastGreeks", "bidGreeks", "askGreeks"):
        comp = getattr(ticker, name, None)
        if comp is None:
            continue
        delta = _clean(getattr(comp, "delta", None))
        iv = _clean(getattr(comp, "impliedVol", None))
        if delta is None and iv is None:
            continue  # present but empty, e.g. before the model has run
        row.source = name.replace("Greeks", "")
        row.implied_vol = iv
        row.delta = delta
        row.gamma = _clean(getattr(comp, "gamma", None))
        row.vega = _clean(getattr(comp, "vega", None))
        row.theta = _clean(getattr(comp, "theta", None))
        row.option_price = _clean(getattr(comp, "optPrice", None))
        row.underlying_price = _clean(getattr(comp, "undPrice", None))
        row.pv_dividend = _clean(getattr(comp, "pvDividend", None))
        return row

    row.error = "no greeks returned (no option market data for this contract)"
    return row


# --- contract construction -------------------------------------------------


def contract_from_row(row: PositionRow) -> Contract:
    """Rebuild a tradable contract from a stored position row.

    Keyed by conid when we have one — the only identifier IB never
    misinterprets — and reconstructed from the option's parts otherwise, which
    is what a row read back from sqlite needs.
    """
    if row.con_id:
        return Contract(conId=row.con_id, exchange="SMART")
    if row.is_option:
        return Option(
            symbol=row.underlying or row.symbol,
            lastTradeDateOrContractMonth=(row.expiry or "").replace("-", ""),
            strike=row.strike or 0.0,
            right=row.right or "C",
            exchange="SMART",
            currency=row.currency or "USD",
            multiplier=str(int(row.multiplier)) if row.multiplier else "100",
        )
    return Stock(row.underlying or row.symbol, "SMART", row.currency or "USD")


def parse_spec(spec: str, currency: str = "USD") -> Contract:
    """Parse a CLI contract spec into an IB contract.

    Accepted, in order of precedence:

      ``12345``                  a bare conid
      ``GDX``                    a stock
      ``GDX:STK``                an explicit sec type
      ``GDX 2026-09-18 P 45``    an option (whitespace or `/`-separated)
    """
    text = (spec or "").strip()
    if not text:
        raise ValueError("empty contract spec")
    if text.isdigit():
        return Contract(conId=int(text), exchange="SMART")

    parts = [p for p in text.replace("/", " ").split() if p]
    if len(parts) >= 4:
        symbol, expiry, right, strike = parts[0], parts[1], parts[2], parts[3]
        return Option(
            symbol=symbol.upper(),
            lastTradeDateOrContractMonth=expiry.replace("-", ""),
            strike=float(strike),
            right=right[:1].upper(),
            exchange="SMART",
            currency=currency,
            multiplier="100",
        )

    head = parts[0]
    if ":" in head:
        symbol, _, sec_type = head.partition(":")
        sec_type = sec_type.upper()
        if sec_type in ("", "STK"):
            return Stock(symbol.upper(), "SMART", currency)
        return Contract(
            secType=sec_type, symbol=symbol.upper(), exchange="SMART", currency=currency
        )
    return Stock(head.upper(), "SMART", currency)


# --- IB glue ---------------------------------------------------------------


def fetch_resolved(settings: Settings, specs: Sequence[str], currency: str = "USD") -> list[Resolved]:
    """Resolve each spec through `reqContractDetails`.

    One unresolvable spec is reported in its own row rather than failing the
    batch: a consumer resolving 40 symbols wants the 39 that worked.
    """
    if not specs:
        return []
    out: list[Resolved] = []
    with connected(settings) as ib:
        for spec in specs:
            try:
                contract = parse_spec(spec, currency)
            except ValueError as exc:
                out.append(Resolved(symbol=str(spec), error=str(exc)))
                continue
            try:
                details = ib.reqContractDetails(contract)
            except Exception as exc:  # noqa: BLE001 - one bad spec must not end the batch
                out.append(Resolved(symbol=str(spec), error=f"{exc.__class__.__name__}: {exc}"))
                continue
            if not details:
                out.append(Resolved(symbol=str(spec), error="not found"))
                continue
            out.extend(resolved_from_details(d) for d in details[:1])
    return out


def fetch_resolved_for_rows(settings: Settings, rows: Sequence[PositionRow]) -> list[Resolved]:
    """Resolve held positions, so options report their underlying conid.

    This is the replacement for the consumer-side pin table: IB states the
    relationship in `ContractDetails.underConId`, so nothing has to be guessed
    from a symbol.
    """
    if not rows:
        return []
    out: list[Resolved] = []
    with connected(settings) as ib:
        for row in rows:
            try:
                details = ib.reqContractDetails(contract_from_row(row))
            except Exception as exc:  # noqa: BLE001
                out.append(
                    Resolved(symbol=row.symbol, error=f"{exc.__class__.__name__}: {exc}")
                )
                continue
            if not details:
                out.append(Resolved(symbol=row.symbol, error="not found"))
                continue
            out.append(resolved_from_details(details[0]))
    return out


def fetch_chain(
    settings: Settings,
    symbol: str,
    *,
    sec_type: str = "STK",
    currency: str = "USD",
    exchange: str = "",
) -> list[ChainParams]:
    """Expirations and strikes for one underlying.

    `reqSecDefOptParams` needs the underlying's conid, so this qualifies the
    underlying first. IB answers per exchange/trading class; `exchange` filters
    that down (SMART is the usual pick).
    """
    symbol = (symbol or "").upper()
    if not symbol:
        return []
    with connected(settings) as ib:
        underlying: Contract = (
            Stock(symbol, "SMART", currency)
            if sec_type.upper() == "STK"
            else Contract(secType=sec_type.upper(), symbol=symbol, exchange="SMART", currency=currency)
        )
        qualified = ib.qualifyContracts(underlying)
        if not qualified or not qualified[0].conId:
            return []
        con_id = qualified[0].conId
        params = ib.reqSecDefOptParams(symbol, "", qualified[0].secType, con_id)
        chains = [chain_from_params(p, underlying_symbol=symbol) for p in params]
    if exchange:
        wanted = exchange.upper()
        chains = [c for c in chains if c.exchange.upper() == wanted]
    return sorted(chains, key=lambda c: (c.exchange, c.trading_class))


def fetch_greeks(
    settings: Settings,
    contracts: Sequence[Contract],
    *,
    quantities: Sequence[float | None] | None = None,
    market_data_type: int | None = None,
) -> list[GreekRow]:
    """Model greeks for the given option contracts, in request order."""
    if not contracts:
        return []
    sizes = list(quantities or [None] * len(contracts))
    with connected(settings) as ib:
        ib.reqMarketDataType(market_data_type or settings.market_data_type)
        qualified = ib.qualifyContracts(*contracts)
        by_id = {c.conId: c for c in qualified if getattr(c, "conId", None)}
        # Preserve the caller's order and keep each contract's size with it.
        wanted: list[Contract] = []
        size_by_id: dict[int, float | None] = {}
        for contract, size in zip(contracts, sizes):
            resolved = by_id.get(getattr(contract, "conId", None)) or next(
                (c for c in qualified if c.localSymbol and c.localSymbol == getattr(contract, "localSymbol", None)),
                None,
            )
            target = resolved or (contract if getattr(contract, "conId", None) else None)
            if target is None:
                continue
            wanted.append(target)
            size_by_id[target.conId] = size

        tickers = ib.reqTickers(*wanted) if wanted else []
        rows = [
            greeks_from_ticker(t, quantity=size_by_id.get(getattr(t.contract, "conId", None)))
            for t in tickers
        ]
    order = {getattr(c, "conId", None): i for i, c in enumerate(wanted)}
    return sorted(rows, key=lambda r: order.get(r.con_id, 10**6))


def option_rows(rows: Iterable[PositionRow]) -> list[PositionRow]:
    """The option subset of a position list, which is all `greeks` can price."""
    return [r for r in rows if r.is_option]


# --- rendering -------------------------------------------------------------


def render_resolved(items: Sequence[Resolved]) -> str:
    header = (
        f"{'SYMBOL':<24}{'TYPE':<5}{'CONID':>10}{'UND CONID':>11}  "
        f"{'CLASS':<7}{'EXCH':<9}NAME"
    )
    lines = [header, "-" * len(header)]
    for r in items:
        if r.error:
            lines.append(f"{r.symbol[:23]:<24}{'':<5}{'':>10}{'':>11}  {'':<7}{'':<9}{r.error}")
            continue
        name = r.long_name or r.underlying_symbol
        lines.append(
            f"{(r.local_symbol or r.symbol)[:23]:<24}{r.sec_type:<5}"
            f"{(r.con_id or 0):>10}{(r.underlying_con_id or 0):>11}  "
            f"{r.asset_class[:6]:<7}{(r.primary_exchange or r.exchange)[:8]:<9}{name[:40]}"
        )
    return "\n".join(lines)


def render_chains(chains: Sequence[ChainParams], *, limit: int = 12) -> str:
    out: list[str] = []
    for c in chains:
        out.append(
            f"{c.underlying_symbol} @ {c.exchange} [{c.trading_class}] x{c.multiplier or '?'}  "
            f"{len(c.expirations)} expirations, {len(c.strikes)} strikes"
        )
        out.append("  expirations: " + _preview(c.expirations, limit))
        out.append("  strikes    : " + _preview([_trim(s) for s in c.strikes], limit))
    return "\n".join(out) if out else "no option parameters returned"


def _preview(values: Sequence[Any], limit: int) -> str:
    shown = ", ".join(str(v) for v in values[:limit])
    return shown + (f", ... (+{len(values) - limit})" if len(values) > limit else "")


def _trim(value: float) -> str:
    return f"{value:g}"


def render_greeks(rows: Sequence[GreekRow]) -> str:
    header = (
        f"{'SYMBOL':<24}{'QTY':>6}{'IV':>8}{'DELTA':>8}{'GAMMA':>9}{'THETA':>9}"
        f"{'VEGA':>9}{'OPT':>9}{'UND':>10}{'POS Δ':>10}  SRC"
    )
    lines = [header, "-" * len(header)]

    def num(value: float | None, width: int, digits: int = 3) -> str:
        return "-".rjust(width) if value is None else f"{value:>{width},.{digits}f}"

    for r in rows:
        if r.error and r.delta is None:
            lines.append(f"{r.symbol[:23]:<24}{num(r.quantity, 6, 0)}  {r.error}")
            continue
        lines.append(
            f"{r.symbol[:23]:<24}{num(r.quantity, 6, 0)}{num(r.implied_vol, 8)}"
            f"{num(r.delta, 8)}{num(r.gamma, 9, 4)}{num(r.theta, 9, 4)}{num(r.vega, 9, 4)}"
            f"{num(r.option_price, 9, 2)}{num(r.underlying_price, 10, 2)}"
            f"{num(r.position_delta, 10, 1)}  {r.source}"
        )
    return "\n".join(lines)


def totals(rows: Sequence[GreekRow]) -> dict[str, Any]:
    """Book-level greek exposure: the sum a risk review actually reads."""

    def total(attr: str) -> float | None:
        values = [
            getattr(r, attr) * (r.quantity or 0) * (r.multiplier or 1.0)
            for r in rows
            if getattr(r, attr) is not None and r.quantity is not None
        ]
        return round(sum(values), 4) if values else None

    priced = [r for r in rows if r.delta is not None]
    return {
        "contracts": len(rows),
        "priced": len(priced),
        "missing_greeks": len(rows) - len(priced),
        "delta": total("delta"),
        "gamma": total("gamma"),
        "theta": total("theta"),
        "vega": total("vega"),
    }


def now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
