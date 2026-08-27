"""Watchlist quotes.

Reads snapshot quotes for a list of symbols. Market data type defaults to 3
("delayed"), which returns free delayed data when the account has no real-time
subscription for that product and real-time data when it does, so a quote never
fails just because of subscriptions.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Iterable, Sequence

from ib_async import Contract, Crypto, Forex, Future, Index, Stock

from .config import Settings
from .portfolio import connected

MARKET_DATA_TYPE_NAMES = {1: "live", 2: "frozen", 3: "delayed", 4: "delayed-frozen"}


@dataclass
class WatchEntry:
    symbol: str
    sec_type: str = "STK"
    exchange: str = "SMART"
    currency: str = "USD"
    note: str = ""


@dataclass
class Quote:
    symbol: str
    sec_type: str
    currency: str
    exchange: str = ""
    con_id: int | None = None
    last: float | None = None
    bid: float | None = None
    ask: float | None = None
    close: float | None = None
    open_: float | None = None
    high: float | None = None
    low: float | None = None
    volume: float | None = None
    market_price: float | None = None
    quote_time: str | None = None
    note: str = ""
    error: str = ""

    @property
    def change(self) -> float | None:
        if self.market_price is None or self.close is None:
            return None
        return self.market_price - self.close

    @property
    def change_pct(self) -> float | None:
        change = self.change
        if change is None or not self.close:
            return None
        return change / self.close * 100.0

    def as_dict(self) -> dict[str, object]:
        data = {k.rstrip("_"): v for k, v in vars(self).items()}
        data["change"] = None if self.change is None else round(self.change, 4)
        data["change_pct"] = None if self.change_pct is None else round(self.change_pct, 3)
        return data


def _clean(value: float | None) -> float | None:
    """IB uses NaN for "no value" (e.g. no bid/ask outside trading hours)."""
    if value is None:
        return None
    try:
        if math.isnan(value):
            return None
    except TypeError:
        return None
    return float(value)


def contract_for(entry: WatchEntry) -> Contract:
    sec_type = entry.sec_type.upper()
    symbol = entry.symbol.upper()
    if sec_type == "STK":
        return Stock(symbol, entry.exchange or "SMART", entry.currency or "USD")
    if sec_type == "IND":
        return Index(symbol, entry.exchange or "CBOE", entry.currency or "USD")
    if sec_type == "CASH":
        return Forex(symbol)
    if sec_type == "CRYPTO":
        return Crypto(symbol, entry.exchange or "PAXOS", entry.currency or "USD")
    if sec_type == "FUT":
        return Future(symbol, exchange=entry.exchange or "", currency=entry.currency or "USD")
    return Contract(
        secType=sec_type,
        symbol=symbol,
        exchange=entry.exchange or "SMART",
        currency=entry.currency or "USD",
    )


def fetch_quotes(
    settings: Settings,
    entries: Sequence[WatchEntry],
    market_data_type: int | None = None,
) -> list[Quote]:
    """Snapshot quote per entry, in the same order as `entries`."""
    if not entries:
        return []
    notes = {e.symbol.upper(): e.note for e in entries}
    with connected(settings) as ib:
        ib.reqMarketDataType(market_data_type or settings.market_data_type)
        contracts = [contract_for(e) for e in entries]
        qualified = ib.qualifyContracts(*contracts)
        resolved = {c.symbol.upper(): c for c in qualified if c.conId}

        quotes: list[Quote] = []
        wanted: list[Contract] = []
        for entry, contract in zip(entries, contracts):
            key = entry.symbol.upper()
            if key in resolved:
                wanted.append(resolved[key])
            else:
                quotes.append(
                    Quote(
                        symbol=key,
                        sec_type=entry.sec_type.upper(),
                        currency=entry.currency.upper(),
                        note=notes.get(key, ""),
                        error="contract not resolved",
                    )
                )

        tickers = ib.reqTickers(*wanted) if wanted else []
        for ticker in tickers:
            c = ticker.contract
            key = c.symbol.upper()
            quotes.append(
                Quote(
                    symbol=key,
                    sec_type=c.secType,
                    currency=c.currency,
                    exchange=c.primaryExchange or c.exchange or "",
                    con_id=c.conId,
                    last=_clean(ticker.last),
                    bid=_clean(ticker.bid),
                    ask=_clean(ticker.ask),
                    close=_clean(ticker.close),
                    open_=_clean(ticker.open),
                    high=_clean(ticker.high),
                    low=_clean(ticker.low),
                    volume=_clean(ticker.volume),
                    market_price=_clean(ticker.marketPrice()),
                    quote_time=ticker.time.isoformat() if ticker.time else None,
                    note=notes.get(key, ""),
                )
            )

    order = {e.symbol.upper(): i for i, e in enumerate(entries)}
    return sorted(quotes, key=lambda q: order.get(q.symbol, 10**6))


def entries_from_symbols(symbols: Iterable[str], sec_type: str = "STK") -> list[WatchEntry]:
    return [WatchEntry(symbol=s.upper(), sec_type=sec_type.upper()) for s in symbols]


def render_quotes(quotes: Sequence[Quote]) -> str:
    header = (
        f"{'SYMBOL':<10}{'TYPE':<6}{'LAST':>11}{'CHG':>10}{'CHG%':>8}"
        f"{'BID':>11}{'ASK':>11}{'CLOSE':>11}{'VOLUME':>12}  NOTE"
    )
    lines = [header, "-" * len(header)]

    def num(value: float | None, width: int, digits: int = 2) -> str:
        return "-".rjust(width) if value is None else f"{value:>{width},.{digits}f}"

    for q in quotes:
        if q.error:
            lines.append(f"{q.symbol:<10}{q.sec_type:<6}{'':>63}  {q.error}")
            continue
        lines.append(
            f"{q.symbol:<10}{q.sec_type:<6}{num(q.last or q.market_price, 11)}"
            f"{num(q.change, 10)}{num(q.change_pct, 8)}{num(q.bid, 11)}{num(q.ask, 11)}"
            f"{num(q.close, 11)}{num(q.volume, 12, 0)}  {q.note}"
        )
    return "\n".join(lines)


def now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
