"""Data-source resolution and runnability preflight for backtest runs."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import cast

from harness.configuration import BacktestConfig
from harness.core.symbols import canonical_symbol
from harness.core.types import QuoteEvent, TradeEvent
from harness.data import ReplayStream
from harness.runtime.backtest.errors import (
    BacktestConfigurationError,
    DataPreflightError,
)
from harness.runtime.backtest.fixture import (
    DataPreflight,
    ReplayFixture,
    SyntheticDataOptions,
    _require_backtest_config,
    backtest_sessions,
    local_lake_fixture,
    synthetic_fixture,
)
from lakeclient import DataType


def preflight_data(
    configuration: BacktestConfig,
    fixture: ReplayFixture,
) -> DataPreflight:
    """Validate one fixture against a run and return assurance evidence."""
    _require_backtest_config(configuration)
    if not isinstance(fixture, ReplayFixture):
        raise TypeError("fixture must be a ReplayFixture")
    if not fixture.sessions:
        raise DataPreflightError("a backtest fixture requires at least one session")
    events, fingerprints = fixture.materialize()
    if not events:
        raise DataPreflightError("a backtest fixture must replay at least one event")
    symbols = tuple(sorted({str(canonical_symbol(event.symbol)) for event in events}))
    missing = tuple(
        sorted(
            {str(canonical_symbol(symbol)) for symbol in configuration.data.symbols}
            - set(symbols)
        )
    )
    if missing:
        raise DataPreflightError(
            f"fixture is missing configured symbols: {', '.join(missing)}"
        )
    for event in events:
        if (
            event.timestamp < configuration.start
            or event.timestamp >= configuration.end
        ):
            raise DataPreflightError(
                f"event {event.event_id!r} falls outside the configured run window"
            )
    if (
        fixture.expected_data_fingerprint is not None
        and fixture.expected_data_fingerprint != fingerprints.data
    ):
        raise DataPreflightError(
            "fixture data fingerprint does not match the expected fingerprint"
        )
    return DataPreflight(
        source_id=fixture.source_id,
        session_count=len(fixture.sessions),
        event_count=len(events),
        quote_count=sum(1 for event in events if isinstance(event, QuoteEvent)),
        trade_count=sum(1 for event in events if isinstance(event, TradeEvent)),
        file_count=fixture.file_count,
        symbols=symbols,
        fingerprints=fingerprints,
    )


type MarketDataSource = ReplayFixture | Mapping[object, object] | Path | str


def resolve_data_source(
    configuration: BacktestConfig,
    data_source: MarketDataSource | None = None,
    *,
    options: SyntheticDataOptions | None = None,
) -> ReplayFixture:
    """Resolve the runner's ``data_source`` argument to one fixture.

    ``None`` builds the deterministic in-memory synthetic benchmark fixture, a
    path reads a user-supplied local Parquet lake directory, a mapping is
    treated as replay streams, and a fixture is used unchanged.
    """
    _require_backtest_config(configuration)
    if data_source is None:
        return synthetic_fixture(configuration, options)
    if isinstance(data_source, ReplayFixture):
        return data_source
    if isinstance(data_source, str | Path):
        return local_lake_fixture(configuration, Path(data_source))
    if isinstance(data_source, Mapping):
        return ReplayFixture(
            sessions=backtest_sessions(configuration, options),
            streams=tuple(_streams_from_mapping(data_source)),
            source_id="streams",
            symbols=tuple(
                sorted(
                    {
                        str(canonical_symbol(stream.symbol))
                        for stream in _streams_from_mapping(data_source)
                    }
                )
            ),
        )
    raise BacktestConfigurationError(
        "data_source must be a ReplayFixture, stream mapping, or local path"
    )


def _streams_from_mapping(streams: Mapping[object, object]) -> tuple[ReplayStream, ...]:
    values: list[ReplayStream] = []
    for key, value in streams.items():
        if isinstance(value, ReplayStream):
            values.append(value)
            continue
        data_type = DataType.QUOTE
        if isinstance(key, tuple) and len(key) == 2:
            symbol, data_type = str(key[0]), DataType(str(key[1]))
        else:
            symbol = key if isinstance(key, str) else str(key)
        values.append(
            ReplayStream(
                symbol,
                data_type,
                cast("Iterable[QuoteEvent | TradeEvent]", value),
            )
        )
    return tuple(values)
