"""Bounded, reproducible Alpaca history coverage probe.

The probe intentionally uses the existing :class:`AlpacaClient` boundary rather
than making a second provider adapter.  It is useful for checking whether
historical market-data responses still contain representative symbols after a
delisting, symbol reuse, acquisition, or bankruptcy.  A missing row is not
interpreted as proof that a symbol never existed.

Real provider access is opt-in in the command line entry point.  Reports never
include response bodies, request headers, exception messages, or credentials.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Literal, Protocol, cast

from lake.configuration import load_settings
from lake.providers.alpaca import (
    AlpacaClient,
    AlpacaHTTPError,
    AlpacaProtocolError,
)

ProbeDataType = Literal["trades", "quotes"]
ProbeStatus = Literal["records", "empty", "error"]
Recommendation = Literal["go", "no-go"]


class TimestampedRecord(Protocol):
    """The small record surface needed by the coverage probe."""

    @property
    def timestamp(self) -> datetime:
        """Return the record timestamp."""


class AlpacaHistoryProvider(Protocol):
    """Provider boundary implemented by ``AlpacaClient`` and offline fakes."""

    async def get_trades(
        self,
        symbol: str,
        start: date | datetime | str,
        end: date | datetime | str,
        *,
        feed: Literal["sip"] = "sip",
        adjustment: Literal["raw"] = "raw",
        limit: int = 10_000,
    ) -> Sequence[TimestampedRecord]:
        """Fetch historical trades for one symbol."""

    async def get_quotes(
        self,
        symbol: str,
        start: date | datetime | str,
        end: date | datetime | str,
        *,
        feed: Literal["sip"] = "sip",
        adjustment: Literal["raw"] = "raw",
        limit: int = 10_000,
    ) -> Sequence[TimestampedRecord]:
        """Fetch historical quotes for one symbol."""


@dataclass(frozen=True, slots=True)
class ProbeSymbol:
    """A representative symbol and the lifecycle cohort it exercises."""

    symbol: str
    cohort: str = "custom"
    event_year: int | None = None

    def __post_init__(self) -> None:
        normalized = self.symbol.strip().upper()
        if not normalized or any(character.isspace() for character in normalized):
            raise ValueError(f"invalid probe symbol: {self.symbol!r}")
        if not self.cohort or any(character.isspace() for character in self.cohort):
            raise ValueError("probe cohort must be a non-empty token")
        if self.event_year is not None and not 1900 <= self.event_year <= 2100:
            raise ValueError("probe event_year must be between 1900 and 2100")
        object.__setattr__(self, "symbol", normalized)

    def as_dict(self) -> dict[str, object]:
        """Return the stable, JSON-safe symbol metadata representation."""
        return {
            "symbol": self.symbol,
            "cohort": self.cohort,
            "event_year": self.event_year,
        }


# The cohorts deliberately include both common Q-tickers and ordinary symbols:
# a provider can treat those cases differently, and the probe should expose
# that difference rather than assuming a Q suffix means "not found".
DEFAULT_SYMBOLS: tuple[ProbeSymbol, ...] = (
    ProbeSymbol("AGN", "acquisition", 2020),
    ProbeSymbol("ALXN", "acquisition", 2021),
    ProbeSymbol("ATVI", "acquisition", 2023),
    ProbeSymbol("BCR", "acquisition", 2017),
    ProbeSymbol("CELG", "acquisition", 2019),
    ProbeSymbol("CTXS", "acquisition", 2022),
    ProbeSymbol("ESRX", "acquisition", 2018),
    ProbeSymbol("MON", "acquisition", 2018),
    ProbeSymbol("MYL", "acquisition", 2020),
    ProbeSymbol("NBL", "acquisition", 2020),
    ProbeSymbol("RHT", "acquisition", 2019),
    ProbeSymbol("SPLK", "acquisition", 2024),
    ProbeSymbol("TIF", "acquisition", 2021),
    ProbeSymbol("TWTR", "acquisition", 2022),
    ProbeSymbol("WORK", "acquisition", 2021),
    ProbeSymbol("BBBYQ", "bankruptcy", 2023),
    ProbeSymbol("CHKAQ", "bankruptcy", 2017),
    ProbeSymbol("DNDNQ", "bankruptcy", 2016),
    ProbeSymbol("ENRNQ", "bankruptcy", 2001),
    ProbeSymbol("FTR", "bankruptcy", 2020),
    ProbeSymbol("HTZ", "bankruptcy", 2020),
    ProbeSymbol("JCPNQ", "bankruptcy", 2020),
    ProbeSymbol("LEHMQ", "bankruptcy", 2008),
    ProbeSymbol("SIVBQ", "bankruptcy", 2023),
    ProbeSymbol("WAMUQ", "bankruptcy", 2008),
    ProbeSymbol("WCOMQ", "bankruptcy", 2002),
    ProbeSymbol("YRCWQ", "bankruptcy", 2023),
    ProbeSymbol("CHL", "delisted", 2021),
    ProbeSymbol("GM", "reused", 2009),
    ProbeSymbol("META", "reused", 2022),
)


@dataclass(frozen=True, slots=True)
class ProbeConfig:
    """Inputs and hard limits for one reproducible sweep."""

    symbols: tuple[ProbeSymbol | str, ...] = DEFAULT_SYMBOLS
    start: date | datetime | str = "2010-01-01T00:00:00Z"
    end: date | datetime | str = "2025-01-01T00:00:00Z"
    data_type: ProbeDataType = "trades"
    feed: Literal["sip"] = "sip"
    adjustment: Literal["raw"] = "raw"
    limit: int = 100
    max_records: int = 1
    max_symbols: int = 30

    def __post_init__(self) -> None:
        normalized_symbols = _normalize_probe_symbols(self.symbols)
        if not normalized_symbols:
            raise ValueError("at least one probe symbol is required")
        if len(normalized_symbols) > self.max_symbols:
            raise ValueError(
                f"probe contains {len(normalized_symbols)} symbols; "
                f"max_symbols is {self.max_symbols}"
            )
        if self.data_type not in {"trades", "quotes"}:
            raise ValueError("data_type must be trades or quotes")
        if self.feed != "sip":
            raise ValueError("feed must be sip")
        if self.adjustment != "raw":
            raise ValueError("adjustment must be raw")
        if (
            not isinstance(self.limit, int)
            or isinstance(self.limit, bool)
            or not 1 <= self.limit <= 10_000
        ):
            raise ValueError("limit must be an integer from 1 through 10000")
        if (
            not isinstance(self.max_records, int)
            or isinstance(self.max_records, bool)
            or not 1 <= self.max_records <= 100
        ):
            raise ValueError("max_records must be an integer from 1 through 100")
        if (
            not isinstance(self.max_symbols, int)
            or isinstance(self.max_symbols, bool)
            or not 1 <= self.max_symbols <= 100
        ):
            raise ValueError("max_symbols must be an integer from 1 through 100")
        object.__setattr__(self, "symbols", normalized_symbols)
        object.__setattr__(self, "start", _canonical_bound(self.start))
        object.__setattr__(self, "end", _canonical_bound(self.end))

    @property
    def symbol_names(self) -> tuple[str, ...]:
        """Return normalized symbols in deterministic order."""
        return tuple(
            symbol.symbol for symbol in cast(tuple[ProbeSymbol, ...], self.symbols)
        )

    def as_dict(self) -> dict[str, object]:
        """Return the stable top-level input representation."""
        return {
            "symbols": [
                symbol.as_dict()
                for symbol in cast(tuple[ProbeSymbol, ...], self.symbols)
            ],
            "start": self.start,
            "end": self.end,
            "data_type": self.data_type,
            "feed": self.feed,
            "adjustment": self.adjustment,
            "limit": self.limit,
            "max_records": self.max_records,
            "max_symbols": self.max_symbols,
        }


@dataclass(frozen=True, slots=True)
class SymbolProbeResult:
    """Safe per-symbol outcome; no provider payload is retained."""

    symbol: str
    cohort: str
    event_year: int | None
    status: ProbeStatus
    record_count: int
    first_timestamp: str | None
    last_timestamp: str | None
    error_code: str | None

    def as_dict(self, config: ProbeConfig) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "cohort": self.cohort,
            "event_year": self.event_year,
            "inputs": {
                "start": config.start,
                "end": config.end,
                "data_type": config.data_type,
                "feed": config.feed,
                "adjustment": config.adjustment,
                "limit": config.limit,
                "max_records": config.max_records,
            },
            "result": {
                "status": self.status,
                "record_count": self.record_count,
                "first_timestamp": self.first_timestamp,
                "last_timestamp": self.last_timestamp,
                "error_code": self.error_code,
            },
        }


@dataclass(frozen=True, slots=True)
class ProbeReport:
    """Deterministic report produced by :func:`probe_symbols`."""

    config: ProbeConfig
    results: tuple[SymbolProbeResult, ...]
    recommendation: Recommendation
    recommendation_reason: str

    def as_dict(self) -> dict[str, object]:
        counts = {status: 0 for status in ("records", "empty", "error")}
        for result in self.results:
            counts[result.status] += 1
        return {
            "schema_version": 1,
            "probe": "alpaca-delisted-history",
            "inputs": self.config.as_dict(),
            "results": [result.as_dict(self.config) for result in self.results],
            "summary": {
                "symbols": len(self.results),
                "records": counts["records"],
                "empty": counts["empty"],
                "errors": counts["error"],
            },
            "recommendation": self.recommendation,
            "recommendation_reason": self.recommendation_reason,
        }


async def probe_symbols(
    provider: AlpacaHistoryProvider,
    config: ProbeConfig | None = None,
) -> ProbeReport:
    """Probe each configured symbol through an injected provider boundary.

    Calls are intentionally sequential.  This gives the sweep a simple and
    auditable request budget of at most one bounded history operation per
    symbol, while the existing client applies its own retry policy if needed.
    """
    resolved_config = ProbeConfig() if config is None else config
    results: list[SymbolProbeResult] = []
    for symbol in cast(tuple[ProbeSymbol, ...], resolved_config.symbols):
        results.append(await _probe_one(provider, resolved_config, symbol))

    result_tuple = tuple(results)
    if any(result.status != "records" for result in result_tuple):
        recommendation: Recommendation = "go"
        reason = (
            "At least one symbol was empty or errored. Supplemental listings "
            "data should provide lifecycle and identity evidence instead of "
            "inferring delisting from market-data absence."
        )
    else:
        recommendation = "no-go"
        reason = (
            "Every probe symbol returned bounded history in this window; the "
            "sweep found no demonstrated listings-coverage gap."
        )
    return ProbeReport(resolved_config, result_tuple, recommendation, reason)


async def _probe_one(
    provider: AlpacaHistoryProvider,
    config: ProbeConfig,
    symbol: ProbeSymbol,
) -> SymbolProbeResult:
    try:
        method_name = f"get_{config.data_type}"
        method = cast(
            Callable[..., Awaitable[Sequence[TimestampedRecord]]],
            getattr(provider, method_name),
        )
        records = tuple(
            (
                await method(
                    symbol.symbol,
                    config.start,
                    config.end,
                    feed=config.feed,
                    adjustment=config.adjustment,
                    limit=config.limit,
                )
            )[: config.max_records]
        )
        timestamps = sorted(_format_timestamp(record.timestamp) for record in records)
    except Exception as error:
        return SymbolProbeResult(
            symbol=symbol.symbol,
            cohort=symbol.cohort,
            event_year=symbol.event_year,
            status="error",
            record_count=0,
            first_timestamp=None,
            last_timestamp=None,
            error_code=_safe_error_code(error),
        )

    return SymbolProbeResult(
        symbol=symbol.symbol,
        cohort=symbol.cohort,
        event_year=symbol.event_year,
        status="records" if records else "empty",
        record_count=len(records),
        first_timestamp=timestamps[0] if timestamps else None,
        last_timestamp=timestamps[-1] if timestamps else None,
        error_code=None,
    )


def serialize_report(report: ProbeReport) -> str:
    """Serialize a report without timestamps or other run-specific fields."""
    return (
        json.dumps(
            report.as_dict(),
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    provider: AlpacaHistoryProvider | None = None,
    settings_root: str | Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    """Run the probe CLI; real Alpaca access requires the explicit ``--live``."""
    parser = _parser()
    arguments = parser.parse_args(argv)
    if provider is None and not arguments.live:
        print(
            "alpaca-delisted-probe: refusing provider access without --live",
            file=sys.stderr,
        )
        return 2

    try:
        symbols = _read_symbols(arguments.symbols, arguments.symbols_file)
        config = ProbeConfig(
            symbols=symbols,
            start=arguments.start,
            end=arguments.end,
            data_type=arguments.data_type,
            limit=arguments.limit,
            max_records=arguments.max_records,
            max_symbols=arguments.max_symbols,
        )
        report = asyncio.run(
            _run_with_provider(
                provider,
                config,
                settings_root=settings_root,
                environ=environ,
            )
        )
        serialized = serialize_report(report)
        if arguments.output == "-":
            sys.stdout.write(serialized)
        else:
            Path(arguments.output).write_text(serialized, encoding="utf-8")
        return 0
    except (OSError, ValueError) as error:
        print(f"alpaca-delisted-probe: error: {error}", file=sys.stderr)
        return 2


async def _run_with_provider(
    provider: AlpacaHistoryProvider | None,
    config: ProbeConfig,
    *,
    settings_root: str | Path | None,
    environ: Mapping[str, str] | None,
) -> ProbeReport:
    if provider is not None:
        return await probe_symbols(provider, config)
    settings = load_settings(settings_root, environ=environ)
    async with AlpacaClient.from_settings(settings.provider) as client:
        return await probe_symbols(client, config)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="alpaca-delisted-probe",
        description="Run a bounded Alpaca historical-symbol coverage sweep.",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="explicitly permit Alpaca access using runtime environment secrets",
    )
    parser.add_argument(
        "--symbols",
        help="comma-separated symbols; defaults to the 30 representative cohorts",
    )
    parser.add_argument(
        "--symbols-file",
        help="UTF-8 file containing one symbol per line (comments start with #)",
    )
    parser.add_argument("--start", default="2010-01-01T00:00:00Z")
    parser.add_argument("--end", default="2025-01-01T00:00:00Z")
    parser.add_argument("--data-type", choices=("trades", "quotes"), default="trades")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--max-records", type=int, default=1)
    parser.add_argument("--max-symbols", type=int, default=30)
    parser.add_argument(
        "--output",
        default="-",
        help="report path, or - for stdout (default: -)",
    )
    return parser


def _read_symbols(
    symbols_argument: str | None,
    symbols_file: str | None,
) -> tuple[ProbeSymbol | str, ...]:
    if symbols_argument is not None and symbols_file is not None:
        raise ValueError("use only one of --symbols and --symbols-file")
    if symbols_argument is not None:
        values = tuple(part.strip() for part in symbols_argument.split(","))
        return tuple(value for value in values if value)
    if symbols_file is not None:
        values = []
        for line in Path(symbols_file).read_text(encoding="utf-8").splitlines():
            value = line.split("#", maxsplit=1)[0].strip()
            if value:
                values.append(value)
        return tuple(values)
    return DEFAULT_SYMBOLS


def _normalize_probe_symbols(
    symbols: Iterable[ProbeSymbol | str],
) -> tuple[ProbeSymbol, ...]:
    by_symbol: dict[str, ProbeSymbol] = {}
    for value in symbols:
        symbol = value if isinstance(value, ProbeSymbol) else ProbeSymbol(value)
        by_symbol.setdefault(symbol.symbol, symbol)
    return tuple(by_symbol[symbol] for symbol in sorted(by_symbol))


def _canonical_bound(value: date | datetime | str) -> str:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("datetime bounds must be timezone-aware")
        normalized = value.astimezone(UTC)
        return _format_timestamp(normalized)
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise TypeError("bounds must be dates, datetimes, or non-empty strings")


def _format_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("provider timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _safe_error_code(error: Exception) -> str:
    if isinstance(error, AlpacaHTTPError):
        return f"http_{error.status_code}"
    if isinstance(error, AlpacaProtocolError):
        return "protocol_error"
    if isinstance(error, (TimeoutError, ConnectionError)):
        return "transport_error"
    return "provider_error"


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "AlpacaHistoryProvider",
    "DEFAULT_SYMBOLS",
    "ProbeConfig",
    "ProbeReport",
    "ProbeSymbol",
    "SymbolProbeResult",
    "main",
    "probe_symbols",
    "serialize_report",
]
