"""Shared helpers for the SPY 0DTE flat-IV decomposition experiment.

Provides closed-session enumeration, deterministic 0DTE OSI contract
construction, chunked session windows, and lake-schema Parquet writing. The
experiment writes under ``.local/spy-0dte/`` in the market-lake partition
layout (``date=/asset_class=/data_type=/*.parquet``) using the exact lake
schemas from ``lake.contracts.schemas``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final

import pyarrow as pa
import pyarrow.parquet as pq

from lake.catalog.models import ContractRecord, OptionType
from lake.contracts.partitions import AssetClass, DataType
from lake.contracts.schemas import schema_for
from lake.coverage import equity_sessions

#: SECONDS_PER_YEAR for the ACT/365 year fraction used everywhere here.
SECONDS_PER_YEAR: Final = 365.0 * 24.0 * 60.0 * 60.0

STATE_FILE: Final = "state.json"


@dataclass(frozen=True, slots=True)
class ContractSpec:
    """One constructed contract (the driver's contract manifest entry)."""

    symbol: str
    strike: float
    option_type: str  # "call" | "put"
    expiry: date | None = None  # None -> session date (0DTE)
    dte: int = 0  # days to expiry from the session date

    def as_record(self, session_date: date) -> ContractRecord:
        expiration = self.expiry if self.expiry is not None else session_date
        return ContractRecord(
            symbol=self.symbol,
            underlying="SPY",
            expiration_date=expiration,
            strike_price=Decimal(str(self.strike)),
            option_type=OptionType(self.option_type),
        )


def build_ladder(
    *,
    session_date: date,
    expiry: date,
    dte: int,
    reference_spot: float,
    strike_pct: float,
    strike_step: float,
) -> tuple[ContractSpec, ...]:
    """Construct one expiry's OSI ladder around ``reference_spot``.

    SPY lists essentially every ``strike_step`` strike near the money;
    strikes that were not listed simply return empty provider data and are
    dropped by the ingest.
    """
    root = f"SPY{expiry.strftime('%y%m%d')}"
    low = reference_spot * (1.0 - strike_pct / 100.0)
    high = reference_spot * (1.0 + strike_pct / 100.0)
    first = int(low / strike_step) * strike_step
    specs: list[ContractSpec] = []
    strike = first
    while strike <= high + 1e-9:
        strike_value = round(strike, 4)
        encoded = f"{int(round(strike_value * 1000)):08d}"
        for suffix, option_type in (("C", "call"), ("P", "put")):
            specs.append(
                ContractSpec(
                    symbol=f"{root}{suffix}{encoded}",
                    strike=strike_value,
                    option_type=option_type,
                    expiry=expiry,
                    dte=dte,
                )
            )
        strike += strike_step
    return tuple(specs)


def closed_sessions(
    start: date, end: date, *, now: datetime | None = None
) -> tuple[tuple[date, datetime, datetime], ...]:
    """Return ``(session_date, open_utc, close_utc)`` for closed XNYS sessions."""
    current = datetime.now(UTC) if now is None else now.astimezone(UTC)
    sessions = equity_sessions(start, end, as_of=current)
    return tuple(
        (session.session_date, session.open_time, session.close_time)
        for session in sessions
        if session.close_time <= current
    )


def chunk_windows(
    open_time: datetime, close_time: datetime, minutes: int
) -> tuple[tuple[datetime, datetime], ...]:
    """Split one session into deterministic half-open UTC fetch windows."""
    step = timedelta(minutes=minutes)
    windows: list[tuple[datetime, datetime]] = []
    cursor = open_time
    while cursor < close_time:
        end = min(cursor + step, close_time)
        windows.append((cursor, end))
        cursor = end
    return tuple(windows)


def build_contracts(
    *,
    session_date: date,
    reference_spot: float,
    strike_pct: float,
    strike_step: float,
    next_expiry: date | None = None,
) -> tuple[ContractSpec, ...]:
    """0DTE ladder plus, optionally, the day-before-expiry ladder."""
    specs = list(
        build_ladder(
            session_date=session_date,
            expiry=session_date,
            dte=0,
            reference_spot=reference_spot,
            strike_pct=strike_pct,
            strike_step=strike_step,
        )
    )
    if next_expiry is not None and next_expiry != session_date:
        specs.extend(
            build_ladder(
                session_date=session_date,
                expiry=next_expiry,
                dte=1,
                reference_spot=reference_spot,
                strike_pct=strike_pct,
                strike_step=strike_step,
            )
        )
    return tuple(specs)


def osi_symbol(
    session_date: date, strike: float, option_type: str, *, root: str = "SPY"
) -> str:
    """Build one OSI symbol deterministically."""
    encoded = f"{int(round(strike * 1000)):08d}"
    return f"{root}{session_date.strftime('%y%m%d')}{'C' if option_type == 'call' else 'P'}{encoded}"


def lake_object_dir(
    data_root: Path,
    session_date: date,
    asset_class: AssetClass,
    data_type: DataType,
) -> Path:
    """Return the partition directory for one session dataset under root."""
    return data_root / (
        f"date={session_date.isoformat()}/asset_class={asset_class.value}"
        f"/data_type={data_type.value}"
    )


def write_lake_table(
    *,
    data_root: Path,
    session_date: date,
    asset_class: AssetClass,
    data_type: DataType,
    symbol: str,
    table: pa.Table,
    part_index: int | None = None,
) -> str:
    """Write one lake-schema Parquet object and return its relative path."""
    if part_index is None:
        filename = f"{symbol}.parquet"
    else:
        filename = f"{symbol}.part-{part_index:04d}.parquet"
    relative = (
        (lake_object_dir(data_root, session_date, asset_class, data_type) / filename)
        .relative_to(data_root)
        .as_posix()
    )
    target = data_root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, target, compression="zstd")
    return relative


def delete_lake_parts(
    data_root: Path,
    session_date: date,
    asset_class: AssetClass,
    data_type: DataType,
) -> int:
    """Delete every Parquet object for one session dataset; returns count."""
    directory = lake_object_dir(data_root, session_date, asset_class, data_type)
    if not directory.is_dir():
        return 0
    removed = 0
    for path in directory.glob("*.parquet"):
        path.unlink()
        removed += 1
    return removed


def read_lake_dataset(
    data_root: Path,
    session_date: date,
    asset_class: AssetClass,
    data_type: DataType,
) -> pa.Table:
    """Read every Parquet object for one session partition (parts included)."""
    directory = lake_object_dir(data_root, session_date, asset_class, data_type)
    if not directory.is_dir():
        return schema_for(asset_class, data_type).empty_table()
    tables = [
        pq.read_table(path)
        for path in sorted(directory.glob("*.parquet"))
        if not path.name.endswith(".consolidating.parquet")
    ]
    if not tables:
        return schema_for(asset_class, data_type).empty_table()
    return pa.concat_tables(tables)


def load_state(data_root: Path) -> dict:
    """Load the experiment state file, or an empty skeleton."""
    path = data_root / STATE_FILE
    if not path.is_file():
        return {"sessions": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(data_root: Path, state: dict) -> None:
    """Persist the experiment state file atomically."""
    path = data_root / STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def years_between(close_time: datetime, moment: datetime) -> float:
    """ACT/365 year fraction from ``moment`` to the session close."""
    return (close_time - moment).total_seconds() / SECONDS_PER_YEAR
