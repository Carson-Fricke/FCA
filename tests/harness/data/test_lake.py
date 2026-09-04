from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime
from typing import cast

import pytest

from harness.core.symbols import Symbol
from harness.data.lake import (
    IncompleteMarketDataError,
    LakeClientPort,
    MarketLakeAdapter,
    MarketLakeRequest,
    UnsupportedMarketDataError,
    resolve_sessions,
)
from lake.contracts import AssetClass as LakeAssetClass
from lake.contracts import DataType as LakeDataType
from lake.contracts import ExchangeSession, partition_path
from lakeclient import (
    AssetClass,
    CoverageEntry,
    CoverageResponse,
    CoverageState,
    DatasetRequest,
    DataType,
    EnsureAccepted,
    FileQueryFile,
    FileQueryResponse,
    FileQueryResults,
    FileQueryState,
    JobState,
    JobStatus,
)

START = date(2024, 11, 25)
END = date(2024, 12, 2)
STAMP = datetime(2024, 12, 3, tzinfo=UTC)


class FakeLakeClient:
    def __init__(
        self,
        *,
        accepted: bool = True,
        complete: bool = True,
        state: FileQueryState = FileQueryState.COMPLETE,
    ) -> None:
        self.accepted = accepted
        self.complete = complete
        self.state = state
        self.calls: list[tuple[str, str, object]] = []

    def ensure(self, request: DatasetRequest) -> CoverageResponse | EnsureAccepted:
        assert request.symbol is not None
        self.calls.append(("ensure", request.symbol, request))
        if self.accepted:
            return EnsureAccepted(f"job-{request.symbol}", JobState.QUEUED)
        return _coverage(request, complete=self.complete)

    def wait_for_job(
        self,
        job_id: str,
        *,
        timeout: float = 60.0,
        poll_interval: float = 0.5,
    ) -> JobStatus:
        self.calls.append(("poll", job_id, (timeout, poll_interval)))
        return JobStatus(job_id, JobState.COMPLETE, STAMP, STAMP)

    def query_files(
        self, request: DatasetRequest, *, ensure: bool = False
    ) -> FileQueryResponse:
        assert request.symbol is not None
        self.calls.append(("query", request.symbol, ensure))
        return _files(request, complete=self.complete, state=self.state)


def _request(symbol: str = "AAPL") -> MarketLakeRequest:
    return MarketLakeRequest(
        asset_class=AssetClass.EQUITY,
        data_type=DataType.QUOTE,
        symbols=(symbol,),
        start_date=START,
        end_date=END,
    )


def _sessions() -> tuple[date, ...]:
    return tuple(session.session_date for session in resolve_sessions(_request()))


def _coverage(request: DatasetRequest, *, complete: bool) -> CoverageResponse:
    assert request.symbol is not None
    sessions = resolve_sessions(_request(request.symbol))
    entries = tuple(
        _coverage_entry(session, request, complete=complete) for session in sessions
    )
    return CoverageResponse(
        asset_class=request.asset_class,
        data_type=request.data_type,
        start_date=request.start_date,
        end_date=request.end_date,
        entries=entries,
        symbol=request.symbol,
    )


def _coverage_entry(
    session: ExchangeSession,
    request: DatasetRequest,
    *,
    complete: bool,
) -> CoverageEntry:
    assert request.symbol is not None
    return CoverageEntry(
        session_date=session.session_date,
        open_time=session.open_time,
        close_time=session.close_time,
        is_partial=False,
        status=CoverageState.COMPLETE if complete else CoverageState.PARTIAL,
        object_path=(
            partition_path(
                session_date=session.session_date,
                asset_class=LakeAssetClass(request.asset_class.value),
                data_type=LakeDataType(request.data_type.value),
                symbol=request.symbol,
            )
            if complete
            else None
        ),
        row_count=1 if complete else None,
        byte_size=100 if complete else None,
        checksum=None,
        error_message=None,
    )


def _files(
    request: DatasetRequest,
    *,
    complete: bool,
    state: FileQueryState,
) -> FileQueryResponse:
    assert request.symbol is not None
    dates = _sessions()
    selected_dates = dates if complete else dates[:-1]
    files = tuple(
        FileQueryFile(
            asset_class=request.asset_class,
            data_type=request.data_type,
            symbol=request.symbol,
            session_date=session_date,
            object_path=partition_path(
                session_date=session_date,
                asset_class=LakeAssetClass(request.asset_class.value),
                data_type=LakeDataType(request.data_type.value),
                symbol=request.symbol,
            ),
            url="https://storage.test/not-used",
            sas_url=f"https://storage.test/read/{request.symbol}/{session_date}",
            row_count=1,
            byte_size=100,
            checksum=None,
        )
        for session_date in selected_dates
    )
    count = len(files)
    return FileQueryResponse(
        files=files,
        results=FileQueryResults(
            requested_sessions=len(dates),
            returned_files=count,
            complete_sessions=count,
            pending_sessions=0 if complete else 1,
            state=state,
        ),
        limitations=(),
    )


def test_resolve_sessions_returns_xnys_windows_including_half_day() -> None:
    sessions = resolve_sessions(_request())

    assert tuple(session.session_date for session in sessions) == (
        date(2024, 11, 25),
        date(2024, 11, 26),
        date(2024, 11, 27),
        date(2024, 11, 29),
        date(2024, 12, 2),
    )
    half_day = sessions[3]
    assert half_day.open_time == datetime(2024, 11, 29, 14, 30, tzinfo=UTC)
    assert half_day.close_time == datetime(2024, 11, 29, 18, tzinfo=UTC)


def test_load_ensures_polls_then_reads_two_symbols_through_sas_urls() -> None:
    client = FakeLakeClient()
    read_urls: list[str] = []

    def reader(files: Iterable[FileQueryFile]) -> tuple[str, ...]:
        read_urls.extend(file.read_url for file in files)
        return tuple(read_urls)

    loaded = MarketLakeAdapter(client, reader=reader).load(
        MarketLakeRequest(
            AssetClass.EQUITY, DataType.QUOTE, ("MSFT", "AAPL"), START, END
        )
    )

    assert len(loaded.sessions) == 5
    assert len(loaded.files) == 10
    assert all(url.startswith("https://storage.test/read/") for url in read_urls)
    assert [call[0] for call in client.calls] == [
        "ensure",
        "poll",
        "ensure",
        "poll",
        "query",
        "query",
    ]
    assert all(call[2] is False for call in client.calls if call[0] == "query")
    assert loaded.projected_bytes == 1_000


def test_load_warns_before_reader_at_configured_projected_egress_threshold() -> None:
    client = FakeLakeClient()
    warnings: list[str] = []
    order: list[str] = []

    def warning(message: str) -> None:
        order.append("warning")
        warnings.append(message)

    def reader(files: Iterable[FileQueryFile]) -> tuple[FileQueryFile, ...]:
        order.append("reader")
        return tuple(files)

    loaded = MarketLakeAdapter(
        client,
        reader=reader,
        warning=warning,
        egress_warning_threshold_bytes=500,
    ).load(_request())

    assert loaded.projected_bytes == 500
    assert order == ["warning", "reader"]
    assert warnings == [
        "projected local market-lake egress (500 bytes) meets or exceeds the "
        "configured threshold (500 bytes)"
    ]


def test_load_rejects_incomplete_query_without_invoking_reader() -> None:
    client = FakeLakeClient(complete=False, state=FileQueryState.ENSURE_PENDING)
    read_calls = 0

    def reader(files: Iterable[FileQueryFile]) -> object:
        nonlocal read_calls
        read_calls += 1
        return tuple(files)

    with pytest.raises(IncompleteMarketDataError, match="remains incomplete"):
        MarketLakeAdapter(client, reader=reader).load(_request())

    assert read_calls == 0


def test_load_rejects_unsupported_asset_before_client_access() -> None:
    client = FakeLakeClient()
    request = DatasetRequest(
        asset_class=AssetClass.OPTION,
        data_type=DataType.QUOTE,
        underlying="AAPL",
        start_date=START,
        end_date=END,
    )

    with pytest.raises(UnsupportedMarketDataError, match="does not support option"):
        MarketLakeAdapter(client).load(request)

    assert client.calls == []


def test_immediate_incomplete_ensure_is_rejected_before_file_query() -> None:
    client = FakeLakeClient(accepted=False, complete=False)

    with pytest.raises(IncompleteMarketDataError, match="coverage remains incomplete"):
        MarketLakeAdapter(client).load(_request())

    assert [call[0] for call in client.calls] == ["ensure"]


def test_market_request_canonicalizes_symbols_and_rejects_duplicates() -> None:
    request = MarketLakeRequest(
        AssetClass.EQUITY, DataType.QUOTE, (" msft ", "Aapl"), START, END
    )

    assert request.symbols == ("AAPL", "MSFT")
    assert Symbol("aapl") == "AAPL"
    with pytest.raises(ValueError, match="duplicates"):
        MarketLakeRequest(
            AssetClass.EQUITY, DataType.QUOTE, ("AAPL", "aapl"), START, END
        )


def test_client_factory_resolves_lazily_and_is_reused_across_loads() -> None:
    constructions: list[int] = []

    def factory() -> FakeLakeClient:
        constructions.append(len(constructions))
        return FakeLakeClient()

    def reader(files: Iterable[FileQueryFile]) -> tuple[str, ...]:
        return tuple(file.symbol for file in files)

    adapter = MarketLakeAdapter(client_factory=factory, reader=reader)
    assert constructions == []

    first = adapter.load(_request())
    second = adapter.load(_request(symbol="MSFT"))

    assert constructions == [0]
    assert first.value == ("AAPL", "AAPL", "AAPL", "AAPL", "AAPL")
    assert second.value == ("MSFT", "MSFT", "MSFT", "MSFT", "MSFT")
    assert len(first.files) == 5
    assert len(second.files) == 5


def test_client_factory_must_produce_a_lake_client_port_on_first_load() -> None:
    class NotAClient:
        pass

    adapter = MarketLakeAdapter(
        client_factory=cast("Callable[[], LakeClientPort]", NotAClient)
    )

    with pytest.raises(TypeError, match="LakeClientPort"):
        adapter.load(_request())


def test_client_factory_failure_surfaces_on_first_load_not_construction() -> None:
    def factory() -> FakeLakeClient:
        raise RuntimeError("credential bootstrap failed")

    adapter = MarketLakeAdapter(client_factory=factory)

    with pytest.raises(RuntimeError, match="credential bootstrap failed"):
        adapter.load(_request())


def test_constructor_requires_exactly_one_client_source() -> None:
    client = FakeLakeClient()

    with pytest.raises(TypeError, match="exactly one of client or client_factory"):
        MarketLakeAdapter(client, client_factory=FakeLakeClient)

    with pytest.raises(TypeError, match="exactly one of client or client_factory"):
        MarketLakeAdapter()


def test_client_factory_must_be_callable() -> None:
    with pytest.raises(TypeError, match="client_factory must be callable"):
        MarketLakeAdapter(client_factory=cast("Callable[[], LakeClientPort]", "x"))
