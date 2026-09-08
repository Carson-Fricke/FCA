"""Intraday profile: where movement happens vs where decay happens.

For every half-hour bucket of the session this script computes, per day:

1. **Realized volatility** of SPY from actual 1-minute price moves
   (annualized) and the bucket's **share of the day's total movement**
   (share of realized variance).
2. **Implied volatility** actually quoted by the option market in that
   bucket (mean per-minute implied vol, normalized to the day's mean).
3. **Actual theta decay**: the option value change with the stock-move
   component stripped out (delta-hedged bleed), as percent of option price
   lost per hour, alongside the flat-IV model's predicted decay rate, and
   the bucket's share of the day's total actual decay.

Comparing the movement share with the decay share answers the core
question: is decay proportional to where price movement actually happens,
or is it front-loaded (or back-loaded) regardless of movement?

Inputs: ``panel.parquet`` from ``analyze.py`` (interval-level option
decomposition) plus the SPY equity lake data for realized volatility.

Run:

    uv run --extra dev python scripts/experiments/spy_0dte/intraday_profile.py `
        --data-root .local/spy-0dte/lake --panel .local/spy-0dte/analysis/panel.parquet `
        --out .local/spy-0dte/profile
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
from analyze import (
    bootstrap_over_sessions,
    equity_mid_frame,
    minute_last,
    regress,
    session_grid,
)
from osi import load_state, read_lake_dataset

from lake.contracts.partitions import AssetClass, DataType

_HOURS_PER_YEAR = 365.0 * 24.0


def spy_minute_mids(
    data_root: Path, session_date_key: str, open_time: datetime, close_time: datetime
) -> pd.Series:
    """1-minute SPY mid series for one session."""
    session_date = datetime.fromisoformat(session_date_key).date()
    equity_quotes = read_lake_dataset(
        data_root, session_date, AssetClass.EQUITY, DataType.QUOTE
    )
    if equity_quotes.num_rows == 0:
        return pd.Series(dtype=float)
    frame = equity_mid_frame(equity_quotes)
    grid = session_grid(open_time, close_time)
    mids = minute_last(frame, ["mid"], grid)["mid"]
    return mids.ffill(limit=2)


def realized_profile(mids: pd.Series, bucket_minutes: int) -> pd.DataFrame:
    """Per-bucket realized variance, annualized vol, and day variance share."""
    values = mids.dropna()
    returns = np.log(values).diff().dropna()
    if returns.empty:
        return pd.DataFrame()
    minute_index = (returns.index - returns.index[0]).total_seconds() // 60
    frame = pd.DataFrame(
        {
            "minute": minute_index.astype(int),
            "ret2": np.square(returns.to_numpy(dtype=float)),
        }
    )
    frame["bucket"] = frame["minute"] // bucket_minutes
    day_total = float(frame["ret2"].sum())
    if day_total <= 0:
        return pd.DataFrame()
    grouped = frame.groupby("bucket")["ret2"].agg(["sum", "count"])
    grouped["rv_share"] = grouped["sum"] / day_total
    minutes_per_year = 390.0 * 252.0
    grouped["rv_annual"] = np.sqrt(
        grouped["sum"] / grouped["count"].clip(lower=1) * minutes_per_year
    )
    return grouped.reset_index()


def decay_profile(panel: pd.DataFrame, bucket_minutes: int) -> pd.DataFrame:
    """Per-bucket decay via the robust regression: actual/model multiple.

    Raw delta-hedged means are dominated by trade-print bounce; the
    regression (with intrinsic-floor filtering and MAD outlier rejection)
    is the validated estimator, so decay here = theta_mult x model rate.
    """
    working = panel.copy()
    working["bucket"] = (working["minute"] // bucket_minutes).astype(int)
    working["iv_norm"] = working.groupby("session")["sigma_t0"].transform(
        lambda series: series / series.mean()
    )
    working["weight"] = working["dt_years"]
    working["model_rate_h"] = (
        -working["model_theta_y"] / working["mid0"] / _HOURS_PER_YEAR * 100.0
    )
    records: list[dict] = []
    for (session, bucket), group in working.groupby(["session", "bucket"]):
        stats = regress(group)
        if stats is None:
            continue
        model_rate = float(np.average(group["model_rate_h"], weights=group["weight"]))
        records.append(
            {
                "session": session,
                "bucket": int(bucket),
                "theta_mult": stats["theta_mult"],
                "delta_mult": stats["delta_mult"],
                "model_rate_h": model_rate,
                "iv_norm": float(group["iv_norm"].mean()),
                "intervals": stats["n"],
            }
        )
    return pd.DataFrame(records)


def aggregate(
    merged: pd.DataFrame, bucket_minutes: int, cfg: argparse.Namespace
) -> tuple[pd.DataFrame, float]:
    merged = merged.copy()
    merged["decay_rate_h"] = merged["theta_mult"] * merged["model_rate_h"]
    merged["decay_amount"] = merged["decay_rate_h"] * bucket_minutes / 60.0
    merged["model_amount"] = merged["model_rate_h"].abs() * bucket_minutes / 60.0
    day_totals = merged.groupby("session")["decay_amount"].transform("sum")
    merged["decay_share"] = np.where(
        day_totals > 0, merged["decay_amount"] / day_totals, np.nan
    )
    model_day_totals = merged.groupby("session")["model_amount"].transform("sum")
    merged["model_share"] = np.where(
        model_day_totals > 0, merged["model_amount"] / model_day_totals, np.nan
    )
    metrics = (
        "rv_share",
        "rv_annual",
        "iv_norm",
        "theta_mult",
        "delta_mult",
        "decay_rate_h",
        "decay_share",
        "model_share",
    )
    records: list[dict] = []
    for bucket, group in merged.groupby("bucket"):
        entry: dict = {
            "bucket": int(bucket),
            "minutes_from_open": f"{bucket * bucket_minutes}-{(bucket + 1) * bucket_minutes}",
            "sessions": int(group["session"].nunique()),
        }
        for metric in metrics:
            per_day = group.groupby("session")[metric].mean()
            mean, low, high = bootstrap_over_sessions(
                dict(per_day), iterations=cfg.bootstrap_iterations, seed=cfg.seed
            )
            entry[metric] = mean
            entry[f"{metric}_lo"] = low
            entry[f"{metric}_hi"] = high
        records.append(entry)
    table = pd.DataFrame(records)
    # Movement-vs-decay coupling across buckets (equal-weight bucket means).
    valid = table.dropna(subset=["rv_share", "decay_share"])
    coupling = (
        float(np.corrcoef(valid["rv_share"], valid["decay_share"])[0, 1])
        if len(valid) >= 3
        else float("nan")
    )
    return table, coupling


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--panel", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--bucket-minutes", type=int, default=30)
    parser.add_argument("--dte", type=int, default=0, choices=(0, 1))
    parser.add_argument("--bootstrap-iterations", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args(argv)
    args.data_root = args.data_root.resolve()
    args.panel = args.panel.resolve()
    args.out = args.out.resolve()
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.panel.is_file():
        print(f"panel not found at {args.panel}; run analyze.py first", flush=True)
        return 1
    panel = pd.read_parquet(args.panel)
    if "dte" in panel.columns:
        panel = panel[panel["dte"] == args.dte]
        if panel.empty:
            print(f"no panel rows for dte={args.dte}", flush=True)
            return 1
    state = load_state(args.data_root)

    realized_frames: list[pd.DataFrame] = []
    for key in sorted(state.get("sessions", {})):
        entry = state["sessions"][key]
        if entry.get("datasets", {}).get("equity/quote") != "done":
            continue
        open_time = datetime.fromisoformat(entry["open"])
        close_time = datetime.fromisoformat(entry["close"])
        mids = spy_minute_mids(args.data_root, key, open_time, close_time)
        profile = realized_profile(mids, args.bucket_minutes)
        if profile.empty:
            continue
        profile["session"] = key
        realized_frames.append(profile)
    if not realized_frames:
        print("no realized-volatility data", flush=True)
        return 1
    realized = pd.concat(realized_frames, ignore_index=True)

    decay = decay_profile(panel, args.bucket_minutes)
    merged = realized.merge(
        decay[
            ["session", "bucket", "theta_mult", "delta_mult", "model_rate_h", "iv_norm"]
        ],
        on=["session", "bucket"],
        how="inner",
    )
    if merged.empty:
        print("no overlap between realized and decay profiles", flush=True)
        return 1

    table, coupling = aggregate(merged, args.bucket_minutes, cfg=args)

    args.out.mkdir(parents=True, exist_ok=True)
    table.to_csv(args.out / "intraday-profile.csv", index=False)
    summary = {
        "bucket_minutes": args.bucket_minutes,
        "sessions": int(merged["session"].nunique()),
        "movement_decay_coupling": coupling,
        "buckets": table.to_dict(orient="records"),
    }
    (args.out / "intraday-summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
    )

    columns = [
        "minutes_from_open",
        "sessions",
        "rv_annual",
        "rv_share",
        "iv_norm",
        "decay_rate_h",
        "theta_mult",
        "delta_mult",
        "decay_share",
    ]
    print(
        f"\nintraday profile ({args.bucket_minutes}-min buckets, "
        f"{merged['session'].nunique()} days, "
        f"movement-vs-decay coupling r = {coupling:+.2f}):",
        flush=True,
    )
    print(
        table[columns].to_string(index=False, float_format=lambda v: f"{v:.3f}"),
        flush=True,
    )
    print(f"\nwrote {args.out / 'intraday-profile.csv'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
