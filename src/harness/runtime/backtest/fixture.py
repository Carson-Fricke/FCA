"""Session windows, synthetic benchmark data, and local replay fixtures."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path, PurePosixPath
from random import Random
from urllib.parse import unquote

from harness.configuration import BacktestConfig
from harness.core.symbols import Symbol, canonical_symbol
from harness.core.types import (
    Event,
    ParquetCodec,
    QuoteEvent,
    TradeEvent,
)
from harness.data import (
    LakeReplayFeed,
    ReplayFingerprints,
    ReplayStream,
)
from harness.runtime.backtest.errors import (
    _ONE_DAY,
    _ZERO,
    BacktestConfigurationError,
    DataPreflightError,
)
from lake.contracts import AssetClass as LakeAssetClass
from lake.contracts import DataType as LakeDataType
from lake.contracts import ExchangeSession, partition_path
from lakeclient import AssetClass, DataType, FileQueryFile

# --------------------------------------------------------------------------
# Session windows and synthetic benchmark data
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SyntheticDataOptions:
    """Exact knobs for the deterministic local benchmark data fixture."""

    symbol: str = "SPY"
    quotes_per_session: int = 12
    trades_per_session: int = 6
    base_price: Decimal = Decimal("450.00")
    tick: Decimal = Decimal("0.01")
    drift_ticks: int = 4
    spread_ticks: int = 1
    quote_quantity: Decimal = Decimal("100")
    trade_quantity: Decimal = Decimal("100")
    session_open: tuple[int, int] = (13, 30)
    session_close: tuple[int, int] = (20, 0)
    venue: str = "XNYS"

    def __post_init__(self) -> None:
        if self.quotes_per_session < 1:
            raise ValueError("quotes_per_session must be positive")
        if not 0 <= self.trades_per_session <= self.quotes_per_session:
            raise ValueError("trades_per_session must not exceed quotes_per_session")
        if self.base_price <= _ZERO or self.tick <= _ZERO:
            raise ValueError("base_price and tick must be positive")
        if self.drift_ticks < 0 or self.spread_ticks < 1:
            raise ValueError("drift_ticks and spread_ticks must be non-negative")
        if self.quote_quantity <= _ZERO or self.trade_quantity <= _ZERO:
            raise ValueError("quantities must be positive")
        for name, value in (
            ("session_open", self.session_open),
            ("session_close", self.session_close),
        ):
            if len(value) != 2 or not (0 <= value[0] <= 23 and 0 <= value[1] <= 59):
                raise ValueError(f"{name} must be a (hour, minute) pair")


def backtest_sessions(
    configuration: BacktestConfig,
    options: SyntheticDataOptions | None = None,
) -> tuple[ExchangeSession, ...]:
    """Return deterministic UTC weekday sessions inside the run window.

    Session bounds are the SPY-style regular trading window clipped to the
    configured ``start`` and ``end``.  No exchange calendar, network call, or
    wall clock is consulted, so the same configuration always produces the same
    sessions on every machine.
    """
    chosen = SyntheticDataOptions() if options is None else options
    sessions: list[ExchangeSession] = []
    for day in _weekday_dates(configuration):
        session = _session_window(configuration, day, chosen)
        if session is not None:
            sessions.append(session)
    return tuple(sessions)


def _weekday_dates(configuration: BacktestConfig) -> tuple[date, ...]:
    days: list[date] = []
    day = configuration.start.date()
    last = configuration.end.date()
    while day <= last:
        if day.weekday() < 5:
            days.append(day)
        day += _ONE_DAY
    return tuple(days)


def _session_window(
    configuration: BacktestConfig,
    day: date,
    options: SyntheticDataOptions,
) -> ExchangeSession | None:
    open_time = datetime.combine(
        day, time(options.session_open[0], options.session_open[1]), UTC
    )
    close_time = datetime.combine(
        day, time(options.session_close[0], options.session_close[1]), UTC
    )
    start = max(open_time, configuration.start)
    end = min(close_time, configuration.end)
    if end <= start:
        return None
    return ExchangeSession(day, start, end)


def _synthetic_events(
    configuration: BacktestConfig,
    options: SyntheticDataOptions,
) -> tuple[tuple[QuoteEvent, ...], tuple[TradeEvent, ...]]:
    """Build exact quote and trade events from the configured seed."""
    symbol = Symbol(str(canonical_symbol(options.symbol)))
    rng = Random(configuration.seed)
    half_spread = options.tick * options.spread_ticks / 2
    floor = options.tick * 100
    quotes: list[QuoteEvent] = []
    trades: list[TradeEvent] = []
    quote_sequence = 0
    trade_sequence = 0
    mid = options.base_price
    for session in backtest_sessions(configuration, options):
        steps = options.quotes_per_session
        span_us = int(
            (session.close_time - session.open_time).total_seconds() * 1_000_000
        )
        step_us = span_us // steps
        stamps: list[datetime] = []
        mids: list[Decimal] = []
        for index in range(steps):
            stamp = session.open_time + timedelta(microseconds=step_us * index)
            drift = rng.randint(-options.drift_ticks, options.drift_ticks)
            mid = mid + drift * options.tick
            if mid < floor:
                mid = floor
            bid = mid - half_spread
            ask = mid + half_spread
            quotes.append(
                QuoteEvent(
                    f"{symbol}-q-{session.session_date.isoformat()}-{index}",
                    stamp,
                    quote_sequence,
                    symbol,
                    bid,
                    options.quote_quantity,
                    ask,
                    options.quote_quantity,
                    options.venue,
                )
            )
            quote_sequence += 1
            stamps.append(stamp)
            mids.append(mid)
        for index in range(options.trades_per_session):
            position = ((index + 1) * steps) // (options.trades_per_session + 1)
            position = min(position, steps - 1)
            trades.append(
                TradeEvent(
                    f"{symbol}-t-{session.session_date.isoformat()}-{index}",
                    stamps[position] + timedelta(microseconds=step_us // 2),
                    trade_sequence,
                    symbol,
                    mids[position],
                    options.trade_quantity,
                    options.venue,
                    f"{session.session_date.isoformat()}-{index}",
                )
            )
            trade_sequence += 1
    return tuple(quotes), tuple(trades)


# --------------------------------------------------------------------------
# Replay fixtures and local lake directory loading
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReplayFixture:
    """One deterministic, re-iterable replay source for a backtest.

    Streams are replayed through :class:`~harness.data.replay.LakeReplayFeed`.
    ``files`` address complete Parquet partitions under ``directory`` using the
    canonical lake layout, and ``streams`` carry in-memory events.  A fixture
    holds no wall clock, randomness, or network state.
    """

    sessions: tuple[ExchangeSession, ...]
    streams: tuple[ReplayStream, ...] = ()
    files: tuple[FileQueryFile, ...] = ()
    source_id: str = "synthetic"
    symbols: tuple[str, ...] = ()
    directory: Path | None = None
    expected_data_fingerprint: str | None = None

    @property
    def file_count(self) -> int:
        """Return the number of complete files addressed by this fixture."""
        return len(self.files)

    @property
    def uses_files(self) -> bool:
        """Return whether this fixture replays Parquet partitions."""
        return bool(self.files)

    def feed(self) -> LakeReplayFeed:
        """Return a fresh, lazy replay feed for this fixture."""
        if not self.sessions:
            raise DataPreflightError("a replay fixture requires at least one session")
        reader: Callable[[FileQueryFile], object] | None = None
        if self.files:
            if self.directory is None:
                raise DataPreflightError(
                    "file-backed replay fixtures require a directory"
                )
            root = self.directory
            reader = lambda file: root / file.object_path  # noqa: E731
        return LakeReplayFeed(
            self.sessions,
            streams=self.streams or None,
            files=self.files,
            reader=reader,
        )

    def materialize(
        self,
    ) -> tuple[tuple[QuoteEvent | TradeEvent, ...], ReplayFingerprints]:
        """Replay the fixture once and return its events and fingerprints."""
        feed = self.feed()
        events = tuple(feed)
        return events, feed.fingerprints


@dataclass(frozen=True, slots=True)
class DataPreflight:
    """Runnability assurance evidence for one local market-data fixture."""

    source_id: str
    session_count: int
    event_count: int
    quote_count: int
    trade_count: int
    file_count: int
    symbols: tuple[str, ...]
    fingerprints: ReplayFingerprints

    def to_json(self) -> dict[str, object]:
        """Return a canonical JSON-ready representation."""
        return {
            "source_id": self.source_id,
            "session_count": self.session_count,
            "event_count": self.event_count,
            "quote_count": self.quote_count,
            "trade_count": self.trade_count,
            "file_count": self.file_count,
            "symbols": list(self.symbols),
            "replay_file_fingerprint": self.fingerprints.file,
            "replay_row_fingerprint": self.fingerprints.row,
            "replay_data_fingerprint": self.fingerprints.data,
        }


def synthetic_fixture(
    configuration: BacktestConfig,
    options: SyntheticDataOptions | None = None,
) -> ReplayFixture:
    """Return an in-memory deterministic benchmark fixture for a run."""
    _require_backtest_config(configuration)
    chosen = SyntheticDataOptions() if options is None else options
    quotes, trades = _synthetic_events(configuration, chosen)
    symbol = str(canonical_symbol(chosen.symbol))
    streams = (
        ReplayStream(symbol, DataType.QUOTE, lambda: quotes),
        ReplayStream(symbol, DataType.TRADE, lambda: trades),
    )
    return ReplayFixture(
        sessions=backtest_sessions(configuration, chosen),
        streams=streams,
        source_id="synthetic",
        symbols=(symbol,),
    )


def write_synthetic_lake(
    configuration: BacktestConfig,
    directory: str | Path,
    options: SyntheticDataOptions | None = None,
) -> ReplayFixture:
    """Write the deterministic benchmark fixture as Parquet partitions.

    Files use the canonical lake layout so the resulting directory is a valid
    user-supplied local lake source.  Nothing is written outside ``directory``.
    """
    _require_backtest_config(configuration)
    chosen = SyntheticDataOptions() if options is None else options
    root = Path(directory)
    quotes, trades = _synthetic_events(configuration, chosen)
    symbol = str(canonical_symbol(chosen.symbol))
    files: list[FileQueryFile] = []
    for session in backtest_sessions(configuration, chosen):
        day = session.session_date
        for data_type, events in (
            (DataType.QUOTE, _events_for_session(quotes, day)),
            (DataType.TRADE, _events_for_session(trades, day)),
        ):
            if not events:
                continue
            object_path = partition_path(
                session_date=day,
                asset_class=LakeAssetClass.EQUITY,
                data_type=LakeDataType(data_type.value),
                symbol=symbol,
            )
            path = root / Path(object_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            ParquetCodec.write(path, events, _record_type(data_type))
            payload = path.read_bytes()
            files.append(
                FileQueryFile(
                    asset_class=AssetClass.EQUITY,
                    data_type=data_type,
                    symbol=symbol,
                    session_date=day,
                    object_path=object_path,
                    url=path.as_posix(),
                    sas_url=path.as_posix(),
                    row_count=len(events),
                    byte_size=len(payload),
                    checksum=hashlib.sha256(payload).hexdigest(),
                )
            )
    sessions = backtest_sessions(configuration, chosen)
    return ReplayFixture(
        sessions=sessions,
        files=tuple(files),
        source_id="local-lake",
        symbols=(symbol,),
        directory=root,
    )


def local_lake_fixture(
    configuration: BacktestConfig,
    directory: str | Path,
) -> ReplayFixture:
    """Return a fixture over a user-supplied local Parquet lake directory."""
    _require_backtest_config(configuration)
    root = Path(directory)
    if not root.is_dir():
        raise DataPreflightError(
            f"local market-data directory does not exist: {root.as_posix()}"
        )
    files = _discover_files(root, configuration)
    if not files:
        raise DataPreflightError(
            f"no quote or trade partitions found under {root.as_posix()}"
        )
    days = sorted({file.session_date for file in files})
    options = SyntheticDataOptions()
    sessions = tuple(
        session
        for session in (_session_window(configuration, day, options) for day in days)
        if session is not None
    )
    if not sessions:
        raise DataPreflightError("local market-data directory has no sessions in range")
    return ReplayFixture(
        sessions=sessions,
        files=files,
        source_id="local-lake",
        symbols=tuple(sorted({file.symbol for file in files})),
        directory=root,
    )


def _discover_files(
    root: Path, configuration: BacktestConfig
) -> tuple[FileQueryFile, ...]:
    wanted = {str(canonical_symbol(symbol)) for symbol in configuration.data.symbols}
    files: list[FileQueryFile] = []
    for path in sorted(root.rglob("*.parquet")):
        record = _parse_partition(root, path)
        if record is None:
            continue
        day, asset_class, data_type, symbol = record
        if day < configuration.start.date() or day > configuration.end.date():
            continue
        if wanted and symbol not in wanted:
            continue
        payload = path.read_bytes()
        files.append(
            FileQueryFile(
                asset_class=asset_class,
                data_type=data_type,
                symbol=symbol,
                session_date=day,
                object_path=partition_path(
                    session_date=day,
                    asset_class=LakeAssetClass(asset_class.value),
                    data_type=LakeDataType(data_type.value),
                    symbol=symbol,
                ),
                url=path.as_posix(),
                sas_url=path.as_posix(),
                row_count=_parquet_row_count(path),
                byte_size=len(payload),
                checksum=hashlib.sha256(payload).hexdigest(),
            )
        )
    return tuple(files)


def _parse_partition(
    root: Path, path: Path
) -> tuple[date, AssetClass, DataType, str] | None:
    parts = path.relative_to(root).parts
    if len(parts) != 4:
        return None
    day = _parse_date_prefix(parts[0], "date")
    asset_class = _parse_value_prefix(parts[1], "asset_class", AssetClass)
    data_type = _parse_value_prefix(parts[2], "data_type", DataType)
    if day is None or asset_class is None or data_type is None:
        return None
    if data_type not in {DataType.QUOTE, DataType.TRADE}:
        return None
    symbol = unquote(PurePosixPath(parts[3]).stem).upper()
    return day, asset_class, data_type, symbol


def _parse_date_prefix(part: str, key: str) -> date | None:
    prefix = f"{key}="
    if not part.startswith(prefix):
        return None
    try:
        return date.fromisoformat(part[len(prefix) :])
    except ValueError:
        return None


def _parse_value_prefix[T: StrEnum](part: str, key: str, enum: type[T]) -> T | None:
    prefix = f"{key}="
    if not part.startswith(prefix):
        return None
    try:
        return enum(part[len(prefix) :])
    except ValueError:
        return None


def _parquet_row_count(path: Path) -> int:
    from pyarrow.parquet import ParquetFile

    return int(ParquetFile(path).metadata.num_rows)


def _events_for_session(events: Sequence[Event], day: date) -> tuple[Event, ...]:
    return tuple(event for event in events if event.timestamp.date() == day)


def _record_type(data_type: DataType) -> type[Event]:
    return QuoteEvent if data_type is DataType.QUOTE else TradeEvent


def _require_backtest_config(configuration: object) -> BacktestConfig:
    if not isinstance(configuration, BacktestConfig):
        raise BacktestConfigurationError(
            "configuration must be a validated BacktestConfig"
        )
    return configuration
