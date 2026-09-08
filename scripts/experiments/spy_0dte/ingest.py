"""SPY 0DTE OPRA ingest driver (local experiment).

Pulls full-tick SPY equity trades/quotes (SIP) and expiration-day option
quotes/trades (OPRA, hard-enforced by the client) for each closed XNYS
session in the requested range, writing market-lake schema Parquet under the
``date=/asset_class=/data_type=`` partition layout.

Run (PowerShell, from the repository root, after setting credentials):

    $env:LAKE_PROVIDER__API_KEY = "..."
    $env:LAKE_PROVIDER__API_SECRET = "..."
    uv run --extra dev python scripts/experiments/spy_0dte/ingest.py `
        --days 42 --data-root .local/spy-0dte/lake

Resumable at (session, dataset, chunk-window) granularity via
``state.json``; completed datasets are skipped on re-run.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from statistics import median

import pyarrow as pa
from osi import (
    ContractSpec,
    build_contracts,
    chunk_windows,
    closed_sessions,
    delete_lake_parts,
    lake_object_dir,
    load_state,
    save_state,
    write_lake_table,
)

from lake.configuration import load_settings
from lake.contracts.partitions import AssetClass, DataType
from lake.contracts.schemas import schema_for
from lake.providers.alpaca.client import AlpacaClient, AlpacaHTTPError
from lake.providers.alpaca.records import (
    NormalizedOptionQuote,
    NormalizedOptionTrade,
    NormalizedQuote,
    NormalizedTrade,
)

_DATASETS: tuple[tuple[AssetClass, DataType], ...] = (
    (AssetClass.EQUITY, DataType.QUOTE),
    (AssetClass.EQUITY, DataType.TRADE),
    (AssetClass.OPTION, DataType.QUOTE),
    (AssetClass.OPTION, DataType.TRADE),
)

_EQUITY_SYMBOL = "SPY"
_OPTION_FILE_SYMBOL = "SPY-0DTE"

#: HTTP statuses worth retrying at the driver level; anything else (bad keys,
#: missing entitlements) fails fast so configuration problems surface.
_RETRIABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})


def equity_quote_table(quotes: Sequence[NormalizedQuote]) -> pa.Table:
    return pa.Table.from_pydict(
        {
            "timestamp": [quote.timestamp for quote in quotes],
            "symbol": [quote.symbol for quote in quotes],
            "bid_exchange": [quote.bid_exchange for quote in quotes],
            "bid_price": [quote.bid_price for quote in quotes],
            "bid_size": [quote.bid_size for quote in quotes],
            "ask_exchange": [quote.ask_exchange for quote in quotes],
            "ask_price": [quote.ask_price for quote in quotes],
            "ask_size": [quote.ask_size for quote in quotes],
            "conditions": [list(quote.conditions) for quote in quotes],
            "tape": [quote.tape for quote in quotes],
        },
        schema=schema_for(AssetClass.EQUITY, DataType.QUOTE),
    )


def equity_trade_table(trades: Sequence[NormalizedTrade]) -> pa.Table:
    return pa.Table.from_pydict(
        {
            "timestamp": [trade.timestamp for trade in trades],
            "symbol": [trade.symbol for trade in trades],
            "trade_id": [trade.trade_id for trade in trades],
            "exchange": [trade.exchange for trade in trades],
            "price": [trade.price for trade in trades],
            "size": [trade.size for trade in trades],
            "conditions": [list(trade.conditions) for trade in trades],
            "tape": [trade.tape for trade in trades],
            "update": [trade.update for trade in trades],
        },
        schema=schema_for(AssetClass.EQUITY, DataType.TRADE),
    )


def option_quote_table(quotes: Sequence[NormalizedOptionQuote]) -> pa.Table:
    return pa.Table.from_pydict(
        {
            "timestamp": [quote.timestamp for quote in quotes],
            "symbol": [quote.symbol for quote in quotes],
            "bid_exchange": [quote.bid_exchange for quote in quotes],
            "bid_price": [quote.bid_price for quote in quotes],
            "bid_size": [quote.bid_size for quote in quotes],
            "ask_exchange": [quote.ask_exchange for quote in quotes],
            "ask_price": [quote.ask_price for quote in quotes],
            "ask_size": [quote.ask_size for quote in quotes],
            "condition": [quote.condition for quote in quotes],
        },
        schema=schema_for(AssetClass.OPTION, DataType.QUOTE),
    )


def option_trade_table(trades: Sequence[NormalizedOptionTrade]) -> pa.Table:
    return pa.Table.from_pydict(
        {
            "timestamp": [trade.timestamp for trade in trades],
            "symbol": [trade.symbol for trade in trades],
            "exchange": [trade.exchange for trade in trades],
            "price": [trade.price for trade in trades],
            "size": [trade.size for trade in trades],
            "condition": [trade.condition for trade in trades],
        },
        schema=schema_for(AssetClass.OPTION, DataType.TRADE),
    )


def part_path(
    data_root: Path,
    session_date: date,
    asset_class: AssetClass,
    data_type: DataType,
    part_index: int,
) -> Path:
    directory = lake_object_dir(data_root, session_date, asset_class, data_type)
    return directory / f"{_OPTION_FILE_SYMBOL}.part-{part_index:04d}.parquet"


async def fetch_window(
    client: AlpacaClient,
    asset_class: AssetClass,
    data_type: DataType,
    contracts: Sequence[ContractSpec],
    session_date: date,
    window: tuple[datetime, datetime],
    attempts: int = 4,
) -> pa.Table:
    """Fetch one chunk window with driver-level retries on HTTP failures."""
    start, end = window
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            if asset_class is AssetClass.EQUITY:
                if data_type is DataType.QUOTE:
                    return equity_quote_table(
                        await client.fetch_quotes(_EQUITY_SYMBOL, start, end)
                    )
                return equity_trade_table(
                    await client.fetch_trades(_EQUITY_SYMBOL, start, end)
                )
            records = [spec.as_record(session_date) for spec in contracts]
            if data_type is DataType.QUOTE:
                return option_quote_table(
                    await client.fetch_option_quotes(records, start, end, limit=10_000)
                )
            return option_trade_table(
                await client.fetch_option_trades(records, start, end, limit=10_000)
            )
        except AlpacaHTTPError as error:
            if error.status_code not in _RETRIABLE_STATUSES:
                raise
            last_error = error
            await asyncio.sleep(min(30.0 * 2 ** (attempt - 1), 300.0))
    message = f"fetch failed for {session_date} {asset_class.value}/{data_type.value}"
    raise RuntimeError(message) from last_error


async def ingest_dataset(
    client: AlpacaClient,
    *,
    session_date: date,
    asset_class: AssetClass,
    data_type: DataType,
    contracts: Sequence[ContractSpec],
    windows: Sequence[tuple[datetime, datetime]],
    data_root: Path,
    fetch_concurrency: int,
    pause_seconds: float,
) -> int:
    """Fetch and write one dataset for one session; returns total rows."""
    semaphore = asyncio.Semaphore(fetch_concurrency)
    total_rows = 0

    async def run(index: int, window: tuple[datetime, datetime]) -> int:
        target = part_path(data_root, session_date, asset_class, data_type, index)
        if target.is_file():
            return 0
        async with semaphore:
            table = await fetch_window(
                client, asset_class, data_type, contracts, session_date, window
            )
            if table.num_rows:
                write_lake_table(
                    data_root=data_root,
                    session_date=session_date,
                    asset_class=asset_class,
                    data_type=data_type,
                    symbol=_OPTION_FILE_SYMBOL,
                    table=table,
                    part_index=index,
                )
            await asyncio.sleep(pause_seconds)
            return table.num_rows

    stride = max(fetch_concurrency, 2)
    for start in range(0, len(windows), stride):
        batch = list(enumerate(windows))[start : start + stride]
        rows = await asyncio.gather(*(run(index, window) for index, window in batch))
        total_rows += sum(rows)
        print(
            f"    {asset_class.value}/{data_type.value}: "
            f"{start + len(batch)}/{len(windows)} windows",
            flush=True,
        )
    return total_rows


def reference_spot_from_quotes(
    tables: Sequence[pa.Table], until: datetime
) -> float | None:
    """Median NBBO mid over valid quotes up to ``until`` (UTC)."""
    mids: list[float] = []
    for table in tables:
        if table.num_rows == 0:
            continue
        frame = table.select(["timestamp", "bid_price", "ask_price"]).to_pandas()
        frame = frame[frame["timestamp"] <= until]
        bids = frame["bid_price"].astype(float)
        asks = frame["ask_price"].astype(float)
        valid = (bids > 0) & (asks > 0) & (asks >= bids)
        mids.extend(((bids + asks) / 2)[valid].tolist())
    return None if not mids else float(median(mids))


async def ingest_session(
    client: AlpacaClient,
    *,
    session: tuple[date, datetime, datetime],
    next_session: tuple[date, datetime, datetime] | None,
    cfg: argparse.Namespace,
    state: dict,
) -> None:
    session_date, open_time, close_time = session
    next_expiry = next_session[0] if next_session is not None else None
    next_close = next_session[2] if next_session is not None else None
    key = session_date.isoformat()
    entry = state["sessions"].setdefault(key, {})
    datasets_state = entry.setdefault("datasets", {})
    if entry.get("open") is None:
        entry["open"] = open_time.isoformat()
        entry["close"] = close_time.isoformat()
    if next_close is not None:
        entry["next_close"] = next_close.isoformat()
    windows = chunk_windows(open_time, close_time, cfg.chunk_minutes)
    print(
        f"session {key}: {len(windows)} windows "
        f"({open_time.isoformat()} -> {close_time.isoformat()})",
        flush=True,
    )

    if entry.get("spot") is None:
        probe = await asyncio.gather(
            fetch_window(
                client, AssetClass.EQUITY, DataType.QUOTE, (), session_date, windows[0]
            ),
            fetch_window(
                client,
                AssetClass.EQUITY,
                DataType.QUOTE,
                (),
                session_date,
                windows[1] if len(windows) > 1 else windows[0],
            ),
        )
        spot = reference_spot_from_quotes(probe, open_time + timedelta(minutes=15))
        if spot is None:
            spot = reference_spot_from_quotes(probe[:1], close_time)
        if spot is None:
            raise RuntimeError(f"no valid SPY quotes found for {key} open")
        entry["spot"] = spot
        entry["feed_equity"] = "sip"
        entry["feed_option"] = "opra"
        save_state(cfg.data_root, state)
    spot = float(entry["spot"])

    manifest = entry.get("contracts")
    if manifest is not None and manifest and manifest[0].get("dte") is None:
        # Pre-1DTE manifest: rebuild with both ladders and re-pull trades
        # from scratch (stale single-ladder parts must not be reused).
        manifest = None
        datasets_state.pop("option/trade", None)
        delete_lake_parts(
            cfg.data_root, session_date, AssetClass.OPTION, DataType.TRADE
        )
        save_state(cfg.data_root, state)
    if manifest is None:
        specs_built = build_contracts(
            session_date=session_date,
            reference_spot=spot,
            strike_pct=cfg.strike_pct,
            strike_step=cfg.strike_step,
            next_expiry=next_expiry if cfg.with_1dte else None,
        )
        entry["contracts"] = [
            {
                "symbol": spec.symbol,
                "strike": spec.strike,
                "type": spec.option_type,
                "expiry": spec.expiry.isoformat() if spec.expiry else None,
                "dte": spec.dte,
            }
            for spec in specs_built
        ]
        save_state(cfg.data_root, state)
    specs = [
        ContractSpec(
            symbol=item["symbol"],
            strike=item["strike"],
            option_type=item["type"],
            expiry=(date.fromisoformat(item["expiry"]) if item.get("expiry") else None),
            dte=int(item.get("dte", 0)),
        )
        for item in entry["contracts"]
    ]
    print(f"  spot={spot:.2f} contracts={len(specs)}", flush=True)

    for asset_class, data_type in _DATASETS:
        name = f"{asset_class.value}/{data_type.value}"
        if datasets_state.get(name) == "done":
            print(f"  {name}: done (skipped)", flush=True)
            continue
        started = time.perf_counter()
        try:
            rows = await ingest_dataset(
                client,
                session_date=session_date,
                asset_class=asset_class,
                data_type=data_type,
                contracts=specs,
                windows=windows,
                data_root=cfg.data_root,
                fetch_concurrency=cfg.fetch_concurrency,
                pause_seconds=cfg.pause_seconds,
            )
        except AlpacaHTTPError as error:
            if (
                error.status_code == 404
                and asset_class is AssetClass.OPTION
                and data_type is DataType.QUOTE
            ):
                print(
                    f"  {name}: HTTP 404 — historical option quotes are not "
                    "enabled on this account; proceeding with option trades",
                    flush=True,
                )
                datasets_state[name] = "unavailable"
                save_state(cfg.data_root, state)
                continue
            raise
        datasets_state[name] = "done"
        entry["rows"] = {**entry.get("rows", {}), name: rows}
        save_state(cfg.data_root, state)
        print(
            f"  {name}: {rows} rows in {time.perf_counter() - started:.1f}s",
            flush=True,
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-root", type=Path, default=Path(".local/spy-0dte/lake"))
    parser.add_argument("--start", type=date.fromisoformat, default=None)
    parser.add_argument("--end", type=date.fromisoformat, default=None)
    parser.add_argument("--days", type=int, default=42)
    parser.add_argument("--strike-pct", type=float, default=2.5)
    parser.add_argument("--strike-step", type=float, default=1.0)
    parser.add_argument("--with-1dte", action="store_true", default=True)
    parser.add_argument("--no-1dte", dest="with_1dte", action="store_false")
    parser.add_argument("--chunk-minutes", type=int, default=30)
    parser.add_argument("--fetch-concurrency", type=int, default=2)
    parser.add_argument("--pause-seconds", type=float, default=0.25)
    parser.add_argument("--max-sessions", type=int, default=None)
    args = parser.parse_args(argv)
    args.data_root = args.data_root.resolve()
    return args


def select_sessions(
    args: argparse.Namespace,
) -> tuple[tuple[date, datetime, datetime], ...]:
    today = datetime.now(UTC).date()
    if args.start is not None and args.end is not None:
        sessions = closed_sessions(args.start, args.end)
    else:
        lookback = max(args.days * 3, 14)
        sessions = closed_sessions(today - timedelta(days=lookback), today)
        sessions = sessions[-args.days :] if args.days else sessions
    return sessions


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = load_settings()
    if settings.provider.api_key is None or settings.provider.api_secret is None:
        print(
            "LAKE_PROVIDER__API_KEY and LAKE_PROVIDER__API_SECRET must be set "
            "in the environment before running the ingest.",
            file=sys.stderr,
        )
        return 2
    sessions = select_sessions(args)
    if args.max_sessions is not None:
        sessions = sessions[: args.max_sessions]
    print(f"ingesting {len(sessions)} sessions into {args.data_root}", flush=True)
    state = load_state(args.data_root)
    state.setdefault("sessions", {})

    async def run() -> None:
        client = AlpacaClient.from_settings(settings.provider)
        try:
            for index, session in enumerate(sessions):
                following = sessions[index + 1] if index + 1 < len(sessions) else None
                await ingest_session(
                    client,
                    session=session,
                    next_session=following,
                    cfg=args,
                    state=state,
                )
        finally:
            await client.aclose()

    asyncio.run(run())
    print("ingest complete", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
