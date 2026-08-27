"""Command line interface.

Every command that returns data accepts `--json`, so the CLI doubles as an
integration surface for scripts and agents:

    ib-agent positions --options --group-by expiry --json
    ib-agent positions --right put --dte-max 30 --json
    ib-agent watchlist quotes --json

The JSON payloads and the exit codes are a published contract; see
`contract.py` and docs/OH-INTEGRATION-PLAN.md before changing either.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time
from typing import Sequence

from . import activity, api, gateway, login, market, query
from .config import Settings, load_settings
from .contract import (
    EXIT_GATEWAY,
    EXIT_INTERRUPT,
    EXIT_NEEDS_2FA,
    EXIT_NO_DATA,
    EXIT_OK,
    EXIT_USAGE,
    SCHEMA_VERSION,
    NoData,
)
from .portfolio import GatewayUnavailable, Snapshot
from .query import PositionFilter
from .watchlist import MARKET_DATA_TYPE_NAMES, WatchEntry, render_quotes


def emit_json(payload: dict[str, object]) -> None:
    """Print one JSON object on stdout, stamped with the schema version.

    Always an object, never a bare array: an array has nowhere to carry
    `schema`, and a consumer that cannot tell which version it is parsing has no
    way to fail safely.
    """
    print(json.dumps({"schema": SCHEMA_VERSION, **payload}, indent=2, sort_keys=True, default=str))


def _fmt(value: float | None, width: int = 14, digits: int = 2) -> str:
    return "-".rjust(width) if value is None else f"{value:>{width},.{digits}f}"


# --- data loading ----------------------------------------------------------


def load_positions(settings: Settings, args: argparse.Namespace) -> api.PositionSet:
    """Adapter over `api.positions`: translate flags, let exceptions through."""
    return api.positions(
        settings,
        use_stored=getattr(args, "stored", False),
        save=getattr(args, "save", False),
    )


def filter_from_args(args: argparse.Namespace) -> PositionFilter:
    return PositionFilter(
        sec_types=query.normalize_sec_types(query.parse_csv(args.type)),
        rights=query.normalize_rights(query.parse_csv(args.right)),
        underlyings=[s.upper() for s in query.parse_csv(args.underlying)],
        asset_classes=[s.upper() for s in query.parse_csv(args.asset)],
        accounts=query.parse_csv(args.account),
        expiry_prefixes=[
            query.normalize_expiry_filter(e) for e in query.parse_csv(args.expiry)
        ],
        expiry_from=query.normalize_expiry_filter(args.expiry_from or ""),
        expiry_to=query.normalize_expiry_filter(args.expiry_to or ""),
        dte_max=args.dte_max,
        dte_min=args.dte_min,
        side=(args.side or "").lower(),
        contains=args.contains or "",
        options_only=args.options,
        equities_only=args.equities,
    )


# --- commands --------------------------------------------------------------


def cmd_status(settings: Settings, args: argparse.Namespace) -> int:
    payload = api.status_payload(settings)
    if args.json:
        emit_json(payload)
        return EXIT_OK if payload["ready"] else EXIT_GATEWAY
    print(f"gateway {settings.host}:{settings.port} ({settings.trading_mode})")
    print(f"  api port listening : {payload['listening']}")
    print(f"  ibc process alive  : {payload['process_running']}")
    print(f"  readonly api       : {settings.readonly}")
    print(f"  db                 : {settings.db_path}")
    if not payload["ready"]:
        print("\nGateway is not accepting connections. Run: scripts/gateway-up.sh")
        return EXIT_GATEWAY
    return EXIT_OK


def print_snapshot(snapshot: Snapshot) -> None:
    print(f"snapshot @ {snapshot.taken_at.isoformat(timespec='seconds')}  "
          f"accounts={','.join(snapshot.accounts) or '?'}")
    if not snapshot.positions:
        print("  (no open positions)")
    else:
        print(query.render_table(snapshot.positions, query.pick_columns(snapshot.positions)))
        print(query.render_totals(query.summarize(snapshot.positions)))
    if snapshot.values:
        print("\n  account values:")
        for v in snapshot.values:
            print(f"    {v.tag:<22}{v.value:>18} {v.currency}")


def cmd_sync(settings: Settings, args: argparse.Namespace) -> int:
    try:
        result = api.sync(settings)
    except GatewayUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_GATEWAY

    if args.json:
        emit_json(api.sync_payload(settings, result))
        return EXIT_OK
    if not args.quiet:
        print_snapshot(result.snapshot)
    print(f"\nsaved snapshot #{result.snapshot_id} -> {settings.db_path}")
    print(f"json copy            -> {result.json_path}")
    return EXIT_OK


def cmd_positions(settings: Settings, args: argparse.Namespace) -> int:
    try:
        result = load_positions(settings, args)
    except GatewayUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_GATEWAY
    except NoData as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_NO_DATA

    flt = filter_from_args(args)
    today = dt.date.today()
    selected = api.select(
        result.rows,
        flt,
        sort=args.sort,
        reverse=getattr(args, "reverse", False),
        limit=args.limit,
        today=today,
    )
    group_by = None if args.group_by in (None, "none") else args.group_by

    if args.json:
        emit_json(
            api.positions_payload(
                result,
                selected,
                flt,
                sort=args.sort,
                group_by=group_by,
                totals_only=args.totals_only,
                today=today,
            )
        )
        return EXIT_OK

    meta = result.meta
    totals = query.summarize(selected)
    columns = query.pick_columns(selected) if selected else query.DEFAULT_COLUMNS
    src = meta["source"]
    if meta.get("snapshot_id") and src == "snapshot":
        src = f"snapshot #{meta['snapshot_id']}"
    print(f"{len(selected)} of {len(result.rows)} positions  [{src} @ {meta['as_of']}]")
    if not selected:
        print("(nothing matches the filter)")
        return EXIT_OK

    if group_by:
        for key, members in query.group_rows(selected, group_by):
            print(f"\n== {group_by} = {key}  ({len(members)}) ==")
            if not args.totals_only:
                print(query.render_table(members, columns))
            print(query.render_totals(query.summarize(members), "  subtotal"))
        print()
    elif not args.totals_only:
        print(query.render_table(selected, columns))
    print(query.render_totals(totals))
    return EXIT_OK


def _summary_command(
    settings: Settings, args: argparse.Namespace, group_by: str, label: str
) -> int:
    """Shared implementation for `expiries` / `underlyings`."""
    try:
        result = load_positions(settings, args)
    except GatewayUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_GATEWAY
    except NoData as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_NO_DATA

    flt = filter_from_args(args)
    selected = api.select(result.rows, flt)
    groups = query.group_rows(selected, group_by)

    if args.json:
        emit_json(api.summary_payload(result, selected, flt, group_by=group_by))
        return EXIT_OK

    meta = result.meta
    header = f"{label:<14}{'POS':>5}{'CONTRACTS':>11}{'VALUE':>14}{'UNREALIZED':>13}"
    print(f"[{meta['source']} @ {meta['as_of']}]")
    print(header)
    print("-" * len(header))
    for key, members in groups:
        t = query.summarize(members)
        print(f"{key:<14}{t.count:>5}{t.contracts:>11,.0f}"
              f"{t.market_value:>14,.2f}{t.unrealized_pnl:>13,.2f}")
    print("-" * len(header))
    print(query.render_totals(query.summarize(selected)))
    return EXIT_OK


def cmd_expiries(settings: Settings, args: argparse.Namespace) -> int:
    return _summary_command(settings, args, "expiry", "EXPIRY")


def cmd_underlyings(settings: Settings, args: argparse.Namespace) -> int:
    return _summary_command(settings, args, "underlying", "UNDERLYING")


def cmd_show(settings: Settings, args: argparse.Namespace) -> int:
    try:
        result = api.stored(settings)
    except NoData as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_NO_DATA
    if args.json:
        emit_json(api.snapshot_payload(result))
        return EXIT_OK
    meta = result.meta
    print(f"snapshot #{meta['snapshot_id']} @ {meta['as_of']}  "
          f"net_liq={meta['net_liquidation']}")
    if result.rows:
        print(query.render_table(result.rows, query.pick_columns(result.rows)))
        print(query.render_totals(query.summarize(result.rows)))
    return EXIT_OK


def cmd_history(settings: Settings, args: argparse.Namespace) -> int:
    rows = api.history(settings, limit=args.limit)
    if args.json:
        emit_json({"count": len(rows), "snapshots": rows})
        return EXIT_OK if rows else EXIT_NO_DATA
    if not rows:
        print("no snapshots stored yet")
        return EXIT_NO_DATA
    for r in rows:
        print(f"#{r['id']:<5}{r['taken_at']:<28}{_fmt(r['net_liq'])}  {r['source']}")
    return EXIT_OK


def cmd_watch(settings: Settings, args: argparse.Namespace) -> int:
    """Sync on a fixed interval; intended for a long-running unattended loop."""
    while True:
        rc = cmd_sync(settings, args)
        if rc != 0:
            print(f"sync failed (rc={rc}); retrying in {args.interval}s", file=sys.stderr)
        time.sleep(args.interval)


def cmd_watchlist(settings: Settings, args: argparse.Namespace) -> int:
    if args.action == "add":
        added = api.watchlist_add(
            settings,
            args.symbols,
            sec_type=args.sec_type,
            exchange=args.exchange,
            currency=args.currency,
            note=args.note,
        )
        print(f"added {added} symbol(s)")
        return EXIT_OK

    if args.action == "remove":
        removed = api.watchlist_remove(settings, args.symbols)
        print(f"removed {removed} row(s)")
        return EXIT_OK

    entries = api.watchlist(settings)
    if args.action == "list":
        if args.json:
            emit_json({"count": len(entries), "watchlist": [vars(e) for e in entries]})
            return EXIT_OK
        if not entries:
            print("watchlist is empty; add one with: ib-agent watchlist add AAPL SPY")
            return EXIT_OK
        for e in entries:
            print(f"{e.symbol:<10}{e.sec_type:<6}{e.exchange:<8}{e.currency:<5}{e.note}")
        return EXIT_OK

    # action == "quotes"
    if args.symbols:
        entries = [
            WatchEntry(symbol=s.upper(), sec_type=args.sec_type, currency=args.currency)
            for s in args.symbols
        ]
    try:
        values = api.quotes(settings, entries)
    except NoData as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_NO_DATA
    except GatewayUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_GATEWAY
    if args.json:
        emit_json(api.quotes_payload(settings, values))
        return EXIT_OK
    mdt = MARKET_DATA_TYPE_NAMES.get(settings.market_data_type, settings.market_data_type)
    print(f"[market data: {mdt}]")
    print(render_quotes(values))
    return EXIT_OK


def cmd_instruments(settings: Settings, args: argparse.Namespace) -> int:
    rows = api.instruments(settings)
    if args.json:
        emit_json({"count": len(rows), "instruments": rows})
        return EXIT_OK
    if not rows:
        print("no instruments classified yet; run `ib-agent sync`")
        return EXIT_OK
    header = f"{'SYMBOL':<8}{'CLASS':<9}{'INDUSTRY':<24}{'NAME':<40}"
    print(header)
    print("-" * len(header))
    for r in rows:
        print(f"{r['symbol']:<8}{r['asset_class']:<9}{(r['industry'] or '')[:23]:<24}"
              f"{(r['long_name'] or '')[:39]:<40}")
    return EXIT_OK


def cmd_resolve(settings: Settings, args: argparse.Namespace) -> int:
    """Symbols/conids -> IB contract ids, or held positions -> underlying conids."""
    meta: dict[str, object] | None = None
    if args.from_positions:
        items, meta = api.resolve_positions(
            settings, use_stored=args.stored, options_only=args.options
        )
    else:
        items = api.resolve(settings, args.specs, currency=args.currency)

    if args.json:
        emit_json(api.resolve_payload(items, meta=meta))
    else:
        print(market.render_resolved(items))
    return EXIT_OK if any(not i.error for i in items) else EXIT_NO_DATA


def cmd_chain(settings: Settings, args: argparse.Namespace) -> int:
    chains = api.chain(
        settings,
        args.symbol,
        sec_type=args.sec_type,
        currency=args.currency,
        exchange=args.exchange,
    )
    if not chains:
        print(f"no option parameters for {args.symbol.upper()}", file=sys.stderr)
        return EXIT_NO_DATA

    payload = api.chain_payload(
        args.symbol,
        chains,
        expiry_prefix=query.normalize_expiry_filter(args.expiry or ""),
        strike_min=args.strike_min,
        strike_max=args.strike_max,
    )
    if args.json:
        emit_json(payload)
        return EXIT_OK
    # Render the narrowed copy, so the table matches what --json would carry.
    print(
        market.render_chains(
            [
                market.ChainParams(
                    underlying_symbol=c["underlying_symbol"],
                    underlying_con_id=c["underlying_con_id"],
                    exchange=c["exchange"],
                    trading_class=c["trading_class"],
                    multiplier=c["multiplier"],
                    expirations=list(c["expirations"]),
                    strikes=list(c["strikes"]),
                )
                for c in payload["chains"]
            ],
            limit=args.limit or 12,
        )
    )
    return EXIT_OK


def cmd_greeks(settings: Settings, args: argparse.Namespace) -> int:
    meta: dict[str, object] | None = None
    if args.specs:
        rows = api.greeks_for_specs(settings, args.specs, currency=args.currency)
    else:
        rows, meta = api.greeks_for_positions(
            settings, use_stored=args.stored, flt=filter_from_args(args)
        )

    if args.json:
        emit_json(api.greeks_payload(settings, rows, meta=meta))
        return EXIT_OK
    mdt = MARKET_DATA_TYPE_NAMES.get(settings.market_data_type, settings.market_data_type)
    print(f"[market data: {mdt}]")
    print(market.render_greeks(rows))
    totals = market.totals(rows)
    print(
        f"\n  contracts {totals['contracts']}  priced {totals['priced']}"
        f"  missing {totals['missing_greeks']}"
    )
    print(
        f"  book delta {_fmt(totals['delta'], 12, 1)}  theta {_fmt(totals['theta'], 12, 1)}"
        f"  vega {_fmt(totals['vega'], 12, 1)}"
    )
    return EXIT_OK if totals["priced"] else EXIT_NO_DATA


def cmd_orders(settings: Settings, args: argparse.Namespace) -> int:
    rows = api.orders(settings, active_only=not args.all)
    if args.json:
        emit_json(api.orders_payload(settings, rows))
        return EXIT_OK
    if not rows:
        print("no working orders")
        print(
            "  (orders placed from another client id — e.g. IBKR Mobile — need\n"
            "   OverrideTwsMasterClientID in ~/ibc/config.ini to be relayed)"
        )
        return EXIT_OK
    print(activity.render_orders(rows))
    t = activity.order_totals(rows)
    print(f"\n  {t['count']} order(s): {t['buy']} buy, {t['sell']} sell, {t['options']} option")
    return EXIT_OK


def cmd_executions(settings: Settings, args: argparse.Namespace) -> int:
    rows = api.executions(
        settings, symbol=args.symbol or "", sec_type=args.sec_type or "", side=args.side or ""
    )
    if args.json:
        emit_json(
            api.executions_payload(
                rows, symbol=args.symbol, sec_type=args.sec_type, side=args.side
            )
        )
        return EXIT_OK
    if not rows:
        print("no executions today (reqExecutions covers the current trading day only)")
        return EXIT_OK
    print(activity.render_executions(rows))
    t = activity.execution_totals(rows)
    print(
        f"\n  {t['count']} fill(s)  proceeds {_fmt(t['proceeds'])}"
        f"  commission {_fmt(t['commission'])}  realized {_fmt(t['realized_pnl'])}"
    )
    return EXIT_OK


def prompt_for_code(attempt: int, total: int) -> str | None:
    """Ask the human for a code, on stderr so `--json` stdout stays parseable.

    Returns None if the user just presses enter, which is how they abandon a
    login without killing the Gateway that is already starting.
    """
    while True:
        if attempt == 1:
            print(
                "\nIBKR is asking for a Mobile Authenticator code.\n"
                "Open IBKR Mobile, read a code that has JUST refreshed (they live ~30s),\n"
                "and type it here. Enter on its own gives up.",
                file=sys.stderr,
            )
        else:
            print(
                f"\nIBKR asked again (attempt {attempt}/{total}) - the last code was "
                "refused or expired.",
                file=sys.stderr,
            )
        print("code: ", end="", file=sys.stderr, flush=True)
        try:
            raw = input()
        except EOFError:
            return None
        if not raw.strip():
            return None
        try:
            return login.validate_code(raw)
        except login.LoginError as exc:
            print(str(exc), file=sys.stderr)


def make_code_provider(args: argparse.Namespace) -> login.CodeProvider:
    """Turn the flags into a code source: `--code` first, then the terminal."""
    supplied = getattr(args, "code", None) or getattr(args, "value", None)
    attempts = getattr(args, "attempts", 3)
    interactive = not getattr(args, "no_prompt", False) and sys.stdin.isatty()

    def provider(attempt: int) -> str | None:
        if attempt == 1 and supplied:
            return login.validate_code(supplied)
        if not interactive:
            return None
        return prompt_for_code(attempt, attempts)

    return provider


def report_login(settings: Settings, args: argparse.Namespace, result: login.LoginResult) -> int:
    payload = api.login_payload(settings, result, action=args.action)
    if args.json:
        emit_json(payload)
    else:
        print(f"login {result.reason}: {result.detail}", file=sys.stderr if not result.ok else sys.stdout)
        print(f"  api port listening : {payload['listening']}")
        print(f"  ibc process alive  : {payload['process_running']}")
        print(f"  codes submitted    : {result.attempts}")
    if result.reason in login.PENDING_REASONS and payload["process_running"]:
        # Never let this be a silent surprise: IBC keeps retrying the login for
        # as long as the process lives, and IBKR eventually refuses the
        # credentials outright. Three days of that cost a locked username once.
        print(
            "\nThe Gateway is still running and will keep retrying this login.\n"
            "Send a code soon with 'ib-agent gateway code <CODE>', or stop it with\n"
            "'ib-agent gateway down' - do not leave it waiting for hours.",
            file=sys.stderr,
        )
    if result.ok:
        return EXIT_OK
    # All three mean the same thing to a caller: a human has to produce a code.
    if result.reason in {
        login.REASON_NEEDS_CODE,
        login.REASON_DIALOG_STALE,
        login.REASON_LOST_RACE,
    }:
        return EXIT_NEEDS_2FA
    return EXIT_GATEWAY


def cmd_gateway(settings: Settings, args: argparse.Namespace) -> int:
    if args.action == "status":
        payload = api.status_payload(settings)
        remaining = login.dialog_remaining()
        payload["dialog_expires_in"] = None if remaining is None else round(remaining, 1)
        if args.json:
            emit_json(payload)
        else:
            print(f"gateway {settings.host}:{settings.port} ({settings.trading_mode})")
            print(f"  api port listening : {payload['listening']}")
            print(f"  ibc process alive  : {payload['process_running']}")
            print(f"  2fa dialog closes  : {payload['dialog_expires_in']}s")
        return EXIT_OK if payload["ready"] else EXIT_GATEWAY
    if args.action == "down":
        result = gateway.stop()
        print(result.stdout.strip() or "stopped")
        return result.returncode
    # up / code: both drive the same login state machine. `code` refuses to
    # start a Gateway, because the caller is answering a prompt from one that is
    # already running.
    supplied = bool(args.code or args.value)
    try:
        result = api.gateway_login(
            settings,
            code_provider=make_code_provider(args),
            launch=args.action == "up",
            attempts=args.attempts,
            dialog_timeout=args.wait,
            # A code already in hand cannot survive a wait for the next dialog,
            # so say so immediately instead of spending it on a dying one.
            wait_for_fresh=not supplied,
        )
    except login.LoginError as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_USAGE
    return report_login(settings, args, result)


# --- parser ----------------------------------------------------------------


def add_json_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="machine-readable output")


def add_source_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--stored",
        action="store_true",
        help="read the last stored snapshot instead of querying the Gateway",
    )
    parser.add_argument(
        "--save", action="store_true", help="persist the freshly fetched snapshot"
    )


def add_filter_args(parser: argparse.ArgumentParser) -> None:
    g = parser.add_argument_group("filters")
    g.add_argument("-t", "--type", help="sec types: stock,option,etf,fut,... (csv)")
    g.add_argument("-r", "--right", help="option right: put,call (csv)")
    g.add_argument("-u", "--underlying", help="underlying symbols (csv)")
    g.add_argument("-a", "--asset", help="asset class: etf,common,adr (csv)")
    g.add_argument("-e", "--expiry", help="expiry prefix: 2026-09, 20260904 (csv)")
    g.add_argument("--expiry-from", help="earliest expiry (ISO date)")
    g.add_argument("--expiry-to", help="latest expiry (ISO date)")
    g.add_argument("--dte-min", type=int, help="minimum days to expiry")
    g.add_argument("--dte-max", type=int, help="maximum days to expiry")
    g.add_argument("--side", choices=["long", "short"], help="long or short positions")
    g.add_argument("--contains", help="substring match on the position symbol")
    g.add_argument("--options", action="store_true", help="options only")
    g.add_argument("--equities", action="store_true", help="stocks/ETFs only")
    g.add_argument("--account", help="account ids (csv)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ib-agent", description="IBKR portfolio sync (read-only)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_status = sub.add_parser("status", help="show gateway/connection status")
    add_json_flag(p_status)

    p_sync = sub.add_parser("sync", help="fetch a portfolio snapshot and store it")
    p_sync.add_argument("--quiet", action="store_true", help="do not print the table")
    add_json_flag(p_sync)

    p_pos = sub.add_parser(
        "positions", aliases=["pos"], help="list positions with filters and grouping"
    )
    add_filter_args(p_pos)
    add_source_args(p_pos)
    p_pos.add_argument(
        "-g",
        "--group-by",
        choices=[*query.GROUP_KEYS, "none"],
        default="none",
        help="group rows and subtotal each group",
    )
    p_pos.add_argument(
        "-s", "--sort", choices=sorted(query.SORT_KEYS), default="symbol", help="sort order"
    )
    p_pos.add_argument(
        "--reverse", action="store_true", help="reverse the sort (e.g. worst P&L first)"
    )
    p_pos.add_argument("--limit", type=int, help="show at most N rows")
    p_pos.add_argument("--totals-only", action="store_true", help="omit the rows")
    add_json_flag(p_pos)

    p_exp = sub.add_parser("expiries", help="option exposure grouped by expiry")
    add_filter_args(p_exp)
    add_source_args(p_exp)
    add_json_flag(p_exp)

    p_und = sub.add_parser("underlyings", help="exposure grouped by underlying")
    add_filter_args(p_und)
    add_source_args(p_und)
    add_json_flag(p_und)

    p_show = sub.add_parser("show", help="print the most recent stored snapshot")
    add_json_flag(p_show)

    p_hist = sub.add_parser("history", help="list stored snapshots")
    p_hist.add_argument("--limit", type=int, default=20)
    add_json_flag(p_hist)

    p_watch = sub.add_parser("watch", help="sync repeatedly on an interval")
    p_watch.add_argument("--interval", type=int, default=900, help="seconds between syncs")
    p_watch.add_argument("--quiet", action="store_true")
    add_json_flag(p_watch)

    p_wl = sub.add_parser("watchlist", aliases=["wl"], help="manage and quote a watchlist")
    p_wl.add_argument("action", choices=["add", "remove", "list", "quotes"])
    p_wl.add_argument("symbols", nargs="*", help="symbols (quotes: overrides the stored list)")
    p_wl.add_argument("--sec-type", default="STK", help="STK, IND, CASH, FUT, CRYPTO")
    p_wl.add_argument("--exchange", default="SMART")
    p_wl.add_argument("--currency", default="USD")
    p_wl.add_argument("--note", default="", help="free-text note stored with the symbol")
    add_json_flag(p_wl)

    p_inst = sub.add_parser("instruments", help="cached underlying reference data")
    add_json_flag(p_inst)

    p_res = sub.add_parser(
        "resolve",
        help="contract ids: symbol -> conid, option -> underlying conid",
        description=(
            "Resolve contract specs through IB. A spec is a conid (12345), a symbol "
            "(GDX), a symbol with sec type (GDX:STK) or an option "
            '("GDX 2026-09-18 P 45"). With --from-positions, resolve what is held, '
            "which reports each option's underlying conid straight from IB."
        ),
    )
    p_res.add_argument("specs", nargs="*", help="contract specs")
    p_res.add_argument(
        "--from-positions",
        action="store_true",
        help="resolve held positions instead of specs",
    )
    p_res.add_argument(
        "--options", action="store_true", help="with --from-positions: options only"
    )
    p_res.add_argument(
        "--stored",
        action="store_true",
        help="with --from-positions: use the last snapshot for the position list",
    )
    p_res.add_argument("--currency", default="USD")
    add_json_flag(p_res)

    p_chain = sub.add_parser("chain", help="option expirations and strikes for an underlying")
    p_chain.add_argument("symbol", help="underlying symbol, e.g. GDX")
    p_chain.add_argument("-e", "--expiry", help="keep expirations with this prefix: 2026-09")
    p_chain.add_argument("--strike-min", type=float)
    p_chain.add_argument("--strike-max", type=float)
    p_chain.add_argument("--exchange", default="", help="keep one exchange, e.g. SMART")
    p_chain.add_argument("--sec-type", default="STK", help="underlying sec type")
    p_chain.add_argument("--currency", default="USD")
    p_chain.add_argument("--limit", type=int, help="values shown per list in the table")
    add_json_flag(p_chain)

    p_greeks = sub.add_parser(
        "greeks",
        help="model greeks (delta, gamma, theta, vega, IV)",
        description=(
            "Model greeks from IB. With no arguments, prices every held option and "
            "totals the book; the position filters apply. Pass contract specs to "
            "price specific contracts instead."
        ),
    )
    p_greeks.add_argument("specs", nargs="*", help='option specs, e.g. "GDX 2026-09-18 P 45"')
    add_filter_args(p_greeks)
    p_greeks.add_argument(
        "--stored", action="store_true", help="use the last snapshot for the position list"
    )
    p_greeks.add_argument("--currency", default="USD")
    add_json_flag(p_greeks)

    p_ord = sub.add_parser("orders", help="working orders (read-only)")
    p_ord.add_argument(
        "--all", action="store_true", help="include inactive/filled orders IB still reports"
    )
    add_json_flag(p_ord)

    p_exec = sub.add_parser(
        "executions",
        aliases=["fills"],
        help="today's fills with commission and realized P&L",
    )
    p_exec.add_argument("--symbol", help="restrict to one underlying")
    p_exec.add_argument("--sec-type", help="restrict to a sec type, e.g. OPT")
    p_exec.add_argument("--side", help="BOT or SLD")
    add_json_flag(p_exec)

    p_gw = sub.add_parser(
        "gateway",
        help="start/stop the Gateway and complete its 2FA login",
        description=(
            "Single entry point for the session: 'up' starts Xvfb and the Gateway, "
            "waits for IBKR's Second Factor Authentication dialog, asks for a "
            "Mobile Authenticator code at the moment it is needed, types it on the "
            "headless display and waits for the API port. 'code' feeds a code to a "
            "login already waiting - use it to refresh a session after IBKR's "
            "weekly server reset."
        ),
    )
    p_gw.add_argument("action", choices=["up", "down", "code", "status"])
    p_gw.add_argument(
        "value",
        nargs="?",
        help="the authenticator code, for 'gateway code 123456'",
    )
    p_gw.add_argument(
        "--code",
        help="authenticator code to submit; prompted for on a terminal if omitted",
    )
    p_gw.add_argument(
        "--attempts",
        type=int,
        default=3,
        help="how many codes to try before giving up (default 3)",
    )
    p_gw.add_argument(
        "--wait",
        type=float,
        default=120.0,
        help="seconds to wait for the 2FA dialog or the port (default 120)",
    )
    p_gw.add_argument(
        "--no-prompt",
        action="store_true",
        help=f"never read stdin; exit {EXIT_NEEDS_2FA} if a code is required (for cron)",
    )
    add_json_flag(p_gw)

    return parser


HANDLERS = {
    "status": cmd_status,
    "sync": cmd_sync,
    "positions": cmd_positions,
    "pos": cmd_positions,
    "expiries": cmd_expiries,
    "underlyings": cmd_underlyings,
    "show": cmd_show,
    "history": cmd_history,
    "watch": cmd_watch,
    "watchlist": cmd_watchlist,
    "wl": cmd_watchlist,
    "instruments": cmd_instruments,
    "resolve": cmd_resolve,
    "chain": cmd_chain,
    "greeks": cmd_greeks,
    "orders": cmd_orders,
    "executions": cmd_executions,
    "fills": cmd_executions,
    "gateway": cmd_gateway,
}


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings()
    try:
        return HANDLERS[args.command](settings, args)
    except KeyboardInterrupt:
        return EXIT_INTERRUPT
    # Backstop: any handler that forgets to translate these still exits with the
    # documented code, so callers can rely on the contract.
    except GatewayUnavailable as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_GATEWAY
    except NoData as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_NO_DATA


if __name__ == "__main__":
    raise SystemExit(main())
