"""Thesis access to the published market-lake client contract.

This module deliberately depends on ``lakeclient`` only.  It does not reach into
the lake service, storage implementation, or quant-harness runtime.  Local and
remote service selection is therefore a transport concern and the values
returned by the published file-query contract remain inspectable by Thesis.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from types import TracebackType
from typing import Protocol, Self
from urllib.parse import urlsplit

import httpx

from lakeclient import (
    AssetClass,
    CoverageResponse,
    DatasetRequest,
    DataType,
    EnsureAccepted,
    FileQueryFile,
    FileQueryResponse,
    JobStatus,
    LakeClient,
    read_files,
)


class ThesisLakeError(RuntimeError):
    """Base error for an unusable Thesis market-lake response."""


class LakeProtocolError(ThesisLakeError):
    """The injected market-lake client returned an invalid contract value."""


class LakeUnavailableError(ThesisLakeError):
    """The selected market-lake capability is not available."""


class LakeClientPort(Protocol):
    """Published ``lakeclient`` operations consumed by Thesis."""

    def ensure(self, request: DatasetRequest) -> CoverageResponse | EnsureAccepted: ...

    def wait_for_job(
        self,
        job_id: str,
        *,
        timeout: float = 60.0,
        poll_interval: float = 0.5,
    ) -> JobStatus: ...

    def query_files(
        self, request: DatasetRequest, *, ensure: bool = False
    ) -> FileQueryResponse: ...


FileReader = Callable[[Iterable[FileQueryFile]], object]


@dataclass(frozen=True, slots=True)
class LakeCredentials:
    """Process-local credentials for a market-lake API."""

    bearer_token: str

    def __post_init__(self) -> None:
        if not isinstance(self.bearer_token, str) or not self.bearer_token.strip():
            raise ValueError("bearer_token must be a non-empty string")
        object.__setattr__(self, "bearer_token", self.bearer_token.strip())

    def __repr__(self) -> str:
        return "LakeCredentials(bearer_token='<redacted>')"


@dataclass(frozen=True, slots=True)
class PointInTimeLakeRequest:
    """A bounded, point-in-time lake selection.

    ``as_of`` is an optional caller-owned snapshot boundary.  The published
    lake API is date-bounded, so the adapter rejects returned partitions newer
    than this boundary rather than pretending to provide a finer-grained
    snapshot than the service contract exposes.
    """

    asset_class: AssetClass
    data_type: DataType
    symbols: tuple[str, ...] | str
    start_date: date
    end_date: date
    as_of: datetime | None = None
    underlying: str | None = None
    contract_symbols: tuple[str, ...] = ()

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
        if self.as_of is not None:
            if self.as_of.tzinfo is None or self.as_of.utcoffset() is None:
                raise ValueError("as_of must include a timezone")
            normalized_as_of = self.as_of.astimezone(UTC)
            if self.end_date > normalized_as_of.date():
                raise ValueError("end_date must not be after the as_of date")
            object.__setattr__(self, "as_of", normalized_as_of)
        if isinstance(self.symbols, str):
            values: Sequence[object] = (self.symbols,)
        elif isinstance(self.symbols, Sequence) and not isinstance(self.symbols, bytes):
            values = self.symbols
        else:
            raise TypeError("symbols must be a symbol or sequence of symbols")
        normalized = tuple(_symbol(value, "symbols") for value in values)
        if not normalized:
            raise ValueError("symbols must not be empty")
        if len(normalized) != len(set(normalized)):
            raise ValueError("symbols must not contain duplicates")
        object.__setattr__(self, "symbols", tuple(sorted(normalized)))
        if self.underlying is not None:
            object.__setattr__(
                self, "underlying", _symbol(self.underlying, "underlying")
            )
        contracts = tuple(
            _symbol(value, "contract_symbols") for value in self.contract_symbols
        )
        if len(contracts) != len(set(contracts)):
            raise ValueError("contract_symbols must not contain duplicates")
        object.__setattr__(self, "contract_symbols", tuple(sorted(contracts)))
        if self.asset_class is not AssetClass.OPTION and (
            self.underlying is not None or self.contract_symbols
        ):
            raise ValueError("option selectors are supported for options only")

    @property
    def start(self) -> date:
        """Return the inclusive request start date."""
        return self.start_date

    @property
    def end(self) -> date:
        """Return the inclusive request end date."""
        return self.end_date

    def dataset_requests(self) -> tuple[DatasetRequest, ...]:
        """Build the published dataset requests without adding private fields."""
        if self.asset_class is AssetClass.OPTION:
            contract_symbols: tuple[str, ...] = self.contract_symbols or (
                tuple(self.symbols)
                if isinstance(self.symbols, tuple) and self.underlying is None
                else ()
            )
            return (
                DatasetRequest(
                    asset_class=self.asset_class,
                    data_type=self.data_type,
                    symbol=self.symbols[0] if len(self.symbols) == 1 else None,
                    underlying=self.underlying,
                    contract_symbols=contract_symbols,
                    start_date=self.start_date,
                    end_date=self.end_date,
                ),
            )
        return tuple(
            DatasetRequest(
                asset_class=self.asset_class,
                data_type=self.data_type,
                symbol=symbol,
                start_date=self.start_date,
                end_date=self.end_date,
            )
            for symbol in self.symbols
        )


@dataclass(frozen=True, slots=True)
class PointInTimeLakeLoad:
    """Validated lake files and the injected reader's result."""

    request: PointInTimeLakeRequest
    files: tuple[FileQueryFile, ...]
    data: object
    coverage: tuple[CoverageResponse, ...] = ()

    @property
    def value(self) -> object:
        """Return the data produced by the injected file reader."""
        return self.data

    @property
    def projected_bytes(self) -> int:
        """Return the total byte size advertised by the lake contract."""
        return sum(file.byte_size for file in self.files)


class ThesisLakeClient:
    """Ensure, query, and read point-in-time data through ``lakeclient``."""

    def __init__(
        self,
        client: LakeClientPort,
        *,
        reader: FileReader = read_files,
        job_timeout: float = 60.0,
        job_poll_interval: float = 0.5,
    ) -> None:
        _require_lake_port(client)
        if not callable(reader):
            raise TypeError("reader must be callable")
        if job_timeout < 0 or isinstance(job_timeout, bool):
            raise ValueError("job_timeout must not be negative")
        if job_poll_interval < 0 or isinstance(job_poll_interval, bool):
            raise ValueError("job_poll_interval must not be negative")
        self._client = client
        self._reader = reader
        self._job_timeout = job_timeout
        self._job_poll_interval = job_poll_interval

    def load(self, request: PointInTimeLakeRequest) -> PointInTimeLakeLoad:
        """Ensure complete partitions, then invoke the injected reader once."""
        if not isinstance(request, PointInTimeLakeRequest):
            raise TypeError("request must be a PointInTimeLakeRequest")
        coverage: list[CoverageResponse] = []
        files: list[FileQueryFile] = []
        for dataset_request in request.dataset_requests():
            ensured = self._client.ensure(dataset_request)
            if isinstance(ensured, EnsureAccepted):
                status = self._client.wait_for_job(
                    ensured.job_id,
                    timeout=self._job_timeout,
                    poll_interval=self._job_poll_interval,
                )
                if not status.succeeded:
                    raise ThesisLakeError(
                        f"market-lake ensure job {ensured.job_id!r} failed"
                    )
            elif isinstance(ensured, CoverageResponse):
                coverage.append(ensured)
            else:
                raise LakeProtocolError("invalid market-lake ensure response")
            response = self._client.query_files(dataset_request, ensure=False)
            files.extend(_validate_files(response, request, dataset_request))

        ordered_files = tuple(
            sorted(
                files,
                key=lambda item: (item.symbol, item.session_date, item.object_path),
            )
        )
        return PointInTimeLakeLoad(
            request=request,
            files=ordered_files,
            data=self._reader(ordered_files),
            coverage=tuple(coverage),
        )

    request = load


class LakeApiAdapter:
    """Authenticated HTTP adapter around the published ``LakeClient``."""

    def __init__(
        self,
        base_url: str,
        credentials: LakeCredentials,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float | httpx.Timeout = 30.0,
    ) -> None:
        if not isinstance(credentials, LakeCredentials):
            raise TypeError("credentials must be LakeCredentials")
        _validate_base_url(base_url, require_https=False)
        self._client = LakeClient(
            base_url,
            credentials.bearer_token,
            transport=transport,
            timeout=timeout,
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Close the injected-client wrapper."""
        self._client.close()

    def ensure(self, request: DatasetRequest) -> CoverageResponse | EnsureAccepted:
        return self._client.ensure(request)

    def wait_for_job(
        self,
        job_id: str,
        *,
        timeout: float = 60.0,
        poll_interval: float = 0.5,
    ) -> JobStatus:
        return self._client.wait_for_job(
            job_id, timeout=timeout, poll_interval=poll_interval
        )

    def query_files(
        self, request: DatasetRequest, *, ensure: bool = False
    ) -> FileQueryResponse:
        return self._client.query_files(request, ensure=ensure)


class LocalLakeApiAdapter(LakeApiAdapter):
    """Market-lake API adapter for a local Thesis runtime."""


class RemoteLakeApiAdapter(LakeApiAdapter):
    """Market-lake API adapter for an authenticated remote runtime."""

    def __init__(
        self,
        base_url: str,
        credentials: LakeCredentials,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float | httpx.Timeout = 30.0,
    ) -> None:
        _validate_base_url(base_url, require_https=True)
        super().__init__(
            base_url,
            credentials,
            transport=transport,
            timeout=timeout,
        )


def _validate_files(
    response: FileQueryResponse,
    request: PointInTimeLakeRequest,
    dataset_request: DatasetRequest,
) -> tuple[FileQueryFile, ...]:
    results = response.results
    if results.state.value != "complete" or results.pending_sessions:
        raise ThesisLakeError("market-lake query returned incomplete coverage")
    if (
        results.requested_sessions <= 0
        or results.complete_sessions != results.requested_sessions
        or results.returned_files != results.requested_sessions
        or results.returned_files != len(response.files)
    ):
        raise LakeProtocolError("market-lake file count does not match results")
    if not response.files:
        raise ThesisLakeError("market-lake query returned no complete files")
    allowed_symbols = set(request.symbols)
    if dataset_request.asset_class is AssetClass.OPTION:
        allowed_symbols.update(request.contract_symbols)
    seen: set[tuple[str, date]] = set()
    for file in response.files:
        if file.asset_class is not dataset_request.asset_class:
            raise LakeProtocolError("market-lake returned an unexpected asset class")
        if file.data_type is not dataset_request.data_type:
            raise LakeProtocolError("market-lake returned an unexpected data type")
        if (
            file.symbol.strip().upper() not in allowed_symbols
            and dataset_request.asset_class is not AssetClass.OPTION
        ):
            raise LakeProtocolError("market-lake returned an unrequested symbol")
        if not request.start_date <= file.session_date <= request.end_date:
            raise LakeProtocolError(
                "market-lake returned a file outside the date range"
            )
        if request.as_of is not None and file.session_date > request.as_of.date():
            raise LakeProtocolError("market-lake returned a file after the as_of date")
        key = (file.symbol, file.session_date)
        if key in seen:
            raise LakeProtocolError("market-lake returned duplicate session files")
        seen.add(key)
        if file.byte_size < 0 or file.row_count < 0 or not file.read_url.strip():
            raise LakeProtocolError("market-lake returned invalid file metadata")
    return response.files


def _require_lake_port(client: object) -> None:
    for name in ("ensure", "wait_for_job", "query_files"):
        if not callable(getattr(client, name, None)):
            raise TypeError(f"client must implement LakeClientPort ({name} is missing)")


def _symbol(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must contain strings")
    normalized = value.strip().upper()
    if not normalized or any(character.isspace() for character in normalized):
        raise ValueError(f"{field} must contain non-empty symbols")
    return normalized


def _validate_base_url(value: str, *, require_https: bool) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("base_url must be a non-empty string")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in ({"https"} if require_https else {"http", "https"})
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        scheme = "HTTPS" if require_https else "HTTP or HTTPS"
        raise ValueError(f"base_url must be a {scheme} service URL")


LakeRequest = PointInTimeLakeRequest
MarketLakeClient = ThesisLakeClient
LocalMarketLakeClient = LocalLakeApiAdapter
RemoteMarketLakeClient = RemoteLakeApiAdapter


__all__ = [
    "FileReader",
    "LakeApiAdapter",
    "LakeClientPort",
    "LakeCredentials",
    "LakeProtocolError",
    "LakeRequest",
    "LakeUnavailableError",
    "LocalLakeApiAdapter",
    "LocalMarketLakeClient",
    "MarketLakeClient",
    "PointInTimeLakeLoad",
    "PointInTimeLakeRequest",
    "RemoteLakeApiAdapter",
    "RemoteMarketLakeClient",
    "ThesisLakeClient",
    "ThesisLakeError",
]
