"""Wavelet regime analysis of SPY at the daily timeframe.

Synthesizes daily bars from the cached 1-minute SPY closes (OHLC from
intraday closes; note highs/lows are slightly understated without native
daily H/L) and runs two daily-timeframe wavelet studies over the full
~1,500-session history:

1. **Daily log-returns** — the price-regime signal. MODWT (LA8, 6 levels:
   D1 2-4 days ... D6 64-128 days, S6 128+ days) partitions each day's
   variance across week-to-quarter timescales; the smooth component is the
   multi-month trend state.
2. **Daily realized volatility** (annualized, from 1-min returns) — the
   vol-regime signal. Volatility clusters at multi-week scales; the
   analytic-signal phase of the D4/D5 vol details locates each day within
   its vol cycle.

Outputs under --out:

- daily-variance.csv   per-level wavelet variance, shares, and counts
- daily-mra.parquet   per-day MRA components D1..D6 + smooth (both signals)
- daily-regimes.csv    per-day regime table: trend state, dominant scale,
                       vol percentile, vol-cycle phase features
- summary.json         config, variance tables, vol-cycle phase stats

Run:

    uv run --extra dev python scripts/experiments/spy_0dte/wavelet_daily.py `
        --bar-cache .local/spy-clusters/bar-cache --out .local/spy-clusters/wavelet-daily
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from harness.signal.analytic import analytic_signal, phase_coherence
from harness.signal.wavelets import modwt, modwt_mra, valid_mask, wavelet_variance

_MINUTES_PER_SESSION = 390
_SESSIONS_PER_YEAR = 252.0
_LEVELS = 6
_ROLLING_WINDOW = 512
_ROLLING_LEVELS = 5
_ROLLING_STEP = 21
_VOL_LEVELS = (4, 5)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--bar-cache", type=Path, default=Path(".local/spy-clusters/bar-cache")
    )
    parser.add_argument(
        "--out", type=Path, default=Path(".local/spy-clusters/wavelet-daily")
    )
    parser.add_argument("--wavelet", type=str, default="la8")
    parser.add_argument("--levels", type=int, default=_LEVELS)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args(argv)
    args.bar_cache = args.bar_cache.resolve()
    args.out = args.out.resolve()
    return args


def load_sessions(cache_dir: Path) -> pd.DataFrame:
    """Daily OHLC + realized vol synthesized from cached 1-min closes."""
    records = []
    for path in sorted(cache_dir.glob("*.json")):
        closes = np.asarray(json.loads(path.read_text(encoding="utf-8")), dtype=float)
        closes = closes[closes > 0.0]
        if closes.size < 60:
            continue
        log_returns = np.diff(np.log(closes))
        realized_vol = float(
            np.sqrt(np.sum(np.square(log_returns)) * _SESSIONS_PER_YEAR)
        )
        records.append(
            {
                "session": path.stem,
                "open": float(closes[0]),
                "high": float(closes.max()),
                "low": float(closes.min()),
                "close": float(closes[-1]),
                "bars": int(closes.size),
                "realized_vol": realized_vol,
            }
        )
    frame = pd.DataFrame(records).sort_values("session").reset_index(drop=True)
    print(f"synthesized {len(frame)} daily bars from {cache_dir}", flush=True)
    return frame


def wavelet_summary(series: np.ndarray, wavelet: str, levels: int) -> pd.DataFrame:
    """Per-level variance table for one daily signal."""
    result = modwt(series, wavelet=wavelet, level=levels, boundary="reflection")
    variance = wavelet_variance(result)
    shares = variance.shares
    rows = []
    for level in range(levels):
        rows.append(
            {
                "component": f"D{level + 1}",
                "period_days": f"{2 ** (level + 1)}-{2 ** (level + 2)}",
                "variance": float(variance.variance[level]),
                "share": float(shares[level]) if np.isfinite(shares[level]) else np.nan,
                "count": int(variance.counts[level]),
            }
        )
    smooth_mask = valid_mask(series.size, result.boundary_widths[-1])
    smooth_values = result.scaling[smooth_mask]
    smooth_variance = float(np.mean(np.square(smooth_values - smooth_values.mean())))
    rows.append(
        {
            "component": f"S{levels}",
            "period_days": f">= {2**levels}",
            "variance": smooth_variance,
            "share": float(
                smooth_variance / (variance.variance.sum() + smooth_variance)
            ),
            "count": int(smooth_mask.sum()),
        }
    )
    return pd.DataFrame(rows)


def mra_frame(
    sessions: pd.DataFrame, series: np.ndarray, prefix: str, wavelet: str, levels: int
) -> pd.DataFrame:
    """Per-day MRA components of one daily signal, boundary-flagged."""
    mra = modwt_mra(series, wavelet=wavelet, level=levels, boundary="reflection")
    frame = pd.DataFrame({"session": sessions["session"]})
    for level, detail in enumerate(mra.details):
        frame[f"{prefix}_d{level + 1}"] = detail
    frame[f"{prefix}_smooth"] = mra.smooth
    widths = mra.boundary_widths[-1]
    frame[f"{prefix}_boundary"] = np.zeros(len(frame), dtype=bool)
    frame.loc[: widths - 1, f"{prefix}_boundary"] = True
    frame.loc[len(frame) - widths :, f"{prefix}_boundary"] = True
    return frame


def rolling_variance_shares(
    series: np.ndarray, wavelet: str, window: int, levels: int, step: int
) -> pd.DataFrame:
    """Rolling wavelet-variance partition at :param:`window` lookback."""
    rows = []
    for end in range(window, series.size + 1, step):
        window_series = series[end - window : end]
        result = modwt(
            window_series, wavelet=wavelet, level=levels, boundary="reflection"
        )
        variance = wavelet_variance(result)
        total = float(np.nansum(variance.variance))
        row: dict = {"session": end - 1}
        for level in range(levels):
            share = variance.variance[level] / total if total > 0 else np.nan
            row[f"share_d{level + 1}"] = float(share)
        row["dominant_scale"] = int(np.nanargmax(variance.variance) + 1)
        rows.append(row)
    return pd.DataFrame(rows)


def vol_cycle_features(
    vol_series: np.ndarray, wavelet: str, levels: tuple[int, ...]
) -> dict[str, float]:
    """Where the latest day sits in its multi-week vol cycle."""
    features: dict[str, float] = {}
    mra = modwt_mra(
        vol_series, wavelet=wavelet, level=max(levels), boundary="reflection"
    )
    for level in levels:
        detail = mra.details[level - 1]
        mask = valid_mask(vol_series.size, mra.boundary_widths[level - 1])
        envelope, phase = analytic_signal(detail)
        masked_phase = np.where(mask, phase, np.nan)
        finite = masked_phase[np.isfinite(masked_phase)]
        features[f"vol_phase_velocity_d{level}"] = (
            float(np.nanmean(np.diff(np.unwrap(finite)))) if finite.size > 2 else np.nan
        )
        features[f"vol_phase_coherence_d{level}"] = phase_coherence(finite)
        clean_envelope = envelope[mask]
        features[f"vol_envelope_percentile_d{level}"] = (
            float(np.mean(clean_envelope < envelope[-1]))
            if clean_envelope.size > 0
            else np.nan
        )
    return features


def main(argv: list[str] | None = None) -> int:
    cfg = parse_args(argv)
    sessions = load_sessions(cfg.bar_cache)
    if len(sessions) < 400:
        print("not enough sessions for daily wavelet analysis", flush=True)
        return 1

    close = sessions["close"].to_numpy(dtype=float)
    daily_returns = np.diff(np.log(close))
    return_sessions = sessions["session"].iloc[1:].to_numpy()
    realized_vol = sessions["realized_vol"].to_numpy(dtype=float)
    log_vol = np.log(np.maximum(realized_vol, 1e-8))

    print(f"MODWT {cfg.wavelet} level {cfg.levels} on daily returns...", flush=True)
    return_variance = wavelet_summary(daily_returns, cfg.wavelet, cfg.levels)
    vol_variance = wavelet_summary(log_vol, cfg.wavelet, cfg.levels)
    print(return_variance.to_string(index=False), flush=True)

    return_mra = mra_frame(
        sessions.iloc[1:], daily_returns, "ret", cfg.wavelet, cfg.levels
    )
    vol_mra = mra_frame(sessions, log_vol, "vol", cfg.wavelet, cfg.levels)

    print(f"rolling {_ROLLING_WINDOW}-day variance partition...", flush=True)
    rolling = rolling_variance_shares(
        daily_returns, cfg.wavelet, _ROLLING_WINDOW, _ROLLING_LEVELS, _ROLLING_STEP
    )
    rolling["session"] = return_sessions[rolling["session"].to_numpy(dtype=int)]

    print("vol-cycle phase features...", flush=True)
    cycle = vol_cycle_features(log_vol, cfg.wavelet, _VOL_LEVELS)
    for key, value in cycle.items():
        print(f"  {key}: {value:+.4f}", flush=True)

    regimes = vol_mra.merge(rolling, on="session", how="left")
    rolling_columns = [f"share_d{level}" for level in range(1, _ROLLING_LEVELS + 1)]
    rolling_columns.append("dominant_scale")
    regimes[rolling_columns] = regimes[rolling_columns].ffill()
    latest_vol_percentile = float(np.mean(realized_vol < realized_vol[-1]))
    regimes["rv_percentile"] = pd.Series(realized_vol).rank(pct=True).to_numpy()
    smooth = regimes["vol_smooth"].to_numpy(dtype=float)
    regimes["trend_state"] = np.where(
        smooth > np.nanpercentile(smooth, 66),
        "high",
        np.where(smooth < np.nanpercentile(smooth, 33), "low", "mid"),
    )
    regimes["vol_state"] = np.where(
        realized_vol > np.nanpercentile(realized_vol, 66),
        "high",
        np.where(realized_vol < np.nanpercentile(realized_vol, 33), "low", "mid"),
    )

    merged = return_mra.merge(regimes, on="session", how="left", suffixes=("_ret", ""))
    cfg.out.mkdir(parents=True, exist_ok=True)
    return_variance.to_csv(cfg.out / "daily-variance.csv", index=False)
    vol_variance.to_csv(cfg.out / "vol-variance.csv", index=False)
    pq.write_table(
        pa.Table.from_pandas(merged, preserve_index=False),
        cfg.out / "daily-mra.parquet",
        compression="zstd",
    )
    rolling.to_csv(cfg.out / "rolling-variance.csv", index=False)
    summary = {
        "config": {
            "wavelet": cfg.wavelet,
            "levels": cfg.levels,
            "sessions": int(len(sessions)),
            "window": _ROLLING_WINDOW,
        },
        "return_variance": return_variance.to_dict(orient="records"),
        "vol_variance": vol_variance.to_dict(orient="records"),
        "vol_cycle": cycle,
        "latest": {
            "session": str(sessions["session"].iloc[-1]),
            "rv_percentile": latest_vol_percentile,
            "trend_state": str(regimes["trend_state"].iloc[-1]),
            "vol_state": str(regimes["vol_state"].iloc[-1]),
            "dominant_scale": int(rolling["dominant_scale"].iloc[-1]),
        },
    }
    (cfg.out / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(f"\nwrote {cfg.out / 'summary.json'}", flush=True)
    print(return_variance.to_string(index=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
