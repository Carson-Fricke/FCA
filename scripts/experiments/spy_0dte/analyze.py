"""Backward-strip decomposition analysis for SPY 0DTE options (experiment).

For every contract-minute the analysis:

1. Builds a 1-minute option-price panel from the tick lake (raw tick data is
   preserved; the panel is the analysis view) alongside the SPY mid series.
2. Inverts each node's **current implied volatility** from the observed
   price, so the model reprices the option exactly at every observation.
3. For every consecutive valid minute interval, runs the observed change
   **backward** at the interval's opening IV: from the observed end price,
   remove the vol move, then the time move, then the spot move:
   - vol component   = mid1 - C(S1, T1, sigma0)
   - theta component = C(S1, T1, sigma0) - C(S1, T0, sigma0)
   - delta component = C(S1, T0, sigma0) - mid0
   The three components reconcile exactly to mid1 - mid0.
4. Inverts implied time-to-expiry T*(t) at each contract's anchor sigma; the
   excess clock ratio ((T*(t) - T*(t+1)) / dt - 1) maps theta-plus-IV drift
   onto a "time speed" (0.0 = model-perfect decay).
5. Aggregates ratio-of-sums actual/model multiples per
   (day, time-of-day, moneyness) bucket with day-block bootstrap CIs.

Outputs under --out: panel.parquet, buckets.csv, summary.json.

Run:

    uv run --extra dev python scripts/experiments/spy_0dte/analyze.py `
        --data-root .local/spy-0dte/lake --out .local/spy-0dte/analysis
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from osi import SECONDS_PER_YEAR, load_state, read_lake_dataset

from harness.quant import crr_implied_time_batch, crr_price_batch
from lake.contracts.partitions import AssetClass, DataType

_TOD_EDGES = (
    (0, 30, "open"),
    (30, 120, "morning"),
    (120, 270, "midday"),
    (270, 345, "afternoon"),
    (345, 390, "power"),
)
_MONEY_EDGES = ((0.0, 0.002, "atm"), (0.002, 0.008, "near"), (0.008, 1.0, "wing"))
_STEPS_PRICE = 128
_STEPS_INVERT = 64
_BISECT_ITERATIONS = 64
_HOURS_PER_YEAR = 365.0 * 24.0


def tod_bucket(minute_from_open: int) -> str:
    for start, end, name in _TOD_EDGES:
        if start <= minute_from_open < end:
            return name
    return "power"


def money_bucket(strike: float, spot: float) -> str:
    distance = abs(strike / spot - 1.0)
    for low, high, name in _MONEY_EDGES:
        if low <= distance < high:
            return name
    return "wing"


def implied_vol_batch(
    *,
    s: np.ndarray,
    k: np.ndarray,
    t: np.ndarray,
    price: np.ndarray,
    r: float,
    q: float,
    is_call: np.ndarray,
    steps: int,
    low: float = 0.005,
    high: float = 3.0,
    iterations: int = 60,
) -> np.ndarray:
    """Lockstep bisection for implied volatility on top of the batch pricer."""
    lo = np.full(price.shape, low)
    hi = np.full(price.shape, high)

    def residual(sigma: np.ndarray) -> np.ndarray:
        return (
            crr_price_batch(
                s=s,
                k=k,
                t=t,
                r=r,
                q=q,
                sigma=sigma,
                is_call=is_call,
                is_american=True,
                steps=steps,
            ).price
            - price
        )

    with np.errstate(invalid="ignore"):
        f_lo = residual(lo)
        f_hi = residual(hi)
    solvable = np.isfinite(f_lo) & np.isfinite(f_hi) & (f_lo < 0) & (f_hi > 0)
    for _ in range(iterations):
        mid = 0.5 * (lo + hi)
        f_mid = residual(mid)
        move = solvable & np.isfinite(f_mid) & (f_mid != 0)
        up = move & (f_mid < 0)
        lo = np.where(up, mid, lo)
        hi = np.where(move & ~up, mid, hi)
    sigma = 0.5 * (lo + hi)
    return np.where(solvable, sigma, np.nan)


def session_grid(open_time: datetime, close_time: datetime) -> pd.DatetimeIndex:
    return pd.date_range(
        pd.Timestamp(open_time).floor("min"),
        pd.Timestamp(close_time).floor("min"),
        freq="min",
        inclusive="left",
    )


def minute_last(
    frame: pd.DataFrame, value_columns: list[str], grid: pd.DatetimeIndex
) -> pd.DataFrame:
    """Last observation per minute, forward-filled within a staleness bound."""
    working = frame.copy()
    working["minute"] = working["timestamp"].dt.floor("min")
    last = working.groupby("minute", sort=True)[value_columns].last()
    return last.reindex(grid).ffill()


def minute_median(
    frame: pd.DataFrame, value_columns: list[str], grid: pd.DatetimeIndex
) -> pd.DataFrame:
    """Median observation per minute, forward-filled within a staleness bound."""
    working = frame.copy()
    working["minute"] = working["timestamp"].dt.floor("min")
    median = working.groupby("minute", sort=True)[value_columns].median()
    return median.reindex(grid).ffill()


def equity_mid_frame(table: pa.Table) -> pd.DataFrame:
    if "bid_price" in table.column_names:
        frame = table.select(["timestamp", "bid_price", "ask_price"]).to_pandas()
        bids = frame["bid_price"].astype(float)
        asks = frame["ask_price"].astype(float)
        valid = (bids > 0) & (asks > 0) & (asks >= bids)
        frame = frame[valid].copy()
        frame["mid"] = (bids[valid] + asks[valid]) / 2.0
    else:
        frame = table.select(["timestamp", "price"]).to_pandas()
        frame["mid"] = frame["price"].astype(float)
    frame = frame[frame["mid"] > 0]
    return frame[["timestamp", "mid"]]


def build_session_panel(
    *,
    data_root: Path,
    open_time: datetime,
    close_time: datetime,
    contracts: list[dict],
    cfg: argparse.Namespace,
) -> dict | None:
    equity_quotes = read_lake_dataset(
        data_root, open_time.date(), AssetClass.EQUITY, DataType.QUOTE
    )
    equity_trades = read_lake_dataset(
        data_root, open_time.date(), AssetClass.EQUITY, DataType.TRADE
    )
    option_quotes = read_lake_dataset(
        data_root, open_time.date(), AssetClass.OPTION, DataType.QUOTE
    )
    option_trades = (
        read_lake_dataset(
            data_root, open_time.date(), AssetClass.OPTION, DataType.TRADE
        )
        if option_quotes.num_rows == 0
        else option_quotes
    )
    if option_trades.num_rows == 0:
        return None
    price_source = "quotes" if option_quotes.num_rows else "trades"
    if equity_quotes.num_rows == 0 and equity_trades.num_rows == 0:
        return None

    grid = session_grid(open_time, close_time)
    spy_source = (
        equity_mid_frame(equity_quotes)
        if equity_quotes.num_rows
        else equity_mid_frame(equity_trades)
    )
    spy_minute = minute_last(spy_source, ["mid"], grid)["mid"]
    spy_minute = spy_minute.ffill(limit=cfg.staleness_minutes)
    if spy_minute.isna().any():
        return None

    if price_source == "quotes":
        option_frame = option_trades.select(
            ["timestamp", "symbol", "bid_price", "ask_price"]
        ).to_pandas()
        bids = option_frame["bid_price"].astype(float)
        asks = option_frame["ask_price"].astype(float)
        spread = asks - bids
        valid = (bids > 0) & (asks > 0) & (asks >= bids) & (spread <= cfg.max_spread)
        option_frame = option_frame[valid].copy()
        option_frame["mid"] = (bids[valid] + asks[valid]) / 2.0
        option_frame["spread"] = spread[valid]
        reduce = minute_last
    else:
        option_frame = option_trades.select(
            ["timestamp", "symbol", "price"]
        ).to_pandas()
        option_frame["mid"] = option_frame["price"].astype(float)
        option_frame["spread"] = 0.0
        reduce = minute_median
    if option_frame.empty:
        return None

    series_by_symbol: dict[str, pd.DataFrame] = {}
    for symbol, group in option_frame.groupby("symbol", sort=True):
        minute = reduce(group, ["mid", "spread"], grid)
        if minute["mid"].notna().sum() >= cfg.min_observations:
            series_by_symbol[str(symbol)] = minute

    log_moves = np.full(grid.size, np.nan)
    log_moves[1:] = np.abs(np.diff(np.log(spy_minute.to_numpy(dtype=float))))
    movement_grid = pd.Series(log_moves).ffill().fillna(0.0).to_numpy(dtype=float)
    total_move = float(movement_grid.sum())

    return {
        "grid": grid,
        "spy_mid": spy_minute.to_numpy(dtype=float),
        "movement_grid": movement_grid,
        "movement_total": total_move,
        "contracts": contracts,
        "series": series_by_symbol,
        "price_source": price_source,
    }


def analyze_session(
    *,
    data_root: Path,
    session_date: date,
    open_time: datetime,
    close_time: datetime,
    next_close: datetime | None,
    contracts: list[dict],
    cfg: argparse.Namespace,
) -> pd.DataFrame:
    panel = build_session_panel(
        data_root=data_root,
        open_time=open_time,
        close_time=close_time,
        contracts=contracts,
        cfg=cfg,
    )
    if panel is None:
        print(f"  {session_date.isoformat()}: panel unavailable", flush=True)
        return pd.DataFrame()

    grid = panel["grid"]
    spy_mid = panel["spy_mid"]
    t_by_dte = {
        0: np.array(
            [
                (close_time - moment.to_pydatetime()).total_seconds() / SECONDS_PER_YEAR
                for moment in grid
            ]
        )
    }
    if next_close is not None:
        t_by_dte[1] = np.array(
            [
                (next_close - moment.to_pydatetime()).total_seconds() / SECONDS_PER_YEAR
                for moment in grid
            ]
        )

    frames: list[pd.DataFrame] = []
    anchor_low = max(cfg.anchor_minute - cfg.anchor_halfwidth, 0)
    anchor_high = cfg.anchor_minute + cfg.anchor_halfwidth

    for contract in contracts:
        series = panel["series"].get(contract["symbol"])
        if series is None:
            continue
        contract_dte = int(contract.get("dte", 0))
        if contract_dte not in t_by_dte:
            continue
        mids = series["mid"].to_numpy(dtype=float)
        spreads = series["spread"].to_numpy(dtype=float)
        valid = np.flatnonzero(
            np.isfinite(mids) & (mids >= cfg.min_mid) & np.isfinite(spy_mid)
        )
        if valid.size < cfg.min_observations:
            continue

        minutes_c = valid.astype(int)
        strike = float(contract["strike"])
        kind_call = bool(contract["type"] == "call")
        s_c = spy_mid[minutes_c]
        t_c = t_by_dte[contract_dte][minutes_c]
        mid_c = mids[minutes_c]
        spread_c = spreads[minutes_c]
        is_call_c = np.full(minutes_c.size, kind_call, dtype=bool)

        # Current implied volatility at every node.
        sigma_c = implied_vol_batch(
            s=s_c,
            k=strike,
            t=t_c,
            price=mid_c,
            r=cfg.rate,
            q=cfg.dividend_yield,
            is_call=is_call_c,
            steps=_STEPS_INVERT,
        )
        # Greeks priced at the current IV.
        greeks = crr_price_batch(
            s=s_c,
            k=strike,
            t=t_c,
            r=cfg.rate,
            q=cfg.dividend_yield,
            sigma=sigma_c,
            is_call=is_call_c,
            is_american=True,
            steps=_STEPS_PRICE,
        )
        model_delta = np.asarray(greeks.delta, dtype=float)
        model_theta_y = np.asarray(greeks.theta, dtype=float)

        # Fixed-IV view: trailing 30-observation SMA of this contract's own
        # IV path, applied to both interval endpoints so the fixed model has
        # no vol component by construction.
        sigma_sma_c = (
            pd.Series(sigma_c, index=minutes_c)
            .rolling(30, min_periods=1)
            .mean()
            .to_numpy(dtype=float)
        )
        sma_greeks = crr_price_batch(
            s=s_c,
            k=strike,
            t=t_c,
            r=cfg.rate,
            q=cfg.dividend_yield,
            sigma=sigma_sma_c,
            is_call=is_call_c,
            is_american=True,
            steps=_STEPS_PRICE,
        )
        model_delta_sma = np.asarray(sma_greeks.delta, dtype=float)

        # Movement-driven scenario: anchor at this contract's first valid IV,
        # then evolve per tick in proportion to smoothed realized movement
        # share (SMA-7). Both endpoints price at the same scenario vol, so
        # the strip has no vol component by construction.
        iv_finite = np.where(np.isfinite(sigma_c), sigma_c, np.nan)
        valid_iv = iv_finite[np.isfinite(iv_finite)]
        if valid_iv.size == 0:
            continue
        sigma_anchor_open = float(valid_iv[0])
        movement_series = pd.Series(panel["movement_grid"][minutes_c])
        movement_smooth = movement_series.rolling(7, min_periods=1).mean()
        movement_total_smooth = float(
            pd.Series(panel["movement_grid"]).rolling(7, min_periods=1).mean().sum()
        )
        if movement_total_smooth <= 0:
            continue
        movement_weights = movement_smooth.to_numpy(dtype=float) / movement_total_smooth
        sigma_move_c = sigma_anchor_open * (1.0 + movement_weights)
        sigma_move_c = np.clip(sigma_move_c, 0.01, 2.0)
        move_greeks = crr_price_batch(
            s=s_c,
            k=strike,
            t=t_c,
            r=cfg.rate,
            q=cfg.dividend_yield,
            sigma=sigma_move_c,
            is_call=is_call_c,
            is_american=True,
            steps=_STEPS_PRICE,
        )
        model_delta_move = np.asarray(move_greeks.delta, dtype=float)
        usable = (
            np.isfinite(sigma_c)
            & np.isfinite(model_delta)
            & (np.abs(model_delta) <= 1.2)
            & (model_theta_y <= 0)
        )
        if usable.sum() < cfg.min_observations:
            continue

        # Anchor IV (around the 10:00 ET mark) drives the implied-time clock.
        anchor_window = (minutes_c >= anchor_low) & (minutes_c < anchor_high)
        anchor_values = sigma_c[anchor_window]
        anchor_values = anchor_values[np.isfinite(anchor_values)]
        if not anchor_values.size:
            anchor_values = sigma_c[usable]
            anchor_values = anchor_values[np.isfinite(anchor_values)]
        if not anchor_values.size:
            continue
        sigma_anchor = float(np.median(anchor_values))
        # Flat-IV reference greeks at the anchor (stable away from 0/0 zones).
        anchor_greeks = crr_price_batch(
            s=s_c,
            k=strike,
            t=t_c,
            r=cfg.rate,
            q=cfg.dividend_yield,
            sigma=np.full(minutes_c.size, sigma_anchor),
            is_call=is_call_c,
            is_american=True,
            steps=_STEPS_PRICE,
        )
        model_theta_anchor_y = np.asarray(anchor_greeks.theta, dtype=float)
        t_star = np.asarray(
            crr_implied_time_batch(
                s=s_c[usable],
                k=strike,
                r=cfg.rate,
                q=cfg.dividend_yield,
                sigma=sigma_anchor,
                price=mid_c[usable],
                t_lower=np.maximum(t_c[usable] / 6.0, 3e-6),
                t_upper=t_c[usable] * 6.0 + 3e-6,
                is_call=is_call_c[usable],
                is_american=True,
                steps=_STEPS_INVERT,
                bisect_iterations=_BISECT_ITERATIONS,
            ),
            dtype=float,
        )
        t_star_full = np.full(minutes_c.size, np.nan)
        t_star_full[usable] = t_star

        # Intervals between nearby valid nodes of this contract only.
        offsets = (1, 2) if cfg.interval_minutes == 1 else (cfg.interval_minutes,)
        a_list: list[np.ndarray] = []
        b_list: list[np.ndarray] = []
        for offset in offsets:
            if minutes_c.size <= offset:
                continue
            span = minutes_c[offset:] - minutes_c[:-offset]
            local = np.flatnonzero(span == offset)
            a_list.append(local)
            b_list.append(local + offset)
        if not a_list:
            continue
        a = np.concatenate(a_list)
        b = np.concatenate(b_list)
        keep = (
            usable[a]
            & usable[b]
            & np.isfinite(t_star_full[a])
            & np.isfinite(t_star_full[b])
        )
        a = a[keep]
        b = b[keep]
        if a.size == 0:
            continue

        s0 = s_c[a]
        s1 = s_c[b]
        t0 = t_c[a]
        t1 = t_c[b]
        sigma_a = sigma_c[a]
        sigma_f_a = sigma_sma_c[a]
        dt_years = t0 - t1
        # Backward strip at the interval's opening IV: from the observed end
        # price remove the vol move, then the time move, then the spot move.
        # The three components reconcile exactly to the observed change.
        corners = crr_price_batch(
            s=np.concatenate([s1, s1]),
            k=strike,
            t=np.concatenate([t1, t0]),
            r=cfg.rate,
            q=cfg.dividend_yield,
            sigma=np.concatenate([sigma_a, sigma_a]),
            is_call=np.full(2 * a.size, kind_call, dtype=bool),
            is_american=True,
            steps=_STEPS_PRICE,
        )
        corner_prices = np.asarray(corners.price, dtype=float)
        end_at_start_vol = corner_prices[: a.size]  # C(s1, t1, sigma_a)
        start_at_end_spot = corner_prices[a.size :]  # C(s1, t0, sigma_a)
        vol_component = mid_c[b] - end_at_start_vol
        theta_component = end_at_start_vol - start_at_end_spot
        delta_component = start_at_end_spot - mid_c[a]

        # Fixed-IV strip: both endpoints priced at the SMA-30 IV, so the
        # spot and time components alone explain the model change.
        fixed_corners = crr_price_batch(
            s=np.concatenate([s1, s1, s0]),
            k=strike,
            t=np.concatenate([t1, t0, t0]),
            r=cfg.rate,
            q=cfg.dividend_yield,
            sigma=np.concatenate([sigma_f_a, sigma_f_a, sigma_f_a]),
            is_call=np.full(3 * a.size, kind_call, dtype=bool),
            is_american=True,
            steps=_STEPS_PRICE,
        )
        fixed_prices = np.asarray(fixed_corners.price, dtype=float)
        end_fixed = fixed_prices[: a.size]  # C(s1, t1, sigma_f)
        start_at_end_spot_fixed = fixed_prices[
            a.size : 2 * a.size
        ]  # C(s1, t0, sigma_f)
        start_fixed = fixed_prices[2 * a.size :]  # C(s0, t0, sigma_f)
        theta_component_sma = end_fixed - start_at_end_spot_fixed
        delta_component_sma = start_at_end_spot_fixed - start_fixed

        # Movement-driven strip: both endpoints at the movement-scaled IV.
        move_corners = crr_price_batch(
            s=np.concatenate([s1, s1, s0]),
            k=strike,
            t=np.concatenate([t1, t0, t0]),
            r=cfg.rate,
            q=cfg.dividend_yield,
            sigma=np.concatenate([sigma_move_c[b], sigma_move_c[a], sigma_move_c[a]]),
            is_call=np.full(3 * a.size, kind_call, dtype=bool),
            is_american=True,
            steps=_STEPS_PRICE,
        )
        move_prices = np.asarray(move_corners.price, dtype=float)
        end_move = move_prices[: a.size]  # C(s1, t1, sigma_move)
        start_at_end_spot_move = move_prices[
            a.size : 2 * a.size
        ]  # C(s1, t0, sigma_move)
        start_move = move_prices[2 * a.size :]  # C(s0, t0, sigma_move)
        theta_component_move = end_move - start_at_end_spot_move
        delta_component_move = start_at_end_spot_move - start_move

        theta_cents_h = -theta_component / dt_years * 100.0 / _HOURS_PER_YEAR
        model_theta_cents_h = -model_theta_y[a] * 100.0 / _HOURS_PER_YEAR
        excess_clock = (t_star_full[a] - t_star_full[b]) / dt_years - 1.0

        frame = pd.DataFrame(
            {
                "session": session_date.isoformat(),
                "symbol": contract["symbol"],
                "dte": contract_dte,
                "is_call": kind_call,
                "strike": strike,
                "minute": minutes_c[a],
                "tod": [tod_bucket(int(m)) for m in minutes_c[a]],
                "money": [money_bucket(strike, float(s)) for s in s0],
                "dt_min": (minutes_c[b] - minutes_c[a]).astype(float),
                "dt_years": dt_years,
                "mid0": mid_c[a],
                "mid1": mid_c[b],
                "spread0": spread_c[a],
                "s0": s0,
                "s1": s1,
                "t0": t0,
                "sigma_t0": sigma_a,
                "sigma_t1": sigma_c[b],
                "sigma_anchor": sigma_anchor,
                "delta_component": delta_component,
                "theta_component": theta_component,
                "vol_component": vol_component,
                "sigma_sma": sigma_f_a,
                "theta_component_sma": theta_component_sma,
                "delta_component_sma": delta_component_sma,
                "delta_move_sma": model_delta_sma[a] * (s1 - s0),
                "sigma_move": sigma_move_c[a],
                "theta_component_move": theta_component_move,
                "delta_component_move": delta_component_move,
                "delta_move_move": model_delta_move[a] * (s1 - s0),
                "d_obs": mid_c[b] - mid_c[a],
                "model_delta": model_delta[a],
                "model_theta_y": model_theta_y[a],
                "model_theta_anchor_y": model_theta_anchor_y[a],
                "delta_move": model_delta[a] * (s1 - s0),
                "theta_cents_h": theta_cents_h,
                "model_theta_cents_h": model_theta_cents_h,
                "tstar0": t_star_full[a],
                "tstar1": t_star_full[b],
                "excess_clock": excess_clock,
            }
        )
        frames.append(frame)
    if not frames:
        return pd.DataFrame()
    frame = pd.concat(frames, ignore_index=True)
    print(
        f"  analyzed {session_date.isoformat()}: {len(frame)} intervals, "
        f"{frame['symbol'].nunique()} contracts",
        flush=True,
    )
    return frame


def regress(frame: pd.DataFrame) -> dict | None:
    """Ratio-of-sums actual/model multiples over one group of intervals.

    The backward-strip components reconcile exactly to the observed change,
    so the group aggregate is the sum of each component against the sum of
    its model counterpart. Intrinsic-floor rows carry no time value and are
    dropped first.
    """
    if len(frame) < 30:
        return None
    working = frame.copy()
    intrinsic0 = np.where(
        working["is_call"].to_numpy(dtype=bool),
        working["s0"] - working["strike"],
        working["strike"] - working["s0"],
    )
    intrinsic1 = np.where(
        working["is_call"].to_numpy(dtype=bool),
        working["s1"] - working["strike"],
        working["strike"] - working["s1"],
    )
    alive = (working["mid0"] > np.maximum(intrinsic0, 0) + 1e-6) & (
        working["mid1"] > np.maximum(intrinsic1, 0) + 1e-6
    )
    working = working[alive]
    if len(working) < 30:
        return None
    model_theta_interval = working["model_theta_y"] * working["dt_years"]
    denom_theta = float(model_theta_interval.sum())
    if abs(denom_theta) < 1e-9:
        return None
    theta_mult = float(working["theta_component"].sum() / denom_theta)
    denom_delta = float(working["delta_move"].abs().sum())
    delta_mult = (
        float(working["delta_component"].abs().sum() / denom_delta)
        if denom_delta > 1e-9
        else float("nan")
    )
    return {
        "theta_mult": theta_mult,
        "delta_mult": delta_mult,
        "n": int(len(working)),
        "model_theta_y_mean": float(working["model_theta_y"].mean()),
        "excess_clock_mean": float(working["excess_clock"].mean()),
        "vol_per_hour_cents": float(
            (working["vol_component"] / working["dt_years"]).mean()
            / _HOURS_PER_YEAR
            * 100.0
        ),
    }


def bootstrap_over_sessions(
    values_by_session: dict[str, float],
    *,
    iterations: int,
    seed: int,
) -> tuple[float, float, float]:
    values = np.array(
        [value for value in values_by_session.values() if np.isfinite(value)]
    )
    if values.size == 0:
        return (float("nan"), float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    samples = rng.choice(values, size=(iterations, values.size), replace=True)
    point = float(np.median(values))
    medians = np.median(samples, axis=1)
    return (
        point,
        float(np.percentile(medians, 2.5)),
        float(np.percentile(medians, 97.5)),
    )


def aggregate(
    panel: pd.DataFrame, cfg: argparse.Namespace
) -> tuple[pd.DataFrame, list[dict]]:
    records: list[dict] = []
    for (tod, money), group in panel.groupby(["tod", "money"], sort=True):
        per_session: dict[str, dict] = {}
        for session, session_group in group.groupby("session", sort=True):
            stats = regress(session_group)
            if stats is not None:
                per_session[str(session)] = stats
        if not per_session:
            continue
        metric_sources = {
            "delta_mult": {s: v["delta_mult"] for s, v in per_session.items()},
            "theta_mult": {s: v["theta_mult"] for s, v in per_session.items()},
            "excess_clock": {s: v["excess_clock_mean"] for s, v in per_session.items()},
            "vol_per_hour_cents": {
                s: v["vol_per_hour_cents"] for s, v in per_session.items()
            },
        }
        entry: dict = {
            "tod": tod,
            "money": money,
            "sessions": len(per_session),
            "intervals": int(len(group)),
        }
        for metric, source in metric_sources.items():
            mean, low, high = bootstrap_over_sessions(
                source, iterations=cfg.bootstrap_iterations, seed=cfg.seed
            )
            entry[metric] = mean
            entry[f"{metric}_lo"] = low
            entry[f"{metric}_hi"] = high
        entry["model_theta_y_mean"] = float(
            np.mean([v["model_theta_y_mean"] for v in per_session.values()])
        )
        records.append(entry)
    return pd.DataFrame(records), records


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--rate", type=float, default=0.043)
    parser.add_argument("--dividend-yield", type=float, default=0.012)
    parser.add_argument("--max-spread", type=float, default=0.05)
    parser.add_argument("--min-mid", type=float, default=0.05)
    parser.add_argument("--min-observations", type=int, default=150)
    parser.add_argument("--staleness-minutes", type=int, default=2)
    parser.add_argument("--max-interval-minutes", type=int, default=2)
    parser.add_argument("--interval-minutes", type=int, default=1)
    parser.add_argument("--anchor-minute", type=int, default=30)
    parser.add_argument("--anchor-halfwidth", type=int, default=3)
    parser.add_argument("--bootstrap-iterations", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--max-sessions", type=int, default=None)
    parser.add_argument("--truth", type=Path, default=None)
    args = parser.parse_args(argv)
    args.data_root = args.data_root.resolve()
    args.out = args.out.resolve()
    return args


def session_bounds(state_entry: dict, session_date: date) -> tuple[datetime, datetime]:
    """Session bounds from state, falling back to observed data extents."""
    if state_entry.get("open") and state_entry.get("close"):
        return (
            datetime.fromisoformat(state_entry["open"]),
            datetime.fromisoformat(state_entry["close"]),
        )
    raise ValueError(
        f"state.json is missing open/close for {session_date.isoformat()}; "
        "re-run the ingest to record session bounds"
    )


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
        f"analyzing {len(sessions)} sessions from {args.data_root} "
        f"(rate={args.rate}, q={args.dividend_yield})",
        flush=True,
    )

    panels: list[pd.DataFrame] = []
    session_meta: dict[str, dict] = {}
    for key in sessions:
        entry = state["sessions"][key]
        contracts = entry.get("contracts", [])
        datasets = entry.get("datasets", {})
        if datasets.get("option/trade") != "done":
            print(f"  {key}: option data not fully pulled, skipped", flush=True)
            continue
        if not contracts:
            continue
        session_date = date.fromisoformat(key)
        try:
            open_time, close_time = session_bounds(entry, session_date)
        except ValueError as error:
            print(f"  {key}: {error}", flush=True)
            continue
        next_close = (
            datetime.fromisoformat(entry["next_close"])
            if entry.get("next_close")
            else None
        )
        frame = analyze_session(
            data_root=args.data_root,
            session_date=session_date,
            open_time=open_time,
            close_time=close_time,
            next_close=next_close,
            contracts=contracts,
            cfg=args,
        )
        if not frame.empty:
            panels.append(frame)
        session_meta[key] = {"contracts": len(contracts)}

    if not panels:
        print("no analyzable sessions", flush=True)
        return 1
    panel = pd.concat(panels, ignore_index=True)
    buckets, records = aggregate(panel, args)

    args.out.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        pa.Table.from_pandas(panel, preserve_index=False),
        args.out / "panel.parquet",
        compression="zstd",
    )
    buckets.to_csv(args.out / "buckets.csv", index=False)
    summary = {
        "buckets": records,
        "config": {
            "rate": args.rate,
            "dividend_yield": args.dividend_yield,
            "max_spread": args.max_spread,
            "min_mid": args.min_mid,
            "anchor_minute": args.anchor_minute,
            "steps_price": _STEPS_PRICE,
            "steps_invert": _STEPS_INVERT,
            "sessions": session_meta,
            "intervals": int(len(panel)),
        },
    }
    if args.truth is not None and args.truth.is_file():
        summary["truth"] = json.loads(args.truth.read_text(encoding="utf-8"))
    (args.out / "summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
    )

    print(f"\npanel rows: {len(panel)}", flush=True)
    print(buckets.to_string(index=False), flush=True)
    print(f"\nwrote {args.out / 'buckets.csv'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
