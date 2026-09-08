"""Strategy backtests over the 42-session SPY 0DTE/1DTE dataset.

Simulates four option-income strategies directly from the per-minute price
panels extracted from the raw lake, using exact trade-print medians at
specific strikes and minutes (no regression, no smoothing — just the actual
NBBO-proxy prices the market printed).

Strategies:

1. **Opening Calendar** — sell the 0DTE ATM straddle at minute 5, buy the
   1DTE ATM straddle at the same time, exit both at minute 45.
   P&L = (entry 0DTE straddle - exit 0DTE straddle)
       + (exit 1DTE straddle - entry 1DTE straddle)

2. **Midday Iron Condor** — sell the ±1% iron condor at minute 120, buy it
   back at minute 330. Only taken when the absolute SPY return from minute
   0 to minute 120 is below 0.5% (quiet-morning filter).

3. **Power-Hour Straddle** — sell the 0DTE ATM straddle at minute 345,
   buy it back at minute 385.

4. **Overnight ATM Straddle** — buy the next-day-expiry (1DTE) ATM straddle
   at the prior session's close (minute 385 of day D), sell it at minute 90
   of session D+1 (when it is 0DTE).

Writes a JSON fixture consumed by the thesis UI demo page's backtest tab:

    src/ui/src/lib/data/spy0dteBacktests.js

Run:

    uv run --extra dev python scripts/experiments/spy_0dte/strategy_backtests.py `
        --data-root .local/spy-0dte/lake
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
from osi import load_state, read_lake_dataset

from lake.contracts.partitions import AssetClass, DataType


def equity_mid_series(data_root: Path, session_date: date) -> pd.Series:
    """1-minute SPY mid series indexed by minute-from-open (0 = 9:30 ET)."""
    equity_quotes = read_lake_dataset(
        data_root, session_date, AssetClass.EQUITY, DataType.QUOTE
    )
    if equity_quotes.num_rows == 0:
        return pd.Series(dtype=float)
    frame = equity_quotes.select(["timestamp", "bid_price", "ask_price"]).to_pandas()
    bids = frame["bid_price"].astype(float)
    asks = frame["ask_price"].astype(float)
    valid = (bids > 0) & (asks > 0) & (asks >= bids)
    frame = frame[valid].copy()
    frame["mid"] = (bids[valid] + asks[valid]) / 2.0
    frame["minute_ts"] = frame["timestamp"].dt.floor("min")
    first = frame["minute_ts"].min()
    frame["minute_offset"] = (
        (frame["minute_ts"] - first).dt.total_seconds() / 60
    ).astype(int)
    return frame.groupby("minute_offset", sort=True)["mid"].last()


def option_minute_panel(
    data_root: Path,
    session_date: date,
    contracts: list[dict],
    dte: int,
    open_minute_ts: pd.Timestamp | None = None,
) -> dict[tuple[float, str], pd.Series]:
    """Per-(strike, option_type) minute-offset price series for one expiry."""
    option_trades = read_lake_dataset(
        data_root, session_date, AssetClass.OPTION, DataType.TRADE
    )
    if option_trades.num_rows == 0:
        return {}
    manifest = {
        contract["symbol"]: contract
        for contract in contracts
        if int(contract.get("dte", 0)) == dte
    }
    frame = option_trades.select(["timestamp", "symbol", "price"]).to_pandas()
    frame["symbol"] = frame["symbol"].astype(str)
    frame = frame[frame["symbol"].isin(manifest)]
    if frame.empty:
        return {}
    frame["strike"] = frame["symbol"].map(lambda s: float(manifest[s]["strike"]))
    frame["option_type"] = frame["symbol"].map(lambda s: str(manifest[s]["type"]))
    frame["price"] = frame["price"].astype(float)
    frame["minute_ts"] = frame["timestamp"].dt.floor("min")
    first = open_minute_ts if open_minute_ts is not None else frame["minute_ts"].min()
    frame["minute_offset"] = (
        (frame["minute_ts"] - first).dt.total_seconds() / 60
    ).astype(int)
    frame = frame[(frame["minute_offset"] >= 0) & (frame["minute_offset"] <= 390)]
    grouped = frame.groupby(["strike", "option_type", "minute_offset"])[
        "price"
    ].median()
    result: dict[tuple[float, str], pd.Series] = {}
    for (strike, option_type), group in grouped.groupby(level=[0, 1]):
        result[(strike, option_type)] = group.droplevel([0, 1])
    return result


def minute_value(
    panel: dict[tuple[float, str], pd.Series],
    strike: float,
    option_type: str,
    minute: int,
    tolerance: int = 3,
) -> float | None:
    """Price at or near a minute, forward-filled within tolerance."""
    key = (strike, option_type)
    series = panel.get(key)
    if series is None or series.empty:
        return None
    for offset in range(tolerance + 1):
        for sign in [0] if offset == 0 else [1, -1]:
            candidate = minute + sign * offset
            if candidate in series.index:
                value = float(series.loc[candidate])
                if np.isfinite(value) and value >= 0:
                    return value
    return None


def straddle_price(
    panel: dict[tuple[float, str], pd.Series],
    strike: float,
    minute: int,
    tolerance: int = 3,
) -> tuple[float, float] | None:
    """(call_price, put_price) at a strike and minute, or None if either missing."""
    call = minute_value(panel, strike, "call", minute, tolerance)
    put = minute_value(panel, strike, "put", minute, tolerance)
    if call is None or put is None:
        return None
    return (call, put)


def nearest_strike(available_strikes: list[float], target: float) -> float | None:
    if not available_strikes:
        return None
    return min(available_strikes, key=lambda s: abs(s - target))


def session_atm_strike(
    panel: dict[tuple[float, str], pd.Series],
    open_spot: float,
    tolerance_pct: float = 1.0,
) -> float | None:
    """Nearest strike with both a call and put traded."""
    strikes = sorted({s for s, _ in panel})
    low = open_spot * (1 - tolerance_pct / 100)
    high = open_spot * (1 + tolerance_pct / 100)
    candidates = [s for s in strikes if low <= s <= high]
    return nearest_strike(candidates, open_spot)


def build_sessions(
    data_root: Path,
) -> list[dict]:
    """Load every session's spot series and option minute panels."""
    state = load_state(data_root)
    sessions = sorted(state.get("sessions", {}))
    result: list[dict] = []
    for key in sessions:
        entry = state["sessions"][key]
        contracts = entry.get("contracts", [])
        if not contracts:
            continue
        session_date = date.fromisoformat(key)
        spot = equity_mid_series(data_root, session_date)
        if spot.empty:
            continue
        open_spot = float(spot.iloc[0]) if len(spot) > 0 else None
        if open_spot is None:
            continue
        open_ts = spot.index[0]
        equity_quotes_ts = (
            read_lake_dataset(
                data_root, session_date, AssetClass.EQUITY, DataType.QUOTE
            )
            .select(["timestamp"])
            .to_pandas()["timestamp"]
            .dt.floor("min")
            .min()
        )
        panel0 = option_minute_panel(
            data_root, session_date, contracts, 0, open_minute_ts=equity_quotes_ts
        )
        panel1 = option_minute_panel(
            data_root, session_date, contracts, 1, open_minute_ts=equity_quotes_ts
        )
        result.append(
            {
                "session": key,
                "date": session_date,
                "spot": spot,
                "open_spot": open_spot,
                "panel0": panel0,
                "panel1": panel1,
                "next_session": None,
            }
        )
    for index in range(len(result) - 1):
        result[index]["next_session"] = result[index + 1]
    return result


def compute_strategies(
    sessions: list[dict],
    *,
    otm_pct_condor: float = 1.0,
    wing_pct_condor: float = 2.0,
    quiet_threshold_pct: float = 0.5,
) -> dict:
    """Simulate all four strategies across sessions; return per-day and summary."""
    calendar_days: list[dict] = []
    condor_days: list[dict] = []
    power_days: list[dict] = []
    overnight_days: list[dict] = []

    for session in sessions:
        key = session["session"]
        spot = session["spot"]
        panel0 = session["panel0"]
        panel1 = session["panel1"]
        open_spot = session["open_spot"]
        next_session = session["next_session"]

        atm = session_atm_strike(panel0, open_spot)
        if atm is None:
            continue

        # --- Strategy 1: Opening Calendar ---
        entry_0dte = straddle_price(panel0, atm, 5)
        exit_0dte = straddle_price(panel0, atm, 45)
        entry_1dte = straddle_price(panel1, atm, 5) if panel1 else None
        exit_1dte = straddle_price(panel1, atm, 45) if panel1 else None
        if all(v is not None for v in [entry_0dte, exit_0dte, entry_1dte, exit_1dte]):
            entry_straddle_0 = entry_0dte[0] + entry_0dte[1]
            exit_straddle_0 = exit_0dte[0] + exit_0dte[1]
            entry_straddle_1 = entry_1dte[0] + entry_1dte[1]
            exit_straddle_1 = exit_1dte[0] + exit_1dte[1]
            pnl = (entry_straddle_0 - exit_straddle_0) + (
                exit_straddle_1 - entry_straddle_1
            )
            calendar_days.append(
                {
                    "session": key,
                    "pnl": round(pnl, 4),
                    "entry_0dte": round(entry_straddle_0, 4),
                    "exit_0dte": round(exit_straddle_0, 4),
                    "entry_1dte": round(entry_straddle_1, 4),
                    "exit_1dte": round(exit_straddle_1, 4),
                }
            )

        # --- Strategy 2: Midday Iron Condor (quiet-morning filter) ---
        spot_at_0 = _spot_at(spot, 0)
        spot_at_120 = _spot_at(spot, 120)
        if spot_at_0 and spot_at_120:
            morning_ret = abs(spot_at_120 / spot_at_0 - 1) * 100
            if morning_ret < quiet_threshold_pct:
                condor_strikes = _find_condor_strikes(
                    panel0, spot_at_120, otm_pct_condor, wing_pct_condor
                )
                if condor_strikes:
                    entry_condor = _condor_value(panel0, condor_strikes, 120)
                    exit_condor = _condor_value(panel0, condor_strikes, 330)
                    if entry_condor is not None and exit_condor is not None:
                        pnl = entry_condor - exit_condor
                        condor_days.append(
                            {
                                "session": key,
                                "pnl": round(pnl, 4),
                                "morning_return": round(morning_ret, 4),
                                "credit": round(entry_condor, 4),
                                "close_cost": round(exit_condor, 4),
                                "strikes": condor_strikes,
                            }
                        )

        # --- Strategy 3: Power-Hour Straddle ---
        entry_ph = straddle_price(panel0, atm, 345)
        exit_ph = straddle_price(panel0, atm, 385)
        if entry_ph and exit_ph:
            entry_s = entry_ph[0] + entry_ph[1]
            exit_s = exit_ph[0] + exit_ph[1]
            pnl = entry_s - exit_s
            power_days.append(
                {
                    "session": key,
                    "pnl": round(pnl, 4),
                    "entry": round(entry_s, 4),
                    "exit": round(exit_s, 4),
                }
            )

        # --- Strategy 4: Overnight ATM Straddle ---
        if next_session:
            prior_atm = session_atm_strike(panel1, open_spot) if panel1 else None
            if prior_atm:
                entry_on = straddle_price(panel1, prior_atm, 385)
                next_panel0 = next_session["panel0"]
                exit_on = straddle_price(next_panel0, prior_atm, 90)
                if entry_on and exit_on:
                    entry_s = entry_on[0] + entry_on[1]
                    exit_s = exit_on[0] + exit_on[1]
                    pnl = exit_s - entry_s
                    overnight_days.append(
                        {
                            "session": f"{key}->{next_session['session']}",
                            "pnl": round(pnl, 4),
                            "entry": round(entry_s, 4),
                            "exit": round(exit_s, 4),
                            "strike": prior_atm,
                        }
                    )

    return {
        "openingCalendar": {"days": calendar_days},
        "middayCondor": {"days": condor_days},
        "powerHourStraddle": {"days": power_days},
        "overnightStraddle": {"days": overnight_days},
    }


def _spot_at(spot: pd.Series, minute: int) -> float | None:
    """Spot at or near a minute offset (0-based from session open)."""
    if spot.empty:
        return None
    for offset in range(4):
        for sign in [0] if offset == 0 else [1, -1]:
            candidate = minute + sign * offset
            if candidate in spot.index:
                value = float(spot.loc[candidate])
                if np.isfinite(value) and value > 0:
                    return value
    return None


def _find_condor_strikes(
    panel: dict[tuple[float, str], pd.Series],
    spot: float,
    otm_pct: float,
    wing_pct: float,
) -> dict | None:
    strikes = sorted({s for s, _ in panel})
    put_sell_target = spot * (1 - otm_pct / 100)
    put_buy_target = spot * (1 - wing_pct / 100)
    call_sell_target = spot * (1 + otm_pct / 100)
    call_buy_target = spot * (1 + wing_pct / 100)

    put_sell = nearest_strike(
        [s for s in strikes if s <= put_sell_target], put_sell_target
    )
    put_buy = nearest_strike(
        [s for s in strikes if s <= put_buy_target], put_buy_target
    )
    call_sell = nearest_strike(
        [s for s in strikes if s >= call_sell_target], call_sell_target
    )
    call_buy = nearest_strike(
        [s for s in strikes if s >= call_buy_target], call_buy_target
    )

    if None in (put_sell, put_buy, call_sell, call_buy):
        return None
    if put_sell is not None and put_buy is not None and put_sell <= put_buy:
        return None
    if call_sell is not None and call_buy is not None and call_sell >= call_buy:
        return None
    return {
        "putSell": put_sell,
        "putBuy": put_buy,
        "callSell": call_sell,
        "callBuy": call_buy,
    }


def _condor_value(
    panel: dict[tuple[float, str], pd.Series],
    strikes: dict,
    minute: int,
) -> float | None:
    """Net credit (+) or cost (-) of the iron condor at a minute."""
    values = []
    for role, option_type in [
        ("putSell", "put"),
        ("putBuy", "put"),
        ("callSell", "call"),
        ("callBuy", "call"),
    ]:
        value = minute_value(panel, strikes[role], option_type, minute)
        if value is None:
            return None
        values.append(value)
    # Credit = (put_sell + call_sell) - (put_buy + call_buy)
    credit = (values[0] + values[2]) - (values[1] + values[3])
    return credit


def summarize(days: list[dict]) -> dict:
    if not days:
        return {"count": 0}
    pnls = [day["pnl"] for day in days]
    wins = sum(1 for p in pnls if p > 0)
    equity = list(np.cumsum(pnls))
    max_dd = 0.0
    peak = 0.0
    for value in equity:
        peak = max(peak, value)
        max_dd = max(max_dd, peak - value)
    return {
        "count": len(days),
        "totalPnl": round(sum(pnls), 4),
        "meanPnl": round(float(np.mean(pnls)), 4),
        "medianPnl": round(float(np.median(pnls)), 4),
        "winRate": round(wins / len(pnls), 4),
        "worst": round(min(pnls), 4),
        "best": round(max(pnls), 4),
        "maxDrawdown": round(max_dd, 4),
        "equity": [round(v, 4) for v in equity],
        "days": days,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--otm-pct", type=float, default=1.0)
    parser.add_argument("--wing-pct", type=float, default=2.0)
    parser.add_argument("--quiet-threshold", type=float, default=0.5)
    args = parser.parse_args(argv)
    args.data_root = args.data_root.resolve()
    args.out = args.out.resolve()
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    sessions = build_sessions(args.data_root)
    print(f"loaded {len(sessions)} sessions", flush=True)
    results = compute_strategies(
        sessions,
        otm_pct_condor=args.otm_pct,
        wing_pct_condor=args.wing_pct,
        quiet_threshold_pct=args.quiet_threshold,
    )
    report = {
        "meta": {
            "sessions": len(sessions),
            "generated": datetime.now(UTC).isoformat(),
            "otmPct": args.otm_pct,
            "wingPct": args.wing_pct,
            "quietThreshold": args.quiet_threshold,
        },
        "strategies": {name: summarize(data["days"]) for name, data in results.items()},
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        "/* Generated by scripts/experiments/spy_0dte/strategy_backtests.py - do not edit. */\n"
        "export const backtests = " + json.dumps(report, indent=1) + ";\n",
        encoding="utf-8",
    )
    print(f"wrote {args.out}", flush=True)
    for name, summary in report["strategies"].items():
        if summary.get("count", 0) > 0:
            print(
                f"  {name}: {summary['count']} trades, "
                f"total {summary['totalPnl']:+.2f}, "
                f"win rate {summary['winRate']:.0%}, "
                f"mean {summary['meanPnl']:+.2f}",
                flush=True,
            )
        else:
            print(f"  {name}: 0 trades", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
