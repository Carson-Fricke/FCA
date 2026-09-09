"""Cross-timeframe merge: intraday clusters conditioned on daily regimes.

Joins the intraday wavelet clustering (features.parquet from
wavelet_intraday.py) with the daily regime table (daily-mra.parquet from
wavelet_daily.py) and measures how intraday day-types are distributed
across daily-timeframe regimes:

- cluster x trend-state and cluster x vol-state cross-tabs with row shares
- regime lift: how much more (or less) often each intraday cluster occurs
  inside a daily trend regime than its base rate
- per-cluster mean trend level, dominant daily scale, and vol percentile
- an overlay table attaching both angles to the SPY 0DTE lake sessions for
  later cross-reference with the theta/IV decomposition studies

Run:

    uv run --extra dev python scripts/experiments/spy_0dte/cross_timeframe.py `
        --intraday .local/spy-clusters/wavelet/features.parquet `
        --daily .local/spy-clusters/wavelet-daily/daily-mra.parquet `
        --out .local/spy-clusters/cross-timeframe `
        --data-root .local/spy-0dte/lake
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from osi import load_state

_RET_SMOOTH = "ret_smooth"
_TREND_COLUMN = "trend_state"
_VOL_COLUMN = "vol_state"
_SCALE_COLUMN = "dominant_scale"
_RV_COLUMN = "rv_percentile"
_SHAPE_PROFILES = (
    "block_morning_d3",
    "block_midday_d3",
    "block_afternoon_d3",
    "block_power_d3",
    "block_midday_d4",
    "phase_speed_open_d4",
    "phase_speed_midday_d3",
    "phase_coherence_d3",
    "envelope_cv_d3",
    "smooth_efficiency",
    "smooth_argmax",
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--intraday",
        type=Path,
        default=Path(".local/spy-clusters/wavelet/features.parquet"),
    )
    parser.add_argument(
        "--daily",
        type=Path,
        default=Path(".local/spy-clusters/wavelet-daily/daily-mra.parquet"),
    )
    parser.add_argument(
        "--out", type=Path, default=Path(".local/spy-clusters/cross-timeframe")
    )
    parser.add_argument("--data-root", type=Path, default=None)
    args = parser.parse_args(argv)
    args.intraday = args.intraday.resolve()
    args.daily = args.daily.resolve()
    args.out = args.out.resolve()
    if args.data_root is not None:
        args.data_root = args.data_root.resolve()
    return args


def load_joined(intraday_path: Path, daily_path: Path) -> pd.DataFrame:
    """Inner join of both study tables on session date."""
    intraday = pq.read_table(intraday_path).to_pandas()
    daily_columns = [
        "session",
        _RET_SMOOTH,
        _TREND_COLUMN,
        _VOL_COLUMN,
        _SCALE_COLUMN,
        _RV_COLUMN,
    ]
    daily = pq.read_table(daily_path, columns=daily_columns).to_pandas()
    joined = intraday.merge(daily, on="session", how="inner")
    joined = joined.dropna(subset=[_RET_SMOOTH, _TREND_COLUMN, _VOL_COLUMN])
    print(
        f"joined {len(joined)} sessions (intraday {len(intraday)}, daily {len(daily)})",
        flush=True,
    )
    return joined


def cross_tab(joined: pd.DataFrame, rows: str, columns: str) -> dict:
    counts = pd.crosstab(joined[rows], joined[columns])
    shares = counts.div(counts.sum(axis=1), axis=0)
    return {
        "counts": {
            str(a): {str(b): int(v) for b, v in row.items()}
            for a, row in counts.iterrows()
        },
        "row_shares": {
            str(a): {str(b): float(v) for b, v in row.items()}
            for a, row in shares.iterrows()
        },
    }


def regime_lift(joined: pd.DataFrame, regime: str) -> dict:
    """Cluster occurrence inside a regime relative to its base rate."""
    labels = joined["label"]
    base_rate = {
        int(label): float((labels == label).mean()) for label in sorted(set(labels))
    }
    lift: dict[str, dict[str, float]] = {}
    for state in sorted(set(joined[regime])):
        inside = joined[joined[regime] == state]
        rate = {
            int(label): float((inside["label"] == label).mean()) for label in base_rate
        }
        lift[str(state)] = {
            str(label): float(rate[label] / base_rate[label])
            if base_rate[label] > 0
            else np.nan
            for label in sorted(base_rate)
        }
    return {"base_rate": {str(k): v for k, v in base_rate.items()}, "lift": lift}


def per_cluster_summary(joined: pd.DataFrame) -> list[dict]:
    rows = []
    for label in sorted(set(joined["label"])):
        members = joined[joined["label"] == label]
        rows.append(
            {
                "label": int(label),
                "days": int(len(members)),
                "mean_trend_level": float(members[_RET_SMOOTH].mean()),
                "mean_rv_percentile": float(members[_RV_COLUMN].mean()),
                "dominant_scale_mode": (
                    int(members[_SCALE_COLUMN].mode().iloc[0])
                    if members[_SCALE_COLUMN].notna().any()
                    else None
                ),
            }
        )
    return rows


def regime_shape_profiles(joined: pd.DataFrame) -> dict:
    """Mean intraday shape features per daily regime state.

    With shape-cluster labels degenerate (all noise), this table answers
    the cross-timeframe question directly: does the daily regime state
    manifest in the intraday multi-scale shape of the day?
    """
    profiles: dict = {}
    available = [c for c in _SHAPE_PROFILES if c in joined.columns]
    for regime in (_TREND_COLUMN, _VOL_COLUMN):
        table: dict = {}
        for state in sorted(set(joined[regime])):
            members = joined[joined[regime] == state]
            table[str(state)] = {
                "days": int(len(members)),
                **{column: float(members[column].mean()) for column in available},
            }
        profiles[regime] = table
    return profiles


def zero_dte_overlay(joined: pd.DataFrame, data_root: Path) -> pd.DataFrame | None:
    """Both timeframe angles for the 0DTE lake sessions."""
    state = load_state(data_root)
    lake_sessions = sorted(state.get("sessions", {}))
    if not lake_sessions:
        print(f"no sessions in lake state {data_root}", flush=True)
        return None
    overlay_columns = [
        "session",
        "label",
        "probability",
        _RET_SMOOTH,
        _TREND_COLUMN,
        _VOL_COLUMN,
        _SCALE_COLUMN,
        _RV_COLUMN,
    ]
    available = joined[joined["session"].isin(lake_sessions)]
    print(
        f"0DTE lake: {len(lake_sessions)} sessions, {len(available)} covered by cache",
        flush=True,
    )
    if available.empty:
        return None
    return available[overlay_columns].reset_index(drop=True)


def main(argv: list[str] | None = None) -> int:
    cfg = parse_args(argv)
    joined = load_joined(cfg.intraday, cfg.daily)
    if joined.empty:
        print("no overlap between studies", flush=True)
        return 1

    summary = {
        "sessions": int(len(joined)),
        "cluster_x_trend": cross_tab(joined, "label", _TREND_COLUMN),
        "cluster_x_vol": cross_tab(joined, "label", _VOL_COLUMN),
        "trend_lift": regime_lift(joined, _TREND_COLUMN),
        "vol_lift": regime_lift(joined, _VOL_COLUMN),
        "regime_shape_profiles": regime_shape_profiles(joined),
        "per_cluster": per_cluster_summary(joined),
    }
    cfg.out.mkdir(parents=True, exist_ok=True)
    (cfg.out / "cross_timeframe.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
    )

    if cfg.data_root is not None:
        overlay = zero_dte_overlay(joined, cfg.data_root)
        if overlay is not None:
            pq.write_table(
                pa.Table.from_pandas(overlay, preserve_index=False),
                cfg.out / "zero_dte_overlay.parquet",
                compression="zstd",
            )
            print(f"wrote {cfg.out / 'zero_dte_overlay.parquet'}", flush=True)

    print(f"\nwrote {cfg.out / 'cross_timeframe.json'}", flush=True)
    per_cluster = pd.DataFrame(summary["per_cluster"])
    print(per_cluster.to_string(index=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
