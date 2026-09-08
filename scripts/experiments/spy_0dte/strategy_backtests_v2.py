"""Strategy backtests v2 — stops, wider wings, commission, and spread modeling.

Re-runs the four strategies with realistic trading costs:

- **Stop-loss at 50% of credit received** on every premium-selling strategy,
  checked at every minute against the prevailing trade-print price.
- **Profit-taking at 60% of credit** for the condor.
- **Wider condor wings** at ±1.5% / ±3%.
- **Morning filter at 1.0%** absolute SPY return.
- **Commission**: $0.65 per contract per fill (typical retail).
- **Bid/ask spread**: half-spread = max($0.025, 3% of option price).
  Buying fills at ask (price + half-spread); selling fills at bid
  (price - half-spread). Every fill in every strategy pays both.

Option prices in the lake data are already in dollars (e.g., a straddle worth
$3.50 is three dollars fifty), so all P&L values below are in dollars.

Writes a JSON fixture consumed by the thesis UI demo page's backtest tab:

    src/ui/src/lib/data/spy0dteBacktestsV2.js

Run:

    uv run --extra dev python scripts/experiments/spy_0dte/strategy_backtests_v2.py `
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
from strategy_backtests import (
    _spot_at,
    equity_mid_series,
    minute_value,
    option_minute_panel,
    session_atm_strike,
)

from lake.contracts.partitions import AssetClass, DataType

_STOP_LOSS_PCT = 0.50
_PROFIT_TAKE_PCT = 0.60
_COMMISSION_PER_CONTRACT = 0.65
_HALF_SPREAD_MIN = 0.025
_HALF_SPREAD_PCT = 0.03


def half_spread(price: float) -> float:
    """Estimated half-spread for one fill at this option price."""
    return max(_HALF_SPREAD_MIN, _HALF_SPREAD_PCT * price)


def fill_buy(price: float) -> float:
    """Effective buy price: ask (trade price + half-spread)."""
    return price + half_spread(price)


def fill_sell(price: float) -> float:
    """Effective sell price: bid (trade price - half-spread)."""
    return price - half_spread(price)


def fill_cost(n_contracts: int) -> float:
    """Total commission for n contract fills."""
    return n_contracts * _COMMISSION_PER_CONTRACT


def build_sessions(data_root: Path) -> list[dict]:
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
        equity_quotes = read_lake_dataset(
            data_root, session_date, AssetClass.EQUITY, DataType.QUOTE
        )
        open_ts = (
            equity_quotes.select(["timestamp"])
            .to_pandas()["timestamp"]
            .dt.floor("min")
            .min()
        )
        panel0 = option_minute_panel(
            data_root, session_date, contracts, 0, open_minute_ts=open_ts
        )
        panel1 = option_minute_panel(
            data_root, session_date, contracts, 1, open_minute_ts=open_ts
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


def nearest_strike(available: list[float], target: float) -> float | None:
    if not available:
        return None
    return min(available, key=lambda s: abs(s - target))


def find_condor_strikes(
    panel: dict[tuple[float, str], pd.Series],
    spot: float,
    otm_pct: float = 1.5,
    wing_pct: float = 3.0,
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
    if put_sell <= put_buy or call_sell >= call_buy:
        return None
    return {
        "putSell": put_sell,
        "putBuy": put_buy,
        "callSell": call_sell,
        "callBuy": call_buy,
    }


def condor_credit_gross(
    panel: dict[tuple[float, str], pd.Series], strikes: dict, minute: int
) -> float | None:
    legs = [
        ("putSell", "put"),
        ("putBuy", "put"),
        ("callSell", "call"),
        ("callBuy", "call"),
    ]
    values = []
    for role, option_type in legs:
        value = minute_value(panel, strikes[role], option_type, minute)
        if value is None:
            return None
        values.append(value)
    return (values[0] + values[2]) - (values[1] + values[3])


def condor_credit_net(
    panel: dict[tuple[float, str], pd.Series], strikes: dict, minute: int
) -> tuple[float, float] | None:
    """Net credit including spread + commission. Returns (net_credit, gross_credit)."""
    legs = [
        ("putSell", "put"),
        ("putBuy", "put"),
        ("callSell", "call"),
        ("callBuy", "call"),
    ]
    values = []
    for role, option_type in legs:
        value = minute_value(panel, strikes[role], option_type, minute)
        if value is None:
            return None
        values.append(value)
    # Selling: receive bid (price - half_spread)
    # Buying: pay ask (price + half_spread)
    net = (
        fill_sell(values[0])
        + fill_sell(values[2])
        - fill_buy(values[1])
        - fill_buy(values[3])
        - fill_cost(4)
    )
    gross = (values[0] + values[2]) - (values[1] + values[3])
    return (net, gross)


def straddle_prices(
    panel: dict[tuple[float, str], pd.Series], strike: float, minute: int
) -> tuple[float, float] | None:
    call = minute_value(panel, strike, "call", minute)
    put = minute_value(panel, strike, "put", minute)
    if call is None or put is None:
        return None
    return (call, put)


def run_condor_v2(
    panel: dict[tuple[float, str], pd.Series],
    strikes: dict,
    entry_minute: int,
    exit_minute: int,
    stop_pct: float = _STOP_LOSS_PCT,
    profit_take_pct: float = _PROFIT_TAKE_PCT,
) -> dict | None:
    """Short condor: sell both spreads, with stop and profit take, net of costs."""
    entry = condor_credit_net(panel, strikes, entry_minute)
    if entry is None:
        return None
    credit_net, credit_gross = entry
    if credit_net <= 0:
        return None

    exit_minute_final = exit_minute
    exit_reason = "expiry"
    exit_net = None

    for minute in range(entry_minute + 1, exit_minute + 1):
        current = condor_credit_net(panel, strikes, minute)
        if current is None:
            continue
        current_net, _ = current
        # To close the condor, we buy back the credit we sold.
        # The cost to close = current_net (what someone would pay us now).
        pnl = current_net  # if positive, we keep some credit; if negative, we paid to close
        loss_pct = -pnl / credit_net if credit_net > 0 else 0
        gain_pct = pnl / credit_net if credit_net > 0 else 0
        if loss_pct >= stop_pct:
            exit_minute_final = minute
            exit_reason = "stop"
            exit_net = current_net
            break
        if gain_pct >= profit_take_pct:
            exit_minute_final = minute
            exit_reason = "profit_take"
            exit_net = current_net
            break
    else:
        exit_data = condor_credit_net(panel, strikes, exit_minute)
        if exit_data is None:
            return None
        exit_net = exit_data[0]

    pnl = exit_net
    return {
        "pnl": round(pnl, 4),
        "credit_net": round(credit_net, 4),
        "credit_gross": round(credit_gross, 4),
        "exit_net": round(exit_net, 4),
        "exit_minute": exit_minute_final,
        "exit_reason": exit_reason,
        "total_costs": round(fill_cost(8), 4),
    }


def run_straddle_v2(
    panel: dict[tuple[float, str], pd.Series],
    strike: float,
    entry_minute: int,
    exit_minute: int,
    stop_pct: float = _STOP_LOSS_PCT,
) -> dict | None:
    """Short straddle with stop, net of commission and spread."""
    entry = straddle_prices(panel, strike, entry_minute)
    if entry is None:
        return None
    # Sell call + sell put: receive bids
    credit_net = fill_sell(entry[0]) + fill_sell(entry[1]) - fill_cost(2)
    if credit_net <= 0:
        return None

    exit_minute_final = exit_minute
    exit_reason = "expiry"
    exit_net = None

    for minute in range(entry_minute + 1, exit_minute + 1):
        current = straddle_prices(panel, strike, minute)
        if current is None:
            continue
        # To close: buy back both legs at asks
        close_cost = fill_buy(current[0]) + fill_buy(current[1]) + fill_cost(2)
        pnl = credit_net - close_cost
        loss_pct = -pnl / credit_net if credit_net > 0 else 0
        if loss_pct >= stop_pct:
            exit_minute_final = minute
            exit_reason = "stop"
            exit_net = credit_net - close_cost
            break
    else:
        exit_price = straddle_prices(panel, strike, exit_minute)
        if exit_price is None:
            return None
        close_cost = fill_buy(exit_price[0]) + fill_buy(exit_price[1]) + fill_cost(2)
        exit_net = credit_net - close_cost

    return {
        "pnl": round(exit_net, 4),
        "credit_net": round(credit_net, 4),
        "exit_minute": exit_minute_final,
        "exit_reason": exit_reason,
        "total_costs": round(fill_cost(4), 4),
    }


def run_calendar_v2(
    panel0: dict[tuple[float, str], pd.Series],
    panel1: dict[tuple[float, str], pd.Series],
    strike: float,
    entry_minute: int,
    exit_minute: int,
    stop_pct: float = _STOP_LOSS_PCT,
) -> dict | None:
    """Calendar: sell 0DTE straddle, buy 1DTE straddle, net of all costs."""
    entry_0 = straddle_prices(panel0, strike, entry_minute)
    entry_1 = straddle_prices(panel1, strike, entry_minute)
    if entry_0 is None or entry_1 is None:
        return None
    # Sell 0DTE call + put (receive bids), buy 1DTE call + put (pay asks)
    net_entry = (
        fill_sell(entry_0[0])
        + fill_sell(entry_0[1])
        - fill_buy(entry_1[0])
        - fill_buy(entry_1[1])
        - fill_cost(4)
    )
    if net_entry <= 0:
        return None

    exit_minute_final = exit_minute
    exit_reason = "expiry"
    exit_0 = straddle_prices(panel0, strike, exit_minute)
    exit_1 = straddle_prices(panel1, strike, exit_minute)
    if exit_0 is None or exit_1 is None:
        return None
    # Close: buy back 0DTE (pay asks), sell 1DTE (receive bids)
    net_exit = (
        fill_buy(exit_0[0])
        + fill_buy(exit_0[1])
        - fill_sell(exit_1[0])
        - fill_sell(exit_1[1])
        + fill_cost(4)
    )
    pnl = net_entry + net_exit

    for minute in range(entry_minute + 1, exit_minute + 1):
        e0 = straddle_prices(panel0, strike, minute)
        e1 = straddle_prices(panel1, strike, minute)
        if e0 is None or e1 is None:
            continue
        close_cost = (
            fill_buy(e0[0])
            + fill_buy(e0[1])
            - fill_sell(e1[0])
            - fill_sell(e1[1])
            + fill_cost(4)
        )
        current_pnl = net_entry + close_cost
        loss_pct = -current_pnl / abs(net_entry) if net_entry != 0 else 0
        if loss_pct >= stop_pct:
            exit_minute_final = minute
            exit_reason = "stop"
            net_exit = close_cost
            pnl = net_entry + net_exit
            break

    return {
        "pnl": round(pnl, 4),
        "net_entry": round(net_entry, 4),
        "exit_minute": exit_minute_final,
        "exit_reason": exit_reason,
        "total_costs": round(fill_cost(8), 4),
    }


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    args.data_root = args.data_root.resolve()
    args.out = args.out.resolve()

    sessions = build_sessions(args.data_root)
    print(f"loaded {len(sessions)} sessions", flush=True)

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

        cal = run_calendar_v2(panel0, panel1, atm, 5, 45)
        if cal:
            cal["session"] = key
            calendar_days.append(cal)

        spot_at_0 = _spot_at(spot, 0)
        spot_at_120 = _spot_at(spot, 120)
        if spot_at_0 and spot_at_120:
            morning_ret = abs(spot_at_120 / spot_at_0 - 1) * 100
            if morning_ret < 1.0:
                condor_strikes = find_condor_strikes(panel0, spot_at_120, 1.5, 3.0)
                if condor_strikes:
                    result = run_condor_v2(panel0, condor_strikes, 120, 330)
                    if result:
                        result["session"] = key
                        result["morning_return"] = round(morning_ret, 4)
                        condor_days.append(result)

        ph = run_straddle_v2(panel0, atm, 345, 385)
        if ph:
            ph["session"] = key
            power_days.append(ph)

        if next_session and panel1:
            prior_atm = session_atm_strike(panel1, open_spot)
            if prior_atm:
                entry_on = straddle_prices(panel1, prior_atm, 385)
                next_panel0 = next_session["panel0"]
                exit_on = straddle_prices(next_panel0, prior_atm, 90)
                if entry_on and exit_on:
                    # Buy 1DTE straddle at close: pay asks + commission
                    entry_net = (
                        fill_buy(entry_on[0]) + fill_buy(entry_on[1]) + fill_cost(2)
                    )
                    # Sell 0DTE straddle at 11:00: receive bids + commission
                    exit_net = (
                        fill_sell(exit_on[0]) + fill_sell(exit_on[1]) - fill_cost(2)
                    )
                    pnl = exit_net - entry_net
                    overnight_days.append(
                        {
                            "session": f"{key}->{next_session['session']}",
                            "pnl": round(pnl, 4),
                            "entry_net": round(entry_net, 4),
                            "exit_net": round(exit_net, 4),
                            "total_costs": round(fill_cost(4), 4),
                        }
                    )

    results = {
        "openingCalendar": {"days": calendar_days},
        "middayCondor": {"days": condor_days},
        "powerHourStraddle": {"days": power_days},
        "overnightStraddle": {"days": overnight_days},
    }
    report = {
        "meta": {
            "sessions": len(sessions),
            "generated": datetime.now(UTC).isoformat(),
            "stopLossPct": _STOP_LOSS_PCT,
            "profitTakePct": _PROFIT_TAKE_PCT,
            "commissionPerContract": _COMMISSION_PER_CONTRACT,
            "halfSpreadMin": _HALF_SPREAD_MIN,
            "halfSpreadPct": _HALF_SPREAD_PCT,
            "condorOtmPct": 1.5,
            "condorWingPct": 3.0,
            "morningFilterPct": 1.0,
        },
        "strategies": {name: summarize(data["days"]) for name, data in results.items()},
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        "/* Generated by scripts/experiments/spy_0dte/strategy_backtests_v2.py - do not edit. */\n"
        "export const backtestsV2 = " + json.dumps(report, indent=1) + ";\n",
        encoding="utf-8",
    )
    print(f"wrote {args.out}", flush=True)
    for name, data in results.items():
        summary = report["strategies"][name]
        if summary.get("count", 0) > 0:
            stops = sum(1 for d in data["days"] if d.get("exit_reason") == "stop")
            takes = sum(
                1 for d in data["days"] if d.get("exit_reason") == "profit_take"
            )
            total_costs = sum(d.get("total_costs", 0) for d in data["days"])
            print(
                f"  {name}: {summary['count']} trades, "
                f"total {summary['totalPnl']:+.2f}, "
                f"win rate {summary['winRate']:.0%}, "
                f"stops {stops}, profit-takes {takes}, "
                f"costs {total_costs:.2f}",
                flush=True,
            )
        else:
            print(f"  {name}: 0 trades", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
