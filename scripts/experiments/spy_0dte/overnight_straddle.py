"""Historical test of the overnight 0DTE straddle hypothesis.

The proposed trade: buy the ATM straddle at the prior session's close (the
contracts are then 1DTE), hold overnight, and sell roughly two hours into the
next session (when they are 0DTE). This script measures exactly that, plus a
"buy at the open, sell 11:30" variant, across every overnight in the sample.

For each session pair (D -> E):

- entry (overnight): median trade mid of the E-expiry ATM call and put over
  D's final ten minutes (15:50-16:00 UTC-4 session close window)
- entry (open): median trade mid over E's first ten minutes (9:30-9:40 ET)
- exit: median trade mid over 11:25-11:35 ET on E
- the straddle is the call plus the put at the strike nearest D's closing
  spot that has both legs priced at both entry and exit

Outputs summary statistics (mean/median/win-rate/percent-of-cost) and a
per-overnight table; writes a JSON summary next to the experiment outputs.

Run:

    uv run --extra dev python scripts/experiments/spy_0dte/overnight_straddle.py `
        --data-root .local/spy-0dte/lake
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from osi import load_state, read_lake_dataset

from lake.contracts.partitions import AssetClass, DataType

_ET = timezone(timedelta(hours=-4))  # sample window is EDT (Jul-Sep)


def window_median(
    option_trades: pd.DataFrame,
    session_date: date,
    start_hour: int,
    start_minute: int,
    minutes: int,
    strike: float,
    option_type: str,
) -> float | None:
    """Median trade price of one contract within an ET window."""
    start = datetime(
        session_date.year,
        session_date.month,
        session_date.day,
        start_hour,
        start_minute,
        tzinfo=_ET,
    )
    end = start + timedelta(minutes=minutes)
    frame = option_trades
    mask = (
        (frame["timestamp"] >= start)
        & (frame["timestamp"] < end)
        & (frame["strike"] == strike)
        & (frame["option_type"] == option_type)
    )
    values = frame.loc[mask, "price"].astype(float)
    if values.empty:
        return None
    return float(values.median())


def load_session_tables(
    data_root: Path, session_date: date
) -> tuple[pd.DataFrame, pd.DataFrame] | None:
    option_trades = read_lake_dataset(
        data_root, session_date, AssetClass.OPTION, DataType.TRADE
    )
    equity_quotes = read_lake_dataset(
        data_root, session_date, AssetClass.EQUITY, DataType.QUOTE
    )
    if option_trades.num_rows == 0 or equity_quotes.num_rows == 0:
        return None
    options = option_trades.select(["timestamp", "symbol", "price"]).to_pandas()
    contracts = load_state(data_root)["sessions"][session_date.isoformat()].get(
        "contracts", []
    )
    manifest = {contract["symbol"]: contract for contract in contracts}
    options = options[options["symbol"].astype(str).isin(manifest)]
    options["strike"] = (
        options["symbol"]
        .astype(str)
        .map(lambda symbol: float(manifest[symbol]["strike"]))
    )
    options["option_type"] = (
        options["symbol"].astype(str).map(lambda symbol: str(manifest[symbol]["type"]))
    )
    options["dte"] = (
        options["symbol"]
        .astype(str)
        .map(lambda symbol: int(manifest[symbol].get("dte", 0)))
    )
    options["timestamp"] = pd.to_datetime(options["timestamp"], utc=True)

    equity = equity_quotes.select(["timestamp", "bid_price", "ask_price"]).to_pandas()
    equity["mid"] = (
        equity["bid_price"].astype(float) + equity["ask_price"].astype(float)
    ) / 2.0
    return options, equity[["timestamp", "mid"]]


def spot_near(equity: pd.DataFrame, moment: datetime, minutes: int = 5) -> float | None:
    end = moment + timedelta(minutes=minutes)
    frame = equity[(equity["timestamp"] >= moment) & (equity["timestamp"] < end)]
    if frame.empty:
        return None
    return float(frame["mid"].median())


def pick_straddle(
    options: pd.DataFrame,
    session_date: date,
    strike_center: float,
    window: tuple[int, int, int],
    dte: int,
    tolerance_pct: float = 2.0,
) -> tuple[float, float, float] | None:
    """Find the strike nearest ``strike_center`` with both legs priced."""
    start_hour, start_minute, minutes = window
    candidates = options[
        (options["dte"] == dte)
        & (options["strike"] >= strike_center * (1 - tolerance_pct / 100))
        & (options["strike"] <= strike_center * (1 + tolerance_pct / 100))
    ]
    priced = candidates.drop_duplicates(subset=["strike", "option_type"])
    for strike in sorted(
        priced["strike"].unique(), key=lambda k: abs(k - strike_center)
    ):
        call = window_median(
            options,
            session_date,
            start_hour,
            start_minute,
            minutes,
            float(strike),
            "call",
        )
        put = window_median(
            options,
            session_date,
            start_hour,
            start_minute,
            minutes,
            float(strike),
            "put",
        )
        if call is not None and put is not None:
            return float(strike), call, put
    return None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    args.data_root = args.data_root.resolve()
    args.out = args.out.resolve() if args.out else None
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    state = load_state(args.data_root)
    sessions = sorted(state.get("sessions", {}))
    rows: list[dict] = []
    for index in range(len(sessions) - 1):
        day_d, day_e = sessions[index], sessions[index + 1]
        d_tables = load_session_tables(args.data_root, date.fromisoformat(day_d))
        e_tables = load_session_tables(args.data_root, date.fromisoformat(day_e))
        if d_tables is None or e_tables is None:
            continue
        d_options, d_equity = d_tables
        e_options, e_equity = e_tables

        close_moment = datetime.fromisoformat(state["sessions"][day_d]["close"])
        close_spot = spot_near(d_equity, close_moment - timedelta(minutes=6))
        if close_spot is None:
            continue

        overnight = pick_straddle(
            d_options, date.fromisoformat(day_d), close_spot, (15, 50, 10), dte=1
        )
        if overnight is None:
            continue
        strike, entry_call_night, entry_put_night = overnight
        date_e = date.fromisoformat(day_e)

        exit_call = window_median(e_options, date_e, 11, 25, 10, strike, "call")
        exit_put = window_median(e_options, date_e, 11, 25, 10, strike, "put")
        open_call = window_median(e_options, date_e, 9, 30, 10, strike, "call")
        open_put = window_median(e_options, date_e, 9, 30, 10, strike, "put")
        if None in (exit_call, exit_put, open_call, open_put):
            continue

        morning_spot = spot_near(
            e_equity,
            datetime.fromisoformat(state["sessions"][day_e]["open"])
            + timedelta(hours=2),
        )
        rows.append(
            {
                "overnight": f"{day_d}->{day_e}",
                "strike": strike,
                "spot_close": close_spot,
                "spot_1130": morning_spot,
                "entry_night": entry_call_night + entry_put_night,
                "entry_open": open_call + open_put,
                "exit_1130": exit_call + exit_put,
            }
        )
    if not rows:
        print("no testable overnights found", flush=True)
        return 1
    frame = pd.DataFrame(rows)
    frame["pnl_night"] = frame["exit_1130"] - frame["entry_night"]
    frame["pnl_open"] = frame["exit_1130"] - frame["entry_open"]
    frame["pct_night"] = frame["pnl_night"] / frame["entry_night"] * 100.0
    frame["pct_open"] = frame["pnl_open"] / frame["entry_open"] * 100.0
    frame["spot_move_pct"] = (frame["spot_1130"] / frame["spot_close"] - 1) * 100.0

    columns = [
        "overnight",
        "strike",
        "spot_move_pct",
        "entry_night",
        "exit_1130",
        "pnl_night",
        "pct_night",
        "entry_open",
        "pnl_open",
        "pct_open",
    ]
    print(
        frame[columns].to_string(index=False, float_format=lambda v: f"{v:.3f}"),
        flush=True,
    )

    def stats(series: pd.Series) -> dict:
        return {
            "mean": float(series.mean()),
            "median": float(series.median()),
            "win_rate": float((series > 0).mean()),
            "worst": float(series.min()),
            "best": float(series.max()),
        }

    summary = {
        "overights": int(len(frame)),
        "buy_night_sell_1130": {
            "pnl_dollars": stats(frame["pnl_night"]),
            "pct_of_cost": stats(frame["pct_night"]),
        },
        "buy_open_sell_1130": {
            "pnl_dollars": stats(frame["pnl_open"]),
            "pct_of_cost": stats(frame["pct_open"]),
        },
    }
    out_path = args.out or args.data_root.parent / "overnight-straddle.json"
    out_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"\nwrote {out_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
