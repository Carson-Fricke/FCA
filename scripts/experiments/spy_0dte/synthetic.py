"""Synthetic 0DTE lake generator with known ground truth (pipeline validation).

Produces the same lake-layout Parquet as the real ingest for N closed
sessions, simulating 0DTE option values with the native CRR pricer under a
**known non-uniform theta schedule**: the option's internal clock advances at
``g(t)`` wall-clock speed (g > 1 decays faster than model, g < 1 slower), so
the analysis must recover ``g`` per time-of-day bucket.

Two modes:
- ``warp`` (default): flat IV all day; recovered excess decay == g(t) - 1.
  Validates the decomposition and implied-time metric end to end.
- ``iv``: IV follows an intraday schedule while the clock runs true; the
  implied-time metric then shows IV drift absorbed as time-warp (expected
  behavior of the method, useful as a control).

Run:

    uv run --extra dev python scripts/experiments/spy_0dte/synthetic.py `
        --sessions 6 --data-root .local/spy-0dte-synthetic/lake
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
from ingest import (
    _EQUITY_SYMBOL,
    _OPTION_FILE_SYMBOL,
    equity_quote_table,
    equity_trade_table,
    option_quote_table,
    option_trade_table,
)
from osi import (
    SECONDS_PER_YEAR,
    closed_sessions,
    osi_symbol,
    save_state,
    write_lake_table,
)

from lake.catalog.models import OptionType
from lake.contracts.partitions import AssetClass, DataType
from lake.providers.alpaca.records import (
    NormalizedOptionQuote,
    NormalizedOptionTrade,
    NormalizedQuote,
    NormalizedTrade,
)

_MINUTES = 390
_EXCHANGES = ("CBOE", "ISE", "AMEX", "ARCX", "BATS", "EDGX")
_TOD_EDGES = (
    (0, 30, "open"),
    (30, 120, "morning"),
    (120, 270, "midday"),
    (270, 345, "afternoon"),
    (345, 390, "power"),
)


def g_schedule() -> dict[str, float]:
    return {
        "open": 1.0,
        "morning": 1.25,
        "midday": 1.0,
        "afternoon": 0.75,
        "power": 1.0,
    }


def g_for_minute(minute: int) -> float:
    for start, end, name in _TOD_EDGES:
        if start <= minute < end:
            return g_schedule()[name]
    return 1.0


def vol_multiplier(minute: int) -> float:
    return float(
        1.0
        + 1.2 * np.exp(-minute / 40.0)
        + 0.8 * np.exp(-(390 - minute) / 35.0)
        + 0.3 * np.exp(-(((minute - 200) / 60.0) ** 2))
    )


def cent(value: float) -> Decimal:
    return Decimal(str(round(value, 2)))


def micro_int(value: int) -> int:
    return int(max(1, value))


def synth_session(
    *,
    session_date,
    open_time: datetime,
    rng: np.random.Generator,
    mode: str,
    sigma_base: float,
    strike_pct: float,
    rate: float,
    dividend_yield: float,
) -> dict:
    from harness.quant import crr_price_batch

    minute_offsets = np.arange(_MINUTES) * 60
    starts = [open_time + timedelta(seconds=int(offset)) for offset in minute_offsets]
    eta_minutes = np.array([g_for_minute(index) for index in range(_MINUTES)])
    dt_years = 60.0 / SECONDS_PER_YEAR

    spot0 = float(rng.uniform(560.0, 690.0))
    realized = 0.10 * np.array([vol_multiplier(index) for index in range(_MINUTES)])
    log_rets = rng.normal(
        loc=-0.5 * (realized * dt_years) ** 2,
        scale=realized * np.sqrt(dt_years),
    )
    spots = spot0 * np.exp(np.cumsum(log_rets))

    minute_axis = np.arange(_MINUTES, dtype=float)
    if mode == "iv":
        sigmas = sigma_base * (
            1.0
            + 0.22 * np.exp(-minute_axis / 45.0)
            + 0.15 * np.exp(-(390 - minute_axis) / 50.0)
        )
    else:
        sigmas = np.full(_MINUTES, sigma_base)

    warp = np.empty(_MINUTES + 1)
    warp[0] = _MINUTES * dt_years
    for index in range(_MINUTES):
        warp[index + 1] = max(warp[index] - eta_minutes[index] * dt_years, 1e-7)

    low = spot0 * (1.0 - strike_pct / 100.0)
    high = spot0 * (1.0 + strike_pct / 100.0)
    strike_ladder = np.arange(np.floor(low), np.ceil(high) + 1.0, 1.0)
    contracts: list[dict] = []
    for strike in strike_ladder:
        for option_type in ("call", "put"):
            contracts.append(
                {
                    "symbol": osi_symbol(session_date, float(strike), option_type),
                    "strike": float(strike),
                    "type": option_type,
                }
            )

    strike_grid = np.array([contract["strike"] for contract in contracts])
    call_grid = np.array(
        [contract["type"] == "call" for contract in contracts], dtype=bool
    )
    option_quotes: list[NormalizedOptionQuote] = []
    option_trades: list[NormalizedOptionTrade] = []
    equity_quotes: list[NormalizedQuote] = []
    equity_trades: list[NormalizedTrade] = []

    for index in range(_MINUTES):
        spot = float(spots[index])
        expiry = float(warp[index + 1])
        values = np.asarray(
            crr_price_batch(
                s=spot,
                k=strike_grid,
                t=expiry,
                r=rate,
                q=dividend_yield,
                sigma=float(sigmas[index]),
                is_call=call_grid,
                is_american=True,
                steps=64,
            ).price,
            dtype=float,
        )

        for offset_seconds, jitter_scale in ((7.0, 1.0), (27.0, 0.7), (47.0, 1.2)):
            timestamp = starts[index] + timedelta(seconds=offset_seconds)
            mids = values + rng.normal(0.0, 0.004, values.shape) * jitter_scale
            moneyness = np.abs(strike_grid / spot - 1.0)
            spreads = np.clip(
                0.01 + 0.6 * moneyness + rng.normal(0.0, 0.002, values.shape),
                0.01,
                0.09,
            )
            bids = np.floor((mids - spreads / 2) * 100) / 100
            asks = np.ceil((mids + spreads / 2) * 100) / 100
            for position, contract in enumerate(contracts):
                if asks[position] <= bids[position]:
                    continue
                option_quotes.append(
                    NormalizedOptionQuote(
                        timestamp=timestamp,
                        symbol=contract["symbol"],
                        underlying="SPY",
                        expiration_date=session_date,
                        strike_price=Decimal(str(contract["strike"])),
                        option_type=OptionType(contract["type"]),
                        bid_exchange=rng.choice(_EXCHANGES),
                        bid_price=cent(float(bids[position])),
                        bid_size=micro_int(int(rng.integers(1, 50))),
                        ask_exchange=rng.choice(_EXCHANGES),
                        ask_price=cent(float(asks[position])),
                        ask_size=micro_int(int(rng.integers(1, 50))),
                        condition="regular",
                    )
                )
        if index % 10 == 0:
            trade_timestamp = starts[index] + timedelta(seconds=37)
            trade_prices = values + rng.normal(0.0, 0.02, values.shape)
            for position, contract in enumerate(contracts):
                option_trades.append(
                    NormalizedOptionTrade(
                        timestamp=trade_timestamp,
                        symbol=contract["symbol"],
                        underlying="SPY",
                        expiration_date=session_date,
                        strike_price=Decimal(str(contract["strike"])),
                        option_type=OptionType(contract["type"]),
                        exchange=rng.choice(_EXCHANGES),
                        price=cent(float(trade_prices[position])),
                        size=micro_int(int(rng.integers(1, 25))),
                        condition="regular",
                    )
                )

        for offset_seconds in (5.0, 25.0, 45.0):
            timestamp = starts[index] + timedelta(seconds=offset_seconds)
            half_spread = rng.uniform(0.005, 0.015)
            equity_quotes.append(
                NormalizedQuote(
                    timestamp=timestamp,
                    symbol=_EQUITY_SYMBOL,
                    bid_exchange=rng.choice(_EXCHANGES),
                    bid_price=cent(spot - half_spread),
                    bid_size=micro_int(int(rng.integers(1, 30))) * 100,
                    ask_exchange=rng.choice(_EXCHANGES),
                    ask_price=cent(spot + half_spread),
                    ask_size=micro_int(int(rng.integers(1, 30))) * 100,
                    conditions=(),
                    tape="C",
                )
            )
        equity_trades.append(
            NormalizedTrade(
                timestamp=starts[index] + timedelta(seconds=27),
                symbol=_EQUITY_SYMBOL,
                trade_id=int(rng.integers(1, 2**31)),
                exchange=rng.choice(_EXCHANGES),
                price=cent(spot + float(rng.normal(0.0, 0.02))),
                size=micro_int(int(rng.integers(1, 40))) * 100,
                conditions=(),
                tape="C",
                update=None,
            )
        )

    return {
        "spot0": spot0,
        "contracts": contracts,
        "option_quotes": option_quotes,
        "option_trades": option_trades,
        "equity_quotes": equity_quotes,
        "equity_trades": equity_trades,
        "g": eta_minutes.tolist(),
        "sigmas": sigmas.tolist(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--sessions", type=int, default=6)
    parser.add_argument("--mode", choices=("warp", "iv"), default="warp")
    parser.add_argument("--sigma-base", type=float, default=0.14)
    parser.add_argument("--strike-pct", type=float, default=2.0)
    parser.add_argument("--rate", type=float, default=0.04)
    parser.add_argument("--dividend-yield", type=float, default=0.012)
    parser.add_argument("--seed", type=int, default=20260907)
    args = parser.parse_args(argv)
    args.data_root = args.data_root.resolve()

    rng = np.random.default_rng(args.seed)
    today = datetime.now(UTC).date()
    sessions = closed_sessions(today - timedelta(days=60), today)[-args.sessions :]
    state: dict = {"sessions": {}, "synthetic": {"mode": args.mode}}
    truth: dict = {"mode": args.mode, "sessions": {}}

    for session_date, open_time, close_time in sessions:
        print(f"synthesizing {session_date.isoformat()} ({args.mode})", flush=True)
        generated = synth_session(
            session_date=session_date,
            open_time=open_time,
            rng=rng,
            mode=args.mode,
            sigma_base=args.sigma_base,
            strike_pct=args.strike_pct,
            rate=args.rate,
            dividend_yield=args.dividend_yield,
        )
        key = session_date.isoformat()
        entry = state["sessions"].setdefault(key, {})
        entry["spot"] = generated["spot0"]
        entry["open"] = open_time.isoformat()
        entry["close"] = close_time.isoformat()
        entry["contracts"] = generated["contracts"]
        entry["feed_equity"] = "sip"
        entry["feed_option"] = "opra"
        entry["datasets"] = {
            "equity/quote": "done",
            "equity/trade": "done",
            "option/quote": "done",
            "option/trade": "done",
        }
        payload = {
            "equity/quote": equity_quote_table(generated["equity_quotes"]),
            "equity/trade": equity_trade_table(generated["equity_trades"]),
            "option/quote": option_quote_table(generated["option_quotes"]),
            "option/trade": option_trade_table(generated["option_trades"]),
        }
        for name, table in payload.items():
            asset_name, data_name = name.split("/")
            write_lake_table(
                data_root=args.data_root,
                session_date=session_date,
                asset_class=AssetClass(asset_name),
                data_type=DataType(data_name),
                symbol=_OPTION_FILE_SYMBOL,
                table=table,
                part_index=0,
            )
            entry["rows"] = {**entry.get("rows", {}), name: table.num_rows}
        truth["sessions"][key] = {
            "spot0": generated["spot0"],
            "g_by_minute": generated["g"],
            "sigma_by_minute": generated["sigmas"],
            "g_schedule": g_schedule(),
        }
        save_state(args.data_root, state)

    truth_path = args.data_root.parent / "synthetic-truth.json"
    truth_path.write_text(json.dumps(truth), encoding="utf-8")
    print(f"wrote truth schedule to {truth_path}", flush=True)
    print("synthetic ingest complete", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
