"""Synchronous market-lake access at the quant-harness boundary.

The adapter owns the provider-facing sequence of ensure, job polling, and file
query calls.  The returned file objects, including their API-issued SAS URLs,
are passed to an injected reader; no storage credentials or provider client are
created by the harness.
"""

from __future__ import annotations

import warnings
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Protocol

from harness.core.symbols import Symbol
from lake.contracts import AssetClass as LakeContractAssetClass
from lake.contracts import DataType as LakeContractDataType
from lake.contracts import (
    ExchangeSession,
    crypto_utc_days,
    partition_path,
    xnys_sessions,
)
from lakeclient import (
    AssetClass,
    CoverageResponse,
    CoverageState,
    DatasetRequest,
    DataType,
    EnsureAccepted,
    FileQueryFile,
    FileQueryResponse,
    FileQueryState,
    JobStatus,
    read_files,
)

DEFAULT_EGRESS_WARNING_THRESHOLD_BYTES = 1_000_000_000


class MarketLakeError(RuntimeError):
    """Base error for an invalid or unusable market-lake response."""


class UnsupportedMarketDataError(MarketLakeError, ValueError):
    """The harness request is outside the supported lake session contract."""


class IncompleteMarketDataError(MarketLakeError, ValueError):
    """The lake did not return every complete partition requested by the run."""


class EnsureJobError(MarketLakeError):
    """An accepted ensure job reached an unsuccessful terminal state."""


class ProjectedEgressWarning(UserWarning):
    """A local read is projected to meet or exceed its configured byte limit."""


class LakeClientPort(Protocol):
    """Published lakeclient operations used by the harness adapter."""

    def ensure(self, request: DatasetRequest) -> CoverageResponse | EnsureAccepted:
        """Ensure one bounded symbol/data-type request."""

    def wait_for_job(
        self,
        job_id: str,
        *,
        timeout: float = 60.0,
        poll_interval: float = 0.5,
    ) -> JobStatus:
        """Poll one accepted ensure job to a terminal state."""

    def query_files(
        self, request: DatasetRequest, *, ensure: bool = False
    ) -> FileQueryResponse:
        """Return complete files and API-issued read URLs."""


FileReader = Callable[[Iterable[FileQueryFile]], object]
WarningSink = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class MarketLakeRequest:
    """A deterministic, multi-symbol request for one lake dataset."""

    asset_class: AssetClass
    data_type: DataType
    symbols: tuple[str, ...] | str
    start_date: date
    end_date: date

    def __post_init__(self) -> None:
        object.__setattr__(self, "asset_class", AssetClass(self.asset_class))
        object.__setattr__(self, "data_type", DataType(self.data_type))
        if isinstance(self.start_date, datetime) or not isinstance(
            self.start_date, date
        ):
            raise TypeError("start_date must be a date, not a datetime")
        if isinstance(self.end_date, datetime) or not isinstance(self.end_date, date):
            raise TypeError("end_date must be a date, not a datetime")
        if self.end_date < self.start_date:
            raise ValueError("end_date must not be before start_date")
        symbols = _canonical_symbols(self.symbols)
        object.__setattr__(self, "symbols", symbols)

    @property
    def start(self) -> date:
        """Alias for the inclusive request start date."""
        return self.start_date

    @property
    def end(self) -> date:
        """Alias for the inclusive request end date."""
        return self.end_date


@dataclass(frozen=True, slots=True)
class MarketLakeLoad:
    """Reader output and validated file metadata for one prepared load."""

    request: MarketLakeRequest
    sessions: tuple[ExchangeSession, ...]
    files: tuple[FileQueryFile, ...]
    data: object
    projected_bytes: int

    @property
    def value(self) -> object:
        """Return the injected reader's output."""
        return self.data


def resolve_sessions(request: MarketLakeRequest) -> tuple[ExchangeSession, ...]:
    """Resolve the request's exchange or UTC-day windows without using a clock."""
    if request.asset_class is AssetClass.EQUITY:
        return xnys_sessions(request.start_date, request.end_date)
    if request.asset_class is AssetClass.CRYPTO:
        return tuple(
            ExchangeSession(
                session_date=session_date,
                open_time=datetime.combine(session_date, datetime.min.time(), UTC),
                close_time=datetime.combine(session_date, datetime.min.time(), UTC)
                + timedelta(days=1),
            )
            for session_date in crypto_utc_days(request.start_date, request.end_date)
        )
    raise UnsupportedMarketDataError(
        f"market-lake adapter does not support {request.asset_class.value} data"
    )


class MarketLakeAdapter:
    """Prepare deterministic harness data through the published lakeclient API."""

    __slots__ = (
        "_client",
        "_egress_warning_threshold_bytes",
        "_job_poll_interval",
        "_job_timeout",
        "_reader",
        "_warning",
    )

    def __init__(
        self,
        client: LakeClientPort,
        *,
        reader: FileReader = read_files,
        warning: WarningSink | None = None,
        egress_warning_threshold_bytes: int
        | None = DEFAULT_EGRESS_WARNING_THRESHOLD_BYTES,
        job_timeout: float = 60.0,
        job_poll_interval: float = 0.5,
    ) -> None:
        _require_client_port(client)
        if not callable(reader):
            raise TypeError("reader must be callable")
        if warning is not None and not callable(warning):
            raise TypeError("warning must be callable")
        _validate_threshold(egress_warning_threshold_bytes)
        if isinstance(job_timeout, bool) or job_timeout < 0:
            raise ValueError("job_timeout must not be negative")
        if isinstance(job_poll_interval, bool) or job_poll_interval < 0:
            raise ValueError("job_poll_interval must not be negative")
        self._client = client
        self._reader = reader
        self._warning = _default_warning if warning is None else warning
        self._egress_warning_threshold_bytes = egress_warning_threshold_bytes
        self._job_timeout = job_timeout
        self._job_poll_interval = job_poll_interval

    @property
    def egress_warning_threshold_bytes(self) -> int | None:
        """Return the configured projected local egress warning threshold."""
        return self._egress_warning_threshold_bytes

    def resolve_sessions(
        self, request: MarketLakeRequest | DatasetRequest
    ) -> tuple[ExchangeSession, ...]:
        """Resolve exact XNYS or UTC session windows for a request."""
        return resolve_sessions(_as_market_request(request))

    def load(
        self,
        request: MarketLakeRequest | DatasetRequest | None = None,
        *,
        asset_class: AssetClass = AssetClass.EQUITY,
        data_type: DataType = DataType.QUOTE,
        symbols: tuple[str, ...] | str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> MarketLakeLoad:
        """Ensure, validate, and read all files needed for one harness load.

        Every symbol is ensured before any file query is made.  Accepted jobs
        are polled through the injected lakeclient, and the reader is invoked
        only after all returned partitions and read URLs pass validation.
        """
        market_request = _request_from_inputs(
            request,
            asset_class=asset_class,
            data_type=data_type,
            symbols=symbols,
            start_date=start_date,
            end_date=end_date,
        )
        if market_request.asset_class not in {AssetClass.EQUITY, AssetClass.CRYPTO}:
            raise UnsupportedMarketDataError(
                f"market-lake adapter does not support {market_request.asset_class.value} data"
            )
        sessions = resolve_sessions(market_request)
        if not sessions:
            raise UnsupportedMarketDataError(
                "requested range contains no supported market sessions"
            )

        requests = tuple(
            DatasetRequest(
                asset_class=market_request.asset_class,
                data_type=market_request.data_type,
                symbol=symbol,
                start_date=market_request.start_date,
                end_date=market_request.end_date,
            )
            for symbol in market_request.symbols
        )
        for dataset_request in requests:
            self._ensure(dataset_request, sessions)

        files = tuple(
            file
            for dataset_request in requests
            for file in self._query(dataset_request, sessions)
        )
        ordered_files = tuple(
            sorted(
                files,
                key=lambda file: (file.symbol, file.session_date, file.object_path),
            )
        )
        projected_bytes = sum(file.byte_size for file in ordered_files)
        if (
            self._egress_warning_threshold_bytes is not None
            and projected_bytes >= self._egress_warning_threshold_bytes
        ):
            self._warning(
                "projected local market-lake egress "
                f"({projected_bytes} bytes) meets or exceeds the configured "
                f"threshold ({self._egress_warning_threshold_bytes} bytes)"
            )
        data = self._reader(ordered_files)
        return MarketLakeLoad(
            request=market_request,
            sessions=sessions,
            files=ordered_files,
            data=data,
            projected_bytes=projected_bytes,
        )

    def _ensure(
        self,
        request: DatasetRequest,
        sessions: tuple[ExchangeSession, ...],
    ) -> None:
        response = self._client.ensure(request)
        if isinstance(response, EnsureAccepted):
            status = self._client.wait_for_job(
                response.job_id,
                timeout=self._job_timeout,
                poll_interval=self._job_poll_interval,
            )
            if not status.succeeded:
                detail = status.error_message or status.state.value
                raise EnsureJobError(
                    f"market-lake ensure job {response.job_id!r} failed: {detail}"
                )
            return
        if isinstance(response, CoverageResponse):
            _validate_coverage_response(response, request, sessions)
            return
        raise MarketLakeError("lakeclient returned an invalid ensure response")

    def _query(
        self,
        request: DatasetRequest,
        sessions: tuple[ExchangeSession, ...],
    ) -> tuple[FileQueryFile, ...]:
        response = self._client.query_files(request, ensure=False)
        return _validate_file_response(response, request, sessions)


def _as_market_request(
    request: MarketLakeRequest | DatasetRequest,
) -> MarketLakeRequest:
    if isinstance(request, MarketLakeRequest):
        return request
    if isinstance(request, DatasetRequest):
        if request.asset_class not in {AssetClass.EQUITY, AssetClass.CRYPTO}:
            raise UnsupportedMarketDataError(
                f"market-lake adapter does not support {request.asset_class.value} data"
            )
        if request.symbol is None:
            raise UnsupportedMarketDataError(
                "a single symbol is required for a harness market-lake load"
            )
        return MarketLakeRequest(
            asset_class=request.asset_class,
            data_type=request.data_type,
            symbols=(request.symbol,),
            start_date=request.start_date,
            end_date=request.end_date,
        )
    raise TypeError("request must be a MarketLakeRequest or DatasetRequest")


def _request_from_inputs(
    request: MarketLakeRequest | DatasetRequest | None,
    *,
    asset_class: AssetClass,
    data_type: DataType,
    symbols: tuple[str, ...] | str | None,
    start_date: date | None,
    end_date: date | None,
) -> MarketLakeRequest:
    if request is not None:
        if any(value is not None for value in (symbols, start_date, end_date)):
            raise TypeError("request cannot be combined with load keyword inputs")
        return _as_market_request(request)
    if symbols is None or start_date is None or end_date is None:
        raise TypeError(
            "symbols, start_date, and end_date are required when request is omitted"
        )
    return MarketLakeRequest(
        asset_class=asset_class,
        data_type=data_type,
        symbols=symbols,
        start_date=start_date,
        end_date=end_date,
    )


def _validate_coverage_response(
    response: CoverageResponse,
    request: DatasetRequest,
    sessions: tuple[ExchangeSession, ...],
) -> None:
    if response.asset_class is not request.asset_class:
        raise MarketLakeError(
            "lakeclient ensure response has an unexpected asset class"
        )
    if response.data_type is not request.data_type:
        raise MarketLakeError("lakeclient ensure response has an unexpected data type")
    if (
        response.start_date != request.start_date
        or response.end_date != request.end_date
    ):
        raise MarketLakeError("lakeclient ensure response has an unexpected date range")
    if (
        response.symbol is not None
        and response.symbol.strip().upper() != request.symbol
    ):
        raise MarketLakeError("lakeclient ensure response has an unexpected symbol")

    expected_sessions = {session.session_date: session for session in sessions}
    expected_dates = set(expected_sessions)
    entries_by_date = {}
    for entry in response.entries:
        if entry.session_date not in expected_dates:
            raise IncompleteMarketDataError(
                f"ensure returned an unexpected session for {request.symbol}"
            )
        if entry.session_date in entries_by_date:
            raise IncompleteMarketDataError(
                f"ensure returned duplicate coverage for {request.symbol}"
            )
        entries_by_date[entry.session_date] = entry
        expected_session = expected_sessions[entry.session_date]
        if (
            entry.open_time != expected_session.open_time
            or entry.close_time != expected_session.close_time
        ):
            raise MarketLakeError(
                f"ensure returned an unexpected XNYS window for {request.symbol} on "
                f"{entry.session_date.isoformat()}"
            )
        if entry.status is not CoverageState.COMPLETE or entry.is_partial:
            raise IncompleteMarketDataError(
                f"coverage remains incomplete for {request.symbol} on "
                f"{entry.session_date.isoformat()}"
            )
        if entry.object_path != _expected_path(request, entry.session_date):
            raise IncompleteMarketDataError(
                f"coverage has no canonical object path for {request.symbol} on "
                f"{entry.session_date.isoformat()}"
            )
    missing = expected_dates.difference(entries_by_date)
    if missing:
        dates = ", ".join(sorted(value.isoformat() for value in missing))
        raise IncompleteMarketDataError(
            f"ensure omitted complete coverage for {request.symbol}: {dates}"
        )


def _validate_file_response(
    response: FileQueryResponse,
    request: DatasetRequest,
    sessions: tuple[ExchangeSession, ...],
) -> tuple[FileQueryFile, ...]:
    expected_dates = {session.session_date for session in sessions}
    expected_count = len(expected_dates)
    results = response.results
    if results.state is not FileQueryState.COMPLETE or results.pending_sessions:
        raise IncompleteMarketDataError(
            f"file query remains incomplete for {request.symbol}"
        )
    if (
        results.requested_sessions != expected_count
        or results.complete_sessions != expected_count
        or results.returned_files != expected_count
    ):
        raise IncompleteMarketDataError(
            f"file query returned incomplete session counts for {request.symbol}"
        )
    if len(response.files) != expected_count:
        raise IncompleteMarketDataError(
            f"file query returned an unexpected file count for {request.symbol}"
        )

    seen_dates: set[date] = set()
    for file in response.files:
        if (
            file.asset_class is not request.asset_class
            or file.data_type is not request.data_type
            or file.symbol.strip().upper() != request.symbol
        ):
            raise MarketLakeError(
                "file query returned a file outside the requested dataset"
            )
        if file.session_date not in expected_dates or file.session_date in seen_dates:
            raise IncompleteMarketDataError(
                f"file query returned an unexpected or duplicate session for {request.symbol}"
            )
        if file.object_path != _expected_path(request, file.session_date):
            raise MarketLakeError("file query returned a non-canonical object path")
        if not file.read_url.strip():
            raise MarketLakeError("file query returned an empty API-issued read URL")
        if file.byte_size < 0 or file.row_count < 0:
            raise MarketLakeError("file query returned negative file metadata")
        seen_dates.add(file.session_date)
    missing = expected_dates.difference(seen_dates)
    if missing:
        dates = ", ".join(sorted(value.isoformat() for value in missing))
        raise IncompleteMarketDataError(
            f"file query omitted requested sessions for {request.symbol}: {dates}"
        )
    return response.files


def _expected_path(request: DatasetRequest, session_date: date) -> str:
    if request.symbol is None:
        raise TypeError("a symbol is required to build a market-lake partition path")
    return partition_path(
        session_date=session_date,
        asset_class=LakeContractAssetClass(request.asset_class.value),
        data_type=LakeContractDataType(request.data_type.value),
        symbol=request.symbol,
    )


def _canonical_symbols(values: tuple[str, ...] | str) -> tuple[str, ...]:
    if isinstance(values, str):
        values = (values,)
    if isinstance(values, bytes) or not isinstance(values, Sequence):
        raise TypeError("symbols must be a symbol or sequence of symbols")
    normalized = tuple(str(Symbol(value)) for value in values)
    if len(normalized) != len(set(normalized)):
        raise ValueError("symbols must not contain duplicates")
    return tuple(sorted(normalized))


def _require_client_port(client: object) -> None:
    for name in ("ensure", "wait_for_job", "query_files"):
        if not callable(getattr(client, name, None)):
            raise TypeError(f"client must implement LakeClientPort ({name} is missing)")


def _validate_threshold(value: int | None) -> None:
    if value is not None and (
        isinstance(value, bool) or not isinstance(value, int) or value < 0
    ):
        raise ValueError(
            "egress_warning_threshold_bytes must be a non-negative integer or None"
        )


def _default_warning(message: str) -> None:
    warnings.warn(message, ProjectedEgressWarning, stacklevel=3)


# Descriptive aliases keep the adapter discoverable without adding another module.
LakeRequest = MarketLakeRequest
LoadedMarketData = MarketLakeLoad
MarketLake = MarketLakeAdapter


__all__ = [
    "DEFAULT_EGRESS_WARNING_THRESHOLD_BYTES",
    "EnsureJobError",
    "FileReader",
    "IncompleteMarketDataError",
    "LakeClientPort",
    "LakeRequest",
    "LoadedMarketData",
    "MarketLake",
    "MarketLakeAdapter",
    "MarketLakeError",
    "MarketLakeLoad",
    "MarketLakeRequest",
    "ProjectedEgressWarning",
    "UnsupportedMarketDataError",
    "WarningSink",
    "resolve_sessions",
]
