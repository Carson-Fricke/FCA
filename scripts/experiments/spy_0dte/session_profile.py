"""Per-minute intraday profile for the UI (SPY 0DTE/1DTE experiment).

Aggregates the backward-strip interval panel into one data point per minute
of the trading session (0-389), per expiry group and per option side:

- mean implied volatility (absolute, from the per-node IV inversions)
- mean option price (trade-print median)
- realized theta: actual time-component bleed in cents per hour with the
  model's cents-per-hour as reference (both from the current-IV pricing)
- realized delta movement: mean absolute spot component in cents with the
  model's mean absolute spot-driven move as reference
- realized vol component: signed mean cents per interval (the IV repricing)
- the underlying's share of the day's realized price movement per minute

Pools all sessions per minute (thousands of intervals per point). Writes the
JavaScript fixture consumed by the thesis UI demo page:

    src/ui/src/lib/data/spy0dteProfile.js

Run:

    uv run --extra dev python scripts/experiments/spy_0dte/session_profile.py `
        --data-root .local/spy-0dte/lake --panel .local/spy-0dte/analysis/panel.parquet
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pandas as pd
from intraday_profile import realized_profile, spy_minute_mids
from osi import load_state

_SMOOTH_WINDOW = 9
_NOMINAL_OPEN_UTC = datetime(2026, 9, 7, 13, 30)  # 9:30 ET on a nominal day
_HOURS_PER_YEAR = 24.0 * 365.0


def pooled_minute_stats(panel: pd.DataFrame, dte: int) -> dict:
    """Per-(minute, side) pooled statistics for one expiry group."""
    group_frame = panel[
        (panel["dte"] == dte) & (panel["money"].isin(["atm", "near"]))
    ].copy()
    working = group_frame
    # Garbage IV inversions and 0/0-unstable tree greeks near the close.
    working.loc[working["sigma_t0"] > 1.5, "sigma_t0"] = np.nan
    working = working[
        (working["model_delta"].abs() <= 1.2) & (working["model_theta_y"] <= 0)
    ]
    # Anchor-based model theta is the flat-IV reference; drop nodes whose
    # per-interval anchor bleed is implausible (T -> 0 blowups).
    anchor_interval = working["model_theta_anchor_y"] * working["dt_years"]
    working = working[anchor_interval.abs() <= 1.0]
    stats: dict[tuple[int, bool], dict] = {}
    for (minute, is_call), group in working.groupby(["minute", "is_call"]):
        iv_values = group["sigma_t0"].dropna()
        stats[(int(minute), bool(is_call))] = {
            "n": int(len(group)),
            "iv": float(iv_values.mean()) if len(iv_values) else np.nan,
            "price": float(group["mid0"].mean()),
            # Actual theta bleed in cents per hour (dt-weighted sums).
            "theta_cents_h": float(
                -group["theta_component"].sum()
                / group["dt_years"].sum()
                * 100.0
                / _HOURS_PER_YEAR
            ),
            "model_theta_cents_h": float(
                -np.average(group["model_theta_anchor_y"], weights=group["dt_years"])
                * 100.0
                / _HOURS_PER_YEAR
            ),
            # Realized delta movement: mean absolute spot component in cents,
            # against the model's mean absolute spot-driven move.
            "delta_cents": float(group["delta_component"].abs().mean() * 100.0),
            "model_delta_cents": float(group["delta_move"].abs().mean() * 100.0),
            # Fixed SMA-30 IV view: both endpoints priced at the same vol.
            "theta_fixed_cents_h": float(
                -group["theta_component_sma"].sum()
                / group["dt_years"].sum()
                * 100.0
                / _HOURS_PER_YEAR
            ),
            "delta_fixed_cents": float(
                group["delta_component_sma"].abs().mean() * 100.0
            ),
            # Movement-driven IV view: both endpoints at the movement-scaled IV.
            "theta_move_cents_h": float(
                -group["theta_component_move"].sum()
                / group["dt_years"].sum()
                * 100.0
                / _HOURS_PER_YEAR
            ),
            "delta_move_view_cents": float(
                group["delta_component_move"].abs().mean() * 100.0
            ),
            # Signed mean vol repricing in cents per interval.
            "vol_cents": float(group["vol_component"].mean() * 100.0),
        }
    return stats


def side_series(stats: dict, side: bool, minutes: list[int]) -> dict[str, list[float]]:
    """Assemble per-minute series for one option side."""
    keys = [(minute, side) for minute in minutes]
    series: dict[str, list[float]] = {
        key: []
        for key in (
            "iv",
            "price",
            "theta",
            "modelTheta",
            "delta",
            "modelDelta",
            "thetaFixed",
            "deltaFixed",
            "thetaMove",
            "deltaMove",
            "vol",
        )
    }
    for key in keys:
        entry = stats.get(key)
        for name, field in (
            ("iv", "iv"),
            ("price", "price"),
            ("theta", "theta_cents_h"),
            ("modelTheta", "model_theta_cents_h"),
            ("delta", "delta_cents"),
            ("modelDelta", "model_delta_cents"),
            ("thetaFixed", "theta_fixed_cents_h"),
            ("deltaFixed", "delta_fixed_cents"),
            ("thetaMove", "theta_move_cents_h"),
            ("deltaMove", "delta_move_view_cents"),
            ("vol", "vol_cents"),
        ):
            value = entry[field] if entry else np.nan
            series[name].append(value)
    return series


def smooth(values: list[float], window: int = _SMOOTH_WINDOW) -> list[float]:
    series = pd.Series(values, dtype=float).replace([np.inf, -np.inf], np.nan)
    filled = series.interpolate(limit_direction="both")
    smoothed = filled.rolling(
        window, center=True, min_periods=max(2, window // 3)
    ).mean()
    return [float(value) if np.isfinite(value) else None for value in smoothed]


_WINSOR = {
    "theta": (0.0, 400.0),
    "modelTheta": (0.0, 400.0),
    "delta": (0.0, 50.0),
    "modelDelta": (0.0, 50.0),
    "thetaFixed": (0.0, 400.0),
    "deltaFixed": (0.0, 50.0),
    "thetaMove": (0.0, 400.0),
    "deltaMove": (0.0, 50.0),
    "vol": (-5.0, 5.0),
}


def winsor(series_name: str, values: list[float]) -> list[float]:
    low, high = _WINSOR.get(series_name, (-np.inf, np.inf))
    return [
        min(max(value, low), high) if np.isfinite(value) else np.nan for value in values
    ]


def movement_share(data_root: Path) -> dict:
    """Per-minute share of the day's realized variance, averaged over days."""
    state = load_state(data_root)
    shares: dict[int, list[float]] = {}
    annual: dict[int, list[float]] = {}
    for key in sorted(state.get("sessions", {})):
        entry = state["sessions"][key]
        if entry.get("datasets", {}).get("equity/quote") != "done":
            continue
        mids = spy_minute_mids(
            data_root,
            key,
            datetime.fromisoformat(entry["open"]),
            datetime.fromisoformat(entry["close"]),
        )
        profile = realized_profile(mids, 1)
        if profile.empty:
            continue
        for _, row in profile.iterrows():
            shares.setdefault(int(row["bucket"]), []).append(float(row["rv_share"]))
            annual.setdefault(int(row["bucket"]), []).append(float(row["rv_annual"]))
    minutes = sorted(shares)
    return {
        "minutes": minutes,
        "share": [float(np.mean(shares[minute])) * 100.0 for minute in minutes],
        "rvAnnual": [float(np.mean(annual[minute])) for minute in minutes],
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--panel", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    args.data_root = args.data_root.resolve()
    args.panel = args.panel.resolve()
    args.out = args.out.resolve()
    return args


def to_points(minutes: list[int], values: list[float | None]) -> list[dict]:
    base = int(_NOMINAL_OPEN_UTC.timestamp() * 1000)
    points = []
    for minute, value in zip(minutes, values, strict=True):
        if value is None or not np.isfinite(value):
            continue
        points.append({"t": base + minute * 60_000, "v": round(float(value), 6)})
    return points


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    panel = pd.read_parquet(args.panel)

    groups: dict[int, dict] = {}
    minute_axis: list[int] | None = None
    for dte in (0, 1):
        if dte not in panel["dte"].unique():
            continue
        stats = pooled_minute_stats(panel, dte)
        minutes = sorted({minute for minute, _ in stats})
        groups[dte] = {
            "minutes": minutes,
            "call": side_series(stats, True, minutes),
            "put": side_series(stats, False, minutes),
        }
        if minute_axis is None or len(minutes) > len(minute_axis):
            minute_axis = minutes
    assert minute_axis is not None

    share = movement_share(args.data_root)

    def chart(group: dict, side: str, field: str) -> list[dict]:
        raw = winsor(field, group[side][field])
        return to_points(group["minutes"], smooth(raw))

    fixture = {
        "meta": {
            "sessions": int(panel["session"].nunique()),
            "firstSession": str(sorted(panel["session"].unique())[0]),
            "lastSession": str(sorted(panel["session"].unique())[-1]),
            "priceSource": "trade prints (median per minute)",
            "smoothing": "centered 9-minute mean",
            "method": "backward strip at current IV",
            "generated": datetime.now(UTC).isoformat(),
        },
        "iv": {
            "dte0": to_points(groups[0]["minutes"], smooth(groups[0]["call"]["iv"])),
            "dte1": to_points(groups[1]["minutes"], smooth(groups[1]["call"]["iv"])),
        },
        "price": {
            "dte0": to_points(groups[0]["minutes"], smooth(groups[0]["call"]["price"])),
            "dte1": to_points(groups[1]["minutes"], smooth(groups[1]["call"]["price"])),
        },
        "theta": {
            "dte0": {
                "call": chart(groups[0], "call", "theta"),
                "put": chart(groups[0], "put", "theta"),
                "modelCall": chart(groups[0], "call", "modelTheta"),
                "modelPut": chart(groups[0], "put", "modelTheta"),
            },
            "dte1": {
                "call": chart(groups[1], "call", "theta"),
                "put": chart(groups[1], "put", "theta"),
                "modelCall": chart(groups[1], "call", "modelTheta"),
                "modelPut": chart(groups[1], "put", "modelTheta"),
            },
        },
        "delta": {
            "dte0": {
                "call": chart(groups[0], "call", "delta"),
                "put": chart(groups[0], "put", "delta"),
                "modelCall": chart(groups[0], "call", "modelDelta"),
                "modelPut": chart(groups[0], "put", "modelDelta"),
            },
            "dte1": {
                "call": chart(groups[1], "call", "delta"),
                "put": chart(groups[1], "put", "delta"),
                "modelCall": chart(groups[1], "call", "modelDelta"),
                "modelPut": chart(groups[1], "put", "modelDelta"),
            },
        },
        "thetaFixed": {
            "dte0": {
                "call": chart(groups[0], "call", "thetaFixed"),
                "put": chart(groups[0], "put", "thetaFixed"),
            },
            "dte1": {
                "call": chart(groups[1], "call", "thetaFixed"),
                "put": chart(groups[1], "put", "thetaFixed"),
            },
        },
        "deltaFixed": {
            "dte0": {
                "call": chart(groups[0], "call", "deltaFixed"),
                "put": chart(groups[0], "put", "deltaFixed"),
            },
            "dte1": {
                "call": chart(groups[1], "call", "deltaFixed"),
                "put": chart(groups[1], "put", "deltaFixed"),
            },
        },
        "thetaMove": {
            "dte0": {
                "call": chart(groups[0], "call", "thetaMove"),
                "put": chart(groups[0], "put", "thetaMove"),
            },
            "dte1": {
                "call": chart(groups[1], "call", "thetaMove"),
                "put": chart(groups[1], "put", "thetaMove"),
            },
        },
        "deltaMove": {
            "dte0": {
                "call": chart(groups[0], "call", "deltaMove"),
                "put": chart(groups[0], "put", "deltaMove"),
            },
            "dte1": {
                "call": chart(groups[1], "call", "deltaMove"),
                "put": chart(groups[1], "put", "deltaMove"),
            },
        },
        "vol": {
            "dte0": {
                "call": chart(groups[0], "call", "vol"),
                "put": chart(groups[0], "put", "vol"),
            },
            "dte1": {
                "call": chart(groups[1], "call", "vol"),
                "put": chart(groups[1], "put", "vol"),
            },
        },
        "movement": {
            "share": to_points(share["minutes"], smooth(share["share"])),
            "rvAnnual": to_points(share["minutes"], smooth(share["rvAnnual"])),
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        "/* Generated by scripts/experiments/spy_0dte/session_profile.py - do not edit. */\n"
        "export const profile = " + json.dumps(fixture, indent=1) + ";\n",
        encoding="utf-8",
    )
    print(f"wrote {args.out}", flush=True)
    print(
        f"dte0 minutes: {len(groups[0]['minutes'])}, "
        f"dte1 minutes: {len(groups[1]['minutes'])}, "
        f"movement minutes: {len(share['minutes'])}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
