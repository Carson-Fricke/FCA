"""Opening-window IV-path test for SPY 0DTE options (local experiment).

The simplest version of the theta non-uniformity hypothesis: sample implied
volatility every ``--bucket-seconds`` (default 20) from the open through
``--window-minutes`` (default 30). With expiry time known and the NBBO mid
observed, the IV path measures exactly how fast the option bleeds relative to
any fixed-vol reference:

- IV stays flat through the opening window -> the option retained its vol
  premium; effective decay was slower than a flat-IV model anchored at the
  open, despite the highest realized volatility of the day.
- IV drops markedly -> the vol premium was consumed; effective decay ran
  ahead of the flat-IV model.

IV is inverted with the native CRR kernels (American, dividend-adjusted) at
each observation. Aggregates are day-level means with day-block bootstrap
confidence intervals, by moneyness bucket (ATM is the headline).

Run:

    uv run --extra dev python scripts/experiments/spy_0dte/open_iv.py `
        --data-root .local/spy-0dte/lake --out .local/spy-0dte/open-iv
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from analyze import implied_vol_batch, money_bucket
from osi import SECONDS_PER_YEAR, load_state, read_lake_dataset

from lake.contracts.partitions import AssetClass, DataType

_STEPS_INVERT = 64
_IV_ITERATIONS = 60


def bucket_grid(
    open_time: datetime, window_minutes: int, bucket_seconds: int
) -> pd.DatetimeIndex:
    end = open_time + timedelta(minutes=window_minutes)
    return pd.date_range(
        pd.Timestamp(open_time),
        pd.Timestamp(end),
        freq=f"{bucket_seconds}s",
        inclusive="left",
    )


def bucket_last(
    frame: pd.DataFrame,
    value_columns: list[str],
    grid: pd.DatetimeIndex,
    open_ns: int,
    bucket_seconds: int,
) -> pd.DataFrame:
    """Last observation per bucket index, reindexed on the bucket grid."""
    if frame.empty:
        return pd.DataFrame(
            {column: pd.Series(dtype=float) for column in value_columns}
        )
    timestamps = frame["timestamp"].to_numpy(dtype="datetime64[ns]").astype("int64")
    indices = (timestamps - open_ns) // (bucket_seconds * 1_000_000_000)
    working = frame.copy()
    working["_bucket"] = indices.astype(int)
    working = working[(working["_bucket"] >= 0) & (working["_bucket"] < len(grid))]
    last = working.groupby("_bucket", sort=True)[value_columns].last()
    return last.reindex(range(len(grid))).ffill()


def mid_frame(table: pa.Table) -> pd.DataFrame:
    columns = ["timestamp", "bid_price", "ask_price"]
    if "symbol" in table.column_names:
        columns.append("symbol")
    frame = table.select(columns).to_pandas()
    bids = frame["bid_price"].astype(float)
    asks = frame["ask_price"].astype(float)
    valid = (bids > 0) & (asks > 0) & (asks >= bids)
    frame = frame[valid].copy()
    frame["mid"] = (bids[valid] + asks[valid]) / 2.0
    frame["spread"] = asks[valid] - bids[valid]
    keep = ["timestamp", "mid", "spread"]
    if "symbol" in frame.columns:
        keep.append("symbol")
    return frame[keep]


def trade_frame(table: pa.Table) -> pd.DataFrame:
    """Trade prints as a price series (median-per-bucket upstream)."""
    frame = table.select(["timestamp", "symbol", "price"]).to_pandas()
    frame["mid"] = frame["price"].astype(float)
    frame["spread"] = 0.0
    return frame[["timestamp", "symbol", "mid", "spread"]]


def bucket_median(
    frame: pd.DataFrame,
    value_columns: list[str],
    grid: pd.DatetimeIndex,
    open_ns: int,
    bucket_seconds: int,
) -> pd.DataFrame:
    """Median observation per bucket index, reindexed on the bucket grid."""
    if frame.empty:
        return pd.DataFrame(
            {column: pd.Series(dtype=float) for column in value_columns}
        )
    timestamps = frame["timestamp"].to_numpy(dtype="datetime64[ns]").astype("int64")
    indices = (timestamps - open_ns) // (bucket_seconds * 1_000_000_000)
    working = frame.copy()
    working["_bucket"] = indices.astype(int)
    working = working[(working["_bucket"] >= 0) & (working["_bucket"] < len(grid))]
    median = working.groupby("_bucket", sort=True)[value_columns].median()
    return median.reindex(range(len(grid)))


def analyze_session_open(
    *,
    data_root: Path,
    session_date: date,
    open_time: datetime,
    close_time: datetime,
    next_close: datetime | None,
    contracts: list[dict],
    cfg: argparse.Namespace,
) -> pd.DataFrame:
    equity_quotes = read_lake_dataset(
        data_root, session_date, AssetClass.EQUITY, DataType.QUOTE
    )
    option_quotes = read_lake_dataset(
        data_root, session_date, AssetClass.OPTION, DataType.QUOTE
    )
    option_trades = (
        read_lake_dataset(data_root, session_date, AssetClass.OPTION, DataType.TRADE)
        if option_quotes.num_rows == 0
        else option_quotes
    )
    if equity_quotes.num_rows == 0 or option_trades.num_rows == 0:
        return pd.DataFrame()
    price_source = "quotes" if option_quotes.num_rows else "trades"

    grid = bucket_grid(open_time, cfg.window_minutes, cfg.bucket_seconds)
    bucket_seconds = cfg.bucket_seconds
    open_ns = int(pd.Timestamp(open_time).value)
    elapsed_years = np.arange(len(grid)) * (bucket_seconds / SECONDS_PER_YEAR)
    t_by_dte = {
        0: (close_time - open_time).total_seconds() / SECONDS_PER_YEAR - elapsed_years
    }
    if next_close is not None:
        t_by_dte[1] = (
            next_close - open_time
        ).total_seconds() / SECONDS_PER_YEAR - elapsed_years

    spy = mid_frame(equity_quotes)
    spy_bucket = bucket_last(spy, ["mid"], grid, open_ns, bucket_seconds)["mid"]
    spy_bucket = spy_bucket.ffill(limit=cfg.staleness_buckets)
    if spy_bucket.isna().any():
        return pd.DataFrame()

    if price_source == "quotes":
        options = mid_frame(option_trades)
        options = options[options["spread"] <= cfg.max_spread]
        options = options[options["mid"] >= cfg.min_mid]
        reduce = bucket_last
    else:
        options = trade_frame(option_trades)
        options = options[options["mid"] >= cfg.min_mid]
        reduce = bucket_median
    if options.empty:
        return pd.DataFrame()

    rows: list[pd.DataFrame] = []
    for contract in contracts:
        group = options[options["symbol"] == contract["symbol"]]
        if group.empty:
            continue
        contract_dte = int(contract.get("dte", 0))
        if contract_dte not in t_by_dte:
            continue
        bucket = reduce(group, ["mid", "spread"], grid, open_ns, bucket_seconds)
        bucket = bucket.ffill(limit=cfg.staleness_buckets)
        mids = bucket["mid"].to_numpy(dtype=float)
        if np.isfinite(mids).sum() < cfg.min_coverage * len(grid):
            continue
        spot = spy_bucket.to_numpy(dtype=float)
        strike = float(contract["strike"])
        # The expiry clock starts at the contract's expiration close, minus
        # the elapsed window time.
        t_years = t_by_dte[contract_dte]
        valid = np.isfinite(mids) & np.isfinite(spot)
        if valid.sum() < cfg.min_coverage * len(grid):
            continue
        frame = pd.DataFrame(
            {
                "session": session_date.isoformat(),
                "symbol": contract["symbol"],
                "strike": strike,
                "is_call": contract["type"] == "call",
                "dte": contract_dte,
                "bucket": np.arange(len(grid)),
                "price_source": price_source,
                "mid": mids,
                "spread": bucket["spread"].to_numpy(dtype=float),
                "spot": spot,
                "t_years": t_years,
            }
        )
        frame = frame[valid].copy()
        frame["money"] = [money_bucket(strike, float(value)) for value in frame["spot"]]
        rows.append(frame)
    if not rows:
        return pd.DataFrame()
    nodes = pd.concat(rows, ignore_index=True)

    sigma_path = implied_vol_batch(
        s=nodes["spot"].to_numpy(dtype=float),
        k=nodes["strike"].to_numpy(dtype=float),
        t=nodes["t_years"].to_numpy(dtype=float),
        price=nodes["mid"].to_numpy(dtype=float),
        r=cfg.rate,
        q=cfg.dividend_yield,
        is_call=nodes["is_call"].to_numpy(dtype=bool),
        steps=_STEPS_INVERT,
        iterations=_IV_ITERATIONS,
    )
    nodes["iv"] = sigma_path

    anchor_buckets = max(cfg.anchor_buckets, 1)
    sigma0 = np.full(nodes.shape[0], np.nan)
    keys = nodes["symbol"].to_numpy()
    iv_values = nodes["iv"].to_numpy()
    buckets = nodes["bucket"].to_numpy()
    for symbol in np.unique(keys):
        mask = keys == symbol
        window = mask & (buckets < anchor_buckets)
        if not np.isfinite(iv_values[window]).any():
            window = mask & (buckets < anchor_buckets * 5)
        values = iv_values[window]
        values = values[np.isfinite(values)]
        if values.size:
            sigma0[mask] = float(np.median(values))
    nodes["sigma0"] = sigma0
    nodes["iv_norm"] = nodes["iv"] / nodes["sigma0"]
    return nodes


def bootstrap_ci(
    values_by_session: dict[str, float], *, iterations: int, seed: int
) -> tuple[float, float, float]:
    values = np.array(
        [value for value in values_by_session.values() if np.isfinite(value)]
    )
    if values.size == 0:
        return (float("nan"), float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    samples = rng.choice(values, size=(iterations, values.size), replace=True).mean(
        axis=1
    )
    return (
        float(values.mean()),
        float(np.percentile(samples, 2.5)),
        float(np.percentile(samples, 97.5)),
    )


def aggregate_paths(
    nodes: pd.DataFrame, cfg: argparse.Namespace
) -> tuple[pd.DataFrame, dict]:
    path_frames: list[pd.DataFrame] = []
    extras: dict = {}
    for dte, group in nodes.groupby("dte"):
        dte_key = int(dte)
        focus = group[group["money"] == "atm"] if not group.empty else group
        per_bucket_day = focus.groupby(["bucket", "session"])["iv_norm"].mean()
        path_records: list[dict] = []
        for bucket in sorted(focus["bucket"].unique()):
            try:
                day_values = per_bucket_day.loc[bucket]
            except KeyError:
                continue
            mean, low, high = bootstrap_ci(
                dict(day_values), iterations=cfg.bootstrap_iterations, seed=cfg.seed
            )
            path_records.append(
                {
                    "dte": dte_key,
                    "bucket": int(bucket),
                    "minute": bucket * cfg.bucket_seconds / 60.0,
                    "iv_norm_mean": mean,
                    "iv_norm_lo": low,
                    "iv_norm_hi": high,
                    "days": int(day_values.size),
                }
            )
        path_frames.append(pd.DataFrame(path_records))

        bucket_seconds = cfg.bucket_seconds
        checkpoints: dict = {}
        for minute in cfg.checkpoints:
            index = int(minute * 60 // bucket_seconds)
            rows = [row for row in path_records if row["bucket"] == index]
            if not rows:
                continue
            row = rows[0]
            checkpoints[f"iv_norm_at_{minute}min"] = {
                "mean": row["iv_norm_mean"],
                "lo": row["iv_norm_lo"],
                "hi": row["iv_norm_hi"],
            }

        day_drops: dict[str, float] = {}
        index_15 = int(15 * 60 // bucket_seconds)
        late_mask = focus["bucket"] == index_15
        early_mask = focus["bucket"] < max(cfg.anchor_buckets, 1)
        if late_mask.any() and early_mask.any():
            late = focus[late_mask].groupby("session")["iv_norm"].mean()
            early = focus[early_mask].groupby("session")["iv_norm"].mean()
            common = late.index.intersection(early.index)
            day_drops = {
                str(session): float(late[session] - early[session])
                for session in common
            }
        extras[f"dte_{dte_key}"] = {
            "checkpoints": checkpoints,
            "drop_15min_by_day": day_drops,
        }
    path = pd.concat(path_frames, ignore_index=True) if path_frames else pd.DataFrame()
    return path, extras


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--window-minutes", type=int, default=30)
    parser.add_argument("--bucket-seconds", type=int, default=20)
    parser.add_argument("--rate", type=float, default=0.043)
    parser.add_argument("--dividend-yield", type=float, default=0.012)
    parser.add_argument("--max-spread", type=float, default=0.05)
    parser.add_argument("--min-mid", type=float, default=0.05)
    parser.add_argument("--staleness-buckets", type=int, default=3)
    parser.add_argument("--min-coverage", type=float, default=0.6)
    parser.add_argument("--anchor-buckets", type=int, default=3)
    parser.add_argument(
        "--checkpoints",
        type=float,
        nargs="*",
        default=[1.0, 5.0, 10.0, 15.0, 20.0, 30.0],
    )
    parser.add_argument("--bootstrap-iterations", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--max-sessions", type=int, default=None)
    parser.add_argument("--truth", type=Path, default=None)
    args = parser.parse_args(argv)
    args.data_root = args.data_root.resolve()
    args.out = args.out.resolve()
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    state = load_state(args.data_root)
    sessions = sorted(state.get("sessions", {}))
    if args.max_sessions is not None:
        sessions = sessions[: args.max_sessions]
    if not sessions:
        print("no sessions found in state.json", flush=True)
        return 1
    print(
        f"open-IV test: {len(sessions)} sessions, "
        f"{args.bucket_seconds}s buckets, first {args.window_minutes} minutes",
        flush=True,
    )

    panels: list[pd.DataFrame] = []
    for key in sessions:
        entry = state["sessions"][key]
        contracts = entry.get("contracts", [])
        if not contracts:
            continue
        try:
            open_time = datetime.fromisoformat(entry["open"])
            close_time = datetime.fromisoformat(entry["close"])
        except (KeyError, ValueError):
            print(f"  {key}: missing session bounds, skipped", flush=True)
            continue
        next_close = (
            datetime.fromisoformat(entry["next_close"])
            if entry.get("next_close")
            else None
        )
        frame = analyze_session_open(
            data_root=args.data_root,
            session_date=date.fromisoformat(key),
            open_time=open_time,
            close_time=close_time,
            next_close=next_close,
            contracts=contracts,
            cfg=args,
        )
        if not frame.empty:
            panels.append(frame)
            print(
                f"  {key}: {len(frame)} observations, "
                f"{frame['symbol'].nunique()} contracts",
                flush=True,
            )
    if not panels:
        print("no analyzable sessions", flush=True)
        return 1
    nodes = pd.concat(panels, ignore_index=True)
    path, extras = aggregate_paths(nodes, args)

    args.out.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.Table.from_pandas(nodes, preserve_index=False),
        args.out / "openiv-panel.parquet",
        compression="zstd",
    )
    path.to_csv(args.out / "openiv-path.csv", index=False)
    summary: dict = {
        "config": {
            "window_minutes": args.window_minutes,
            "bucket_seconds": args.bucket_seconds,
            "rate": args.rate,
            "dividend_yield": args.dividend_yield,
            "max_spread": args.max_spread,
            "anchor_buckets": args.anchor_buckets,
            "sessions": int(nodes["session"].nunique()),
            "observations": int(len(nodes)),
        },
        **extras,
    }
    if args.truth is not None and args.truth.is_file():
        summary["truth"] = json.loads(args.truth.read_text(encoding="utf-8"))
    (args.out / "openiv-summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
    )

    print("\nnormalized IV path (ATM), mean with 95% day-bootstrap CI:", flush=True)
    print(path.to_string(index=False), flush=True)
    for key, block in extras.items():
        drops = block["drop_15min_by_day"]
        if not drops:
            continue
        values = np.array(list(drops.values()))
        print(
            f"\n[{key}] IV change over first 15 min (ATM): "
            f"mean {values.mean():+.4f}, median {np.median(values):+.4f}, "
            f"days with drop >1%: {int((values < -0.01).sum())}/{values.size}",
            flush=True,
        )
    print(f"\nwrote {args.out / 'openiv-path.csv'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
