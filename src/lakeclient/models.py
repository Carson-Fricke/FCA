"""Public request and response values for the market-lake API client."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum


class AssetClass(StrEnum):
    """Asset classes accepted by the market-lake API."""

    EQUITY = "equity"
    OPTION = "option"
    CRYPTO = "crypto"


class DataType(StrEnum):
    """Market-data record types accepted by the market-lake API."""

    TRADE = "trade"
    QUOTE = "quote"


class CoverageState(StrEnum):
    """Coverage state for one requested market session."""

    MISSING = "missing"
    EMPTY = "empty"
    PARTIAL = "partial"
    COMPLETE = "complete"
    ERROR = "error"


class JobState(StrEnum):
    """State of an asynchronous ensure job."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETE = "complete"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class FileQueryState(StrEnum):
    """Completeness of a file-query response."""

    COMPLETE = "complete"
    ENSURE_PENDING = "ensure_pending"


@dataclass(frozen=True, slots=True)
class DatasetRequest:
    """A bounded dataset selection shared by ensure and file queries."""

    asset_class: AssetClass
    data_type: DataType
    start_date: date
    end_date: date
    symbol: str | None = None
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

        symbol = _optional_symbol(self.symbol, "symbol")
        underlying = _optional_symbol(self.underlying, "underlying")
        contracts = tuple(
            _required_symbol(value, "contract_symbols")
            for value in self.contract_symbols
        )
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "underlying", underlying)
        object.__setattr__(self, "contract_symbols", contracts)

        if self.asset_class is AssetClass.OPTION:
            if symbol is None and underlying is None and not contracts:
                raise ValueError(
                    "an option symbol, underlying, or contract is required"
                )
        elif symbol is None:
            raise ValueError("symbol is required for equity and crypto requests")
        elif underlying is not None or contracts:
            raise ValueError("option selectors are supported for options only")

    def to_payload(self) -> dict[str, object]:
        """Return the JSON request body expected by the API."""
        payload: dict[str, object] = {
            "asset_class": self.asset_class.value,
            "data_type": self.data_type.value,
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
        }
        if self.symbol is not None:
            payload["symbol"] = self.symbol
        if self.underlying is not None:
            payload["underlying"] = self.underlying
        if self.contract_symbols:
            payload["contract_symbols"] = list(self.contract_symbols)
        return payload


@dataclass(frozen=True, slots=True)
class CoverageEntry:
    """Coverage and object metadata for one expected session."""

    session_date: date
    open_time: datetime
    close_time: datetime
    is_partial: bool
    status: CoverageState
    object_path: str | None
    row_count: int | None
    byte_size: int | None
    checksum: str | None
    error_message: str | None
    contract_symbol: str | None = None


@dataclass(frozen=True, slots=True)
class CoverageResponse:
    """A synchronously satisfied ensure response."""

    asset_class: AssetClass
    data_type: DataType
    start_date: date
    end_date: date
    entries: tuple[CoverageEntry, ...]
    symbol: str | None = None
    underlying: str | None = None
    contracts: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EnsureAccepted:
    """An ensure request accepted for asynchronous work."""

    job_id: str
    state: JobState
    location: str | None = None


@dataclass(frozen=True, slots=True)
class JobStatus:
    """Current persisted state of an ensure job."""

    job_id: str
    state: JobState
    created_at: datetime
    updated_at: datetime
    error_message: str | None = None

    @property
    def is_terminal(self) -> bool:
        """Return whether polling should stop for this state."""
        return self.state in {
            JobState.COMPLETE,
            JobState.SUCCEEDED,
            JobState.FAILED,
            JobState.CANCELLED,
        }

    @property
    def succeeded(self) -> bool:
        """Return whether the job reached a successful terminal state."""
        return self.state in {JobState.COMPLETE, JobState.SUCCEEDED}


@dataclass(frozen=True, slots=True)
class FileQueryFile:
    """One complete Parquet partition and its object-scoped read URL."""

    asset_class: AssetClass
    data_type: DataType
    symbol: str
    session_date: date
    object_path: str
    url: str
    sas_url: str
    row_count: int
    byte_size: int
    checksum: str | None = None

    @property
    def read_url(self) -> str:
        """Return the server-issued URL consumed by file readers."""
        return self.sas_url


@dataclass(frozen=True, slots=True)
class FileQueryResults:
    """Completeness counts for a file query."""

    requested_sessions: int
    returned_files: int
    complete_sessions: int
    pending_sessions: int
    state: FileQueryState


@dataclass(frozen=True, slots=True)
class FileQueryResponse:
    """File URLs and completeness metadata returned by the query API."""

    files: tuple[FileQueryFile, ...]
    results: FileQueryResults
    limitations: tuple[str, ...]


def parse_ensure_response(
    body: dict[str, object], *, location: str | None
) -> CoverageResponse | EnsureAccepted:
    """Parse either shape returned by the ensure endpoint."""
    if "job_id" in body:
        return EnsureAccepted(
            job_id=_string(body, "job_id"),
            state=JobState(_string(body, "state")),
            location=location,
        )

    raw_entries = _list(body, "entries")
    return CoverageResponse(
        asset_class=AssetClass(_string(body, "asset_class")),
        data_type=DataType(_string(body, "data_type")),
        start_date=_date(body, "start_date"),
        end_date=_date(body, "end_date"),
        entries=tuple(
            _coverage_entry(_object(value, "entries")) for value in raw_entries
        ),
        symbol=_optional_string(body, "symbol"),
        underlying=_optional_string(body, "underlying"),
        contracts=tuple(
            _string_value(value, "contracts")
            for value in _optional_list(body, "contracts")
        ),
    )


def parse_job_status(body: dict[str, object]) -> JobStatus:
    """Parse a job polling response."""
    return JobStatus(
        job_id=_string(body, "job_id"),
        state=JobState(_string(body, "state")),
        created_at=_datetime(body, "created_at"),
        updated_at=_datetime(body, "updated_at"),
        error_message=_optional_string(body, "error_message"),
    )


def parse_file_query_response(body: dict[str, object]) -> FileQueryResponse:
    """Parse a file-query response, including an ensure-pending response."""
    raw_results = _object(body.get("results"), "results")
    return FileQueryResponse(
        files=tuple(
            _file_query_file(_object(value, "files")) for value in _list(body, "files")
        ),
        results=FileQueryResults(
            requested_sessions=_integer(raw_results, "requested_sessions"),
            returned_files=_integer(raw_results, "returned_files"),
            complete_sessions=_integer(raw_results, "complete_sessions"),
            pending_sessions=_integer(raw_results, "pending_sessions"),
            state=FileQueryState(_string(raw_results, "state")),
        ),
        limitations=tuple(
            _string_value(value, "limitations") for value in _list(body, "limitations")
        ),
    )


def _coverage_entry(body: dict[str, object]) -> CoverageEntry:
    return CoverageEntry(
        session_date=_date(body, "session_date"),
        open_time=_datetime(body, "open_time"),
        close_time=_datetime(body, "close_time"),
        is_partial=_boolean(body, "is_partial"),
        status=CoverageState(_string(body, "status")),
        object_path=_optional_string(body, "object_path"),
        row_count=_optional_integer(body, "row_count"),
        byte_size=_optional_integer(body, "byte_size"),
        checksum=_optional_string(body, "checksum"),
        error_message=_optional_string(body, "error_message"),
        contract_symbol=_optional_string(body, "contract_symbol"),
    )


def _file_query_file(body: dict[str, object]) -> FileQueryFile:
    return FileQueryFile(
        asset_class=AssetClass(_string(body, "asset_class")),
        data_type=DataType(_string(body, "data_type")),
        symbol=_string(body, "symbol"),
        session_date=_date(body, "session_date"),
        object_path=_string(body, "object_path"),
        url=_string(body, "url"),
        sas_url=_string(body, "sas_url"),
        row_count=_integer(body, "row_count"),
        byte_size=_integer(body, "byte_size"),
        checksum=_optional_string(body, "checksum"),
    )


def _object(value: object, field: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise TypeError(f"{field} must be a JSON object")
    return value


def _list(body: dict[str, object], field: str) -> list[object]:
    value = body.get(field)
    if not isinstance(value, list):
        raise TypeError(f"{field} must be a JSON array")
    return value


def _optional_list(body: dict[str, object], field: str) -> list[object]:
    value = body.get(field)
    if value is None:
        return []
    if not isinstance(value, list):
        raise TypeError(f"{field} must be a JSON array")
    return value


def _string(body: dict[str, object], field: str) -> str:
    return _string_value(body.get(field), field)


def _string_value(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string")
    return value


def _optional_string(body: dict[str, object], field: str) -> str | None:
    value = body.get(field)
    if value is None:
        return None
    return _string_value(value, field)


def _integer(body: dict[str, object], field: str) -> int:
    value = body.get(field)
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{field} must be an integer")
    return value


def _optional_integer(body: dict[str, object], field: str) -> int | None:
    value = body.get(field)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{field} must be an integer")
    return value


def _boolean(body: dict[str, object], field: str) -> bool:
    value = body.get(field)
    if not isinstance(value, bool):
        raise TypeError(f"{field} must be a boolean")
    return value


def _date(body: dict[str, object], field: str) -> date:
    try:
        return date.fromisoformat(_string(body, field))
    except ValueError as error:
        raise ValueError(f"{field} must be an ISO date") from error


def _datetime(body: dict[str, object], field: str) -> datetime:
    try:
        value = datetime.fromisoformat(_string(body, field).replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field} must be an ISO datetime") from error
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return value.astimezone(UTC)


def _required_symbol(value: str, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must contain strings")
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field} must not contain empty strings")
    return stripped


def _optional_symbol(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    return _required_symbol(value, field)


__all__ = [
    "AssetClass",
    "CoverageEntry",
    "CoverageResponse",
    "CoverageState",
    "DataType",
    "DatasetRequest",
    "EnsureAccepted",
    "FileQueryFile",
    "FileQueryResponse",
    "FileQueryResults",
    "FileQueryState",
    "JobState",
    "JobStatus",
]
