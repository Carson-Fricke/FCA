from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import cast

import pytest

from harness.core.symbols import Symbol
from harness.core.types.events import QuoteEvent, TradeEvent
from harness.data.gap_healing import (
    CoverageCalendar,
    CoverageGap,
    HealingAction,
    HealingStatus,
    LakeGapBackfiller,
    LakeSessionCalendar,
    LiveGapDetector,
    LiveGapHealer,
    MarketEvent,
    UncoveredGapError,
)
from harness.data.lake import (
    LakeClientPort,
    MarketLakeError,
    UnsupportedMarketDataError,
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

DAY = date(2024, 3, 1)
STAMP = datetime(2024, 3, 1, 12, 0, tzinfo=UTC)
OUTAGE_START = datetime(2024, 3, 1, 12, 0, 0, tzinfo=UTC)
RESUME = datetime(2024, 3, 1, 12, 2, 5, tzinfo=UTC)
HEAL_END = datetime(2024, 3, 1, 12, 2, 0, tzinfo=UTC)
STALE_AFTER = timedelta(minutes=2)


class ScriptedLakeClient:
    """In-memory LakeClientPort with a call log and scripted responses."""

    def __init__(
        self,
        *,
        ensure_results: Sequence[CoverageResponse | EnsureAccepted | Exception] = (),
        job_results: Sequence[JobStatus] = (),
        query_results: Sequence[FileQueryResponse | Exception] = (),
    ) -> None:
        self._ensure_results = list(ensure_results)
        self._job_results = list(job_results)
        self._query_results = list(query_results)
        self.ensure_calls: list[DatasetRequest] = []
        self.job_calls: list[str] = []
        self.query_calls: list[DatasetRequest] = []

    def ensure(self, request: DatasetRequest) -> CoverageResponse | EnsureAccepted:
        self.ensure_calls.append(request)
        result = self._ensure_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def wait_for_job(
        self,
        job_id: str,
        *,
        timeout: float = 60.0,
        poll_interval: float = 0.5,
    ) -> JobStatus:
        self.job_calls.append(job_id)
        if self._job_results:
            return self._job_results.pop(0)
        return JobStatus(job_id, JobState.SUCCEEDED, STAMP, STAMP)

    def query_files(
        self, request: DatasetRequest, *, ensure: bool = False
    ) -> FileQueryResponse:
        self.query_calls.append(request)
        result = self._query_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


@dataclass(frozen=True, slots=True)
class _FixedCalendar:
    """Deterministic in-memory coverage calendar for clipping tests."""

    windows: tuple[ExchangeSession, ...]

    def sessions(self, start: date, end: date) -> tuple[ExchangeSession, ...]:
        return self.windows


def _quote(
    event_id: str, at: datetime, sequence: int, *, symbol: str = "BTCUSD"
) -> QuoteEvent:
    return QuoteEvent(
        event_id,
        at,
        sequence,
        Symbol(symbol),
        Decimal("100"),
        Decimal("1"),
        Decimal("101"),
        Decimal("1"),
    )


def _trade(
    event_id: str, at: datetime, sequence: int, *, symbol: str = "BTCUSD"
) -> TradeEvent:
    return TradeEvent(
        event_id, at, sequence, Symbol(symbol), Decimal("100.5"), Decimal("1")
    )


def _dataset_request(
    day: date = DAY, *, symbol: str = "BTCUSD", data_type: DataType = DataType.QUOTE
) -> DatasetRequest:
    return DatasetRequest(
        asset_class=AssetClass.CRYPTO,
        data_type=data_type,
        symbol=symbol,
        start_date=day,
        end_date=day,
    )


def _path(request: DatasetRequest, day: date = DAY) -> str:
    assert request.symbol is not None
    return partition_path(
        session_date=day,
        asset_class=LakeAssetClass(request.asset_class.value),
        data_type=LakeDataType(request.data_type.value),
        symbol=request.symbol,
    )


def _entry(
    request: DatasetRequest,
    day: date,
    *,
    status: CoverageState,
    is_partial: bool = False,
    error_message: str | None = None,
) -> CoverageEntry:
    has_object = status in {CoverageState.COMPLETE, CoverageState.PARTIAL}
    return CoverageEntry(
        session_date=day,
        open_time=datetime.combine(day, datetime.min.time(), UTC),
        close_time=datetime.combine(day, datetime.min.time(), UTC) + timedelta(days=1),
        is_partial=is_partial,
        status=status,
        object_path=_path(request, day) if has_object else None,
        row_count=4 if has_object else None,
        byte_size=256 if has_object else None,
        checksum=None,
        error_message=error_message,
    )


def _coverage_response(
    request: DatasetRequest, entries: Sequence[CoverageEntry]
) -> CoverageResponse:
    return CoverageResponse(
        asset_class=request.asset_class,
        data_type=request.data_type,
        start_date=request.start_date,
        end_date=request.end_date,
        entries=tuple(entries),
        symbol=request.symbol,
    )


def _file(request: DatasetRequest, day: date) -> FileQueryFile:
    assert request.symbol is not None
    return FileQueryFile(
        asset_class=request.asset_class,
        data_type=request.data_type,
        symbol=request.symbol,
        session_date=day,
        object_path=_path(request, day),
        url="https://lake.test/not-used",
        sas_url=f"https://lake.test/read/{day.isoformat()}",
        row_count=4,
        byte_size=256,
        checksum=None,
    )


def _query_response(request: DatasetRequest, days: Sequence[date]) -> FileQueryResponse:
    files = tuple(_file(request, day) for day in days)
    return FileQueryResponse(
        files=files,
        results=FileQueryResults(
            requested_sessions=(request.end_date - request.start_date).days + 1,
            returned_files=len(files),
            complete_sessions=len(files),
            pending_sessions=0,
            state=FileQueryState.COMPLETE,
        ),
        limitations=(),
    )


def _partial_client(request: DatasetRequest, *, calls: int) -> ScriptedLakeClient:
    """Script a current-session PARTIAL coverage answer ``calls`` times."""
    return ScriptedLakeClient(
        ensure_results=[
            _coverage_response(
                request,
                [_entry(request, DAY, status=CoverageState.PARTIAL, is_partial=True)],
            )
            for _ in range(calls)
        ],
        query_results=[_query_response(request, (DAY,)) for _ in range(calls)],
    )


def _outage_rows(request: DatasetRequest) -> dict[str, object]:
    return {
        _path(request): (
            _quote("pre-seam", OUTAGE_START, 40),
            _quote("hole-a", datetime(2024, 3, 1, 12, 0, 30, tzinfo=UTC), 41),
            _quote("hole-b", datetime(2024, 3, 1, 12, 1, 30, tzinfo=UTC), 42),
            _quote("post-seam", RESUME, 43),
        )
    }


def _hole_rows(request: DatasetRequest) -> dict[str, object]:
    return {
        _path(request): (
            _quote("hole-a", datetime(2024, 3, 1, 12, 0, 30, tzinfo=UTC), 41),
            _quote("hole-b", datetime(2024, 3, 1, 12, 1, 30, tzinfo=UTC), 42),
        )
    }


def _reader_for(rows: Mapping[str, object]):
    def read(file: FileQueryFile) -> object:
        return rows[file.object_path]

    return read


def _healer(
    client: ScriptedLakeClient,
    rows: Mapping[str, object],
    *,
    stale_after: timedelta = STALE_AFTER,
) -> LiveGapHealer:
    calendar = LakeSessionCalendar(AssetClass.CRYPTO)
    return LiveGapHealer(
        detector=LiveGapDetector(calendar=calendar),
        backfill=LakeGapBackfiller(
            client=client,
            reader=_reader_for(rows),
            calendar=calendar,
            asset_class=AssetClass.CRYPTO,
        ),
        stale_after=stale_after,
    )


def _gap(start: datetime, end: datetime, *, symbol: str = "BTCUSD") -> CoverageGap:
    return CoverageGap(symbol, DataType.QUOTE, start, end)


def test_two_minute_quote_outage_heals_to_continuous_ordered_data() -> None:
    request = _dataset_request()
    client = _partial_client(request, calls=1)
    healer = _healer(client, _outage_rows(request))

    first = healer.feed(_quote("live-1", OUTAGE_START, 7))
    resumed = healer.feed(_quote("live-2", RESUME, 8))
    stream = (*first, *resumed)

    assert [event.event_id for event in stream] == [
        "live-1",
        "hole-a",
        "hole-b",
        "live-2",
    ]
    assert [event.timestamp for event in stream] == sorted(
        event.timestamp for event in stream
    )
    assert client.ensure_calls == [request]
    assert client.query_calls == [request]
    assert client.job_calls == []
    assert healer.duplicate_events_dropped == 0
    assert healer.unordered_events_dropped == 0


def test_seam_events_stay_owned_by_the_live_stream() -> None:
    request = _dataset_request()
    healer = _healer(_partial_client(request, calls=1), _outage_rows(request))

    healer.feed(_quote("live-1", OUTAGE_START, 7))
    resumed = healer.feed(_quote("live-2", RESUME, 8))

    assert [event.event_id for event in resumed] == [
        "hole-a",
        "hole-b",
        "live-2",
    ]
    action = healer.actions[0]
    assert action.status is HealingStatus.HEALED
    assert action.gap == _gap(OUTAGE_START, RESUME)
    assert action.events == 2
    assert action.sessions == (DAY,)
    assert action.detail == "replayed 2 lake events"
    assert healer.duplicate_events_dropped == 0
    assert healer.unordered_events_dropped == 0


def test_late_live_event_inside_healed_interval_is_dropped_and_counted() -> None:
    request = _dataset_request()
    healer = _healer(_partial_client(request, calls=1), _outage_rows(request))
    healer.feed(_quote("live-1", OUTAGE_START, 7))
    healer.feed(_quote("live-2", RESUME, 8))

    late = healer.feed(_quote("late", datetime(2024, 3, 1, 12, 1, 0, tzinfo=UTC), 9))

    assert late == ()
    assert healer.duplicate_events_dropped == 1


def test_unordered_live_event_outside_healed_window_is_dropped() -> None:
    request = _dataset_request()
    healer = _healer(_partial_client(request, calls=1), _outage_rows(request))
    healer.feed(_quote("live-1", OUTAGE_START, 7))
    healer.feed(_quote("live-2", RESUME, 8))

    stale = healer.feed(_quote("old", datetime(2024, 3, 1, 11, 0, 0, tzinfo=UTC), 9))

    assert stale == ()
    assert healer.unordered_events_dropped == 1


def test_tick_heals_silent_partition_without_resuming_event() -> None:
    request = _dataset_request()
    client = _partial_client(request, calls=2)
    healer = _healer(client, _hole_rows(request))
    healer.feed(_quote("live-1", OUTAGE_START, 7))

    healed = healer.tick(now=datetime(2024, 3, 1, 12, 3, 0, tzinfo=UTC))
    resumed = healer.feed(
        _quote("live-2", datetime(2024, 3, 1, 12, 5, 0, tzinfo=UTC), 8)
    )

    assert [event.event_id for event in healed] == ["hole-a", "hole-b"]
    assert [event.event_id for event in resumed] == ["live-2"]
    assert [action.status for action in healer.actions] == [
        HealingStatus.HEALED,
        HealingStatus.HEALED,
    ]
    assert healer.actions[1].events == 0


def test_accepted_ensure_job_is_polled_then_queried() -> None:
    request = _dataset_request()
    client = ScriptedLakeClient(
        ensure_results=[EnsureAccepted("job-1", JobState.QUEUED)],
        job_results=[JobStatus("job-1", JobState.SUCCEEDED, STAMP, STAMP)],
        query_results=[_query_response(request, (DAY,))],
    )
    healer = _healer(client, _outage_rows(request))

    healed = healer.heal([_gap(OUTAGE_START, HEAL_END)])

    assert [event.event_id for event in healed] == ["hole-a", "hole-b"]
    assert client.job_calls == ["job-1"]
    assert [action.status for action in healer.actions] == [HealingStatus.HEALED]


def test_failed_ensure_job_raises_uncovered_and_records_action() -> None:
    request = _dataset_request()
    client = ScriptedLakeClient(
        ensure_results=[EnsureAccepted("job-1", JobState.QUEUED)],
        job_results=[
            JobStatus(
                "job-1",
                JobState.FAILED,
                STAMP,
                STAMP,
                error_message="provider unavailable",
            )
        ],
    )
    healer = _healer(client, _outage_rows(request))
    gap = _gap(OUTAGE_START, HEAL_END)

    with pytest.raises(UncoveredGapError) as excinfo:
        healer.heal([gap])

    assert "provider unavailable" in str(excinfo.value)
    assert excinfo.value.gap == gap
    assert excinfo.value.healed_events == ()
    assert healer.actions == (
        HealingAction(
            gap,
            HealingStatus.UNCOVERED,
            0,
            (),
            "market-lake ensure job 'job-1' failed: provider unavailable",
        ),
    )


def test_missing_coverage_records_uncovered_action() -> None:
    request = _dataset_request()
    client = ScriptedLakeClient(
        ensure_results=[
            _coverage_response(
                request, [_entry(request, DAY, status=CoverageState.MISSING)]
            )
        ],
    )
    healer = _healer(client, _outage_rows(request))
    gap = _gap(OUTAGE_START, HEAL_END)

    with pytest.raises(UncoveredGapError) as excinfo:
        healer.heal([gap])

    assert "coverage is missing" in str(excinfo.value)
    assert healer.actions == (
        HealingAction(
            gap,
            HealingStatus.UNCOVERED,
            0,
            (),
            f"{DAY.isoformat()}: coverage is missing",
        ),
    )


def test_uncovered_error_carries_events_healed_for_earlier_gaps() -> None:
    btc_request = _dataset_request()
    eth_request = _dataset_request(symbol="ETHUSD")
    client = ScriptedLakeClient(
        ensure_results=[
            _coverage_response(
                btc_request,
                [
                    _entry(
                        btc_request, DAY, status=CoverageState.PARTIAL, is_partial=True
                    )
                ],
            ),
            _coverage_response(
                eth_request, [_entry(eth_request, DAY, status=CoverageState.MISSING)]
            ),
        ],
        query_results=[_query_response(btc_request, (DAY,))],
    )
    healer = _healer(client, _outage_rows(btc_request))
    gaps = [
        _gap(OUTAGE_START, HEAL_END, symbol="ETHUSD"),
        _gap(OUTAGE_START, HEAL_END),
    ]

    with pytest.raises(UncoveredGapError) as excinfo:
        healer.heal(gaps)

    assert excinfo.value.gap.symbol == "ETHUSD"
    assert [event.event_id for event in excinfo.value.healed_events] == [
        "hole-a",
        "hole-b",
    ]
    assert [action.status for action in healer.actions] == [
        HealingStatus.HEALED,
        HealingStatus.UNCOVERED,
    ]
    assert client.ensure_calls == [btc_request, eth_request]


def test_empty_session_coverage_heals_without_events_or_query() -> None:
    request = _dataset_request()
    client = ScriptedLakeClient(
        ensure_results=[
            _coverage_response(
                request, [_entry(request, DAY, status=CoverageState.EMPTY)]
            )
        ],
    )
    healer = _healer(client, _outage_rows(request))
    gap = _gap(OUTAGE_START, HEAL_END)

    healed = healer.heal([gap])

    assert healed == ()
    assert client.query_calls == []
    assert healer.actions == (
        HealingAction(gap, HealingStatus.HEALED, 0, (DAY,), "replayed 0 lake events"),
    )


def test_covered_session_without_query_file_raises_uncovered() -> None:
    request = _dataset_request()
    client = ScriptedLakeClient(
        ensure_results=[
            _coverage_response(
                request, [_entry(request, DAY, status=CoverageState.COMPLETE)]
            )
        ],
        query_results=[_query_response(request, ())],
    )
    healer = _healer(client, _outage_rows(request))

    with pytest.raises(UncoveredGapError) as excinfo:
        healer.heal([_gap(OUTAGE_START, HEAL_END)])

    assert "no file for covered session" in str(excinfo.value)


def test_heal_refuses_when_live_already_passed_the_gap() -> None:
    request = _dataset_request()
    healer = _healer(_partial_client(request, calls=1), _outage_rows(request))
    healer.feed(_quote("live-1", OUTAGE_START, 7))
    healer.feed(_quote("live-2", RESUME, 8))

    with pytest.raises(UncoveredGapError) as excinfo:
        healer.heal([_gap(OUTAGE_START, datetime(2024, 3, 1, 12, 1, 0, tzinfo=UTC))])

    assert "live flow already passed the end of the gap" in excinfo.value.reason
    assert excinfo.value.healed_events == ()
    assert [action.status for action in healer.actions] == [
        HealingStatus.HEALED,
        HealingStatus.UNCOVERED,
    ]


def test_backfill_events_at_or_before_live_head_are_dropped() -> None:
    request = _dataset_request()
    healer = _healer(_partial_client(request, calls=1), _outage_rows(request))
    healer.feed(_quote("live-1", OUTAGE_START, 7))
    healer.feed(_quote("mid", datetime(2024, 3, 1, 12, 1, 0, tzinfo=UTC), 8))

    healed = healer.heal([_gap(OUTAGE_START, HEAL_END)])

    assert [event.event_id for event in healed] == ["hole-b"]
    assert healer.unordered_events_dropped == 1
    assert healer.actions[0].detail == "replayed 1 of 2 lake events"


def test_heal_processes_gaps_in_deterministic_order() -> None:
    request = _dataset_request()
    rows = {
        _path(request): (
            _quote("hole-a", datetime(2024, 3, 1, 12, 0, 30, tzinfo=UTC), 41),
            _quote("hole-b", datetime(2024, 3, 1, 12, 2, 30, tzinfo=UTC), 42),
        )
    }
    healer = _healer(_partial_client(request, calls=2), rows)
    gap_two_start = datetime(2024, 3, 1, 12, 2, 0, tzinfo=UTC)
    gap_two_end = datetime(2024, 3, 1, 12, 3, 0, tzinfo=UTC)
    gap_one_end = datetime(2024, 3, 1, 12, 1, 0, tzinfo=UTC)
    unsorted = [
        _gap(gap_two_start, gap_two_end),
        _gap(OUTAGE_START, gap_one_end),
    ]

    healed = healer.heal(unsorted)

    assert [event.event_id for event in healed] == ["hole-a", "hole-b"]
    starts = [action.gap.start for action in healer.actions]
    assert starts == sorted(starts)


def test_detect_clips_staleness_to_expected_coverage_windows() -> None:
    session = ExchangeSession(
        DAY,
        datetime(2024, 3, 1, 12, 0, tzinfo=UTC),
        datetime(2024, 3, 1, 12, 30, tzinfo=UTC),
    )
    detector = LiveGapDetector(calendar=_FixedCalendar((session,)))
    detector.observe(_quote("q", datetime(2024, 3, 1, 12, 5, 0, tzinfo=UTC), 1))

    gaps = detector.detect(
        now=datetime(2024, 3, 1, 12, 40, 0, tzinfo=UTC),
        stale_after=timedelta(minutes=10),
    )

    assert [(gap.start, gap.end) for gap in gaps] == [
        (
            datetime(2024, 3, 1, 12, 5, 0, tzinfo=UTC),
            datetime(2024, 3, 1, 12, 30, 0, tzinfo=UTC),
        )
    ]


def test_detect_requires_at_least_stale_after_silence() -> None:
    detector = LiveGapDetector(calendar=LakeSessionCalendar(AssetClass.CRYPTO))
    detector.observe(_quote("q", OUTAGE_START, 1))

    below = detector.detect(
        now=datetime(2024, 3, 1, 12, 1, 59, 999999, tzinfo=UTC),
        stale_after=STALE_AFTER,
    )
    gaps = detector.detect(now=RESUME, stale_after=STALE_AFTER)

    assert below == ()
    assert [(gap.start, gap.end) for gap in gaps] == [(OUTAGE_START, RESUME)]


def test_detect_skips_intervals_already_marked_dirty() -> None:
    detector = LiveGapDetector(calendar=LakeSessionCalendar(AssetClass.CRYPTO))
    detector.observe(_quote("q", OUTAGE_START, 1))

    first = detector.detect(
        now=datetime(2024, 3, 1, 12, 5, 0, tzinfo=UTC), stale_after=STALE_AFTER
    )
    detector.mark_dirty(first[0])
    repeat = detector.detect(
        now=datetime(2024, 3, 1, 12, 5, 0, tzinfo=UTC), stale_after=STALE_AFTER
    )
    later = detector.detect(
        now=datetime(2024, 3, 1, 12, 7, 0, tzinfo=UTC), stale_after=STALE_AFTER
    )

    assert len(first) == 1
    assert repeat == ()
    assert [(gap.start, gap.end) for gap in later] == [
        (
            datetime(2024, 3, 1, 12, 5, 0, tzinfo=UTC),
            datetime(2024, 3, 1, 12, 7, 0, tzinfo=UTC),
        )
    ]


def test_detect_filters_by_canonical_symbol() -> None:
    detector = LiveGapDetector(calendar=LakeSessionCalendar(AssetClass.CRYPTO))
    detector.observe(_quote("btc-q", OUTAGE_START, 1))
    detector.observe(_quote("eth-q", OUTAGE_START, 2, symbol="ETHUSD"))

    gaps = detector.detect(
        now=datetime(2024, 3, 1, 12, 5, 0, tzinfo=UTC),
        stale_after=STALE_AFTER,
        symbols=("btcusd",),
    )

    assert [gap.symbol for gap in gaps] == ["BTCUSD"]


def test_partitions_are_tracked_independently() -> None:
    request = _dataset_request()
    healer = _healer(_partial_client(request, calls=1), _hole_rows(request))
    healer.feed(_quote("q1", OUTAGE_START, 1))
    healer.feed(_trade("t1", datetime(2024, 3, 1, 12, 4, 0, tzinfo=UTC), 2))

    healed = healer.tick(now=datetime(2024, 3, 1, 12, 5, 0, tzinfo=UTC))

    assert [event.event_id for event in healed] == ["hole-a", "hole-b"]
    assert [action.gap.data_type for action in healer.actions] == [DataType.QUOTE]


def test_gap_outside_expected_coverage_is_refused() -> None:
    client = ScriptedLakeClient()
    session = ExchangeSession(
        DAY,
        datetime(2024, 3, 1, 12, 0, tzinfo=UTC),
        datetime(2024, 3, 1, 12, 30, tzinfo=UTC),
    )
    backfiller = LakeGapBackfiller(
        client=client,
        reader=_reader_for({}),
        calendar=_FixedCalendar((session,)),
        asset_class=AssetClass.CRYPTO,
    )

    with pytest.raises(UncoveredGapError) as excinfo:
        backfiller.backfill(
            _gap(
                datetime(2024, 3, 1, 12, 0, 0, tzinfo=UTC),
                datetime(2024, 3, 1, 13, 0, 0, tzinfo=UTC),
            )
        )

    assert "past the last expected session close" in excinfo.value.reason
    assert client.ensure_calls == []


def test_ensure_response_mismatch_is_rejected() -> None:
    eth_request = _dataset_request(symbol="ETHUSD")
    client = ScriptedLakeClient(
        ensure_results=[
            _coverage_response(
                eth_request, [_entry(eth_request, DAY, status=CoverageState.COMPLETE)]
            )
        ],
    )
    backfiller = LakeGapBackfiller(
        client=client,
        reader=_reader_for({}),
        calendar=LakeSessionCalendar(AssetClass.CRYPTO),
        asset_class=AssetClass.CRYPTO,
    )

    with pytest.raises(MarketLakeError):
        backfiller.backfill(_gap(OUTAGE_START, RESUME))


def test_query_validation_rejects_non_canonical_paths() -> None:
    request = _dataset_request()
    misplaced = FileQueryFile(
        asset_class=request.asset_class,
        data_type=request.data_type,
        symbol="BTCUSD",
        session_date=DAY,
        object_path="not/canonical.parquet",
        url="https://lake.test/not-used",
        sas_url="https://lake.test/read",
        row_count=4,
        byte_size=256,
        checksum=None,
    )
    client = ScriptedLakeClient(
        ensure_results=[
            _coverage_response(
                request, [_entry(request, DAY, status=CoverageState.COMPLETE)]
            )
        ],
        query_results=[
            FileQueryResponse(
                files=(misplaced,),
                results=FileQueryResults(
                    requested_sessions=1,
                    returned_files=1,
                    complete_sessions=1,
                    pending_sessions=0,
                    state=FileQueryState.COMPLETE,
                ),
                limitations=(),
            )
        ],
    )
    backfiller = LakeGapBackfiller(
        client=client,
        reader=_reader_for({}),
        calendar=LakeSessionCalendar(AssetClass.CRYPTO),
        asset_class=AssetClass.CRYPTO,
    )

    with pytest.raises(MarketLakeError) as excinfo:
        backfiller.backfill(_gap(OUTAGE_START, RESUME))

    assert "non-canonical" in str(excinfo.value)


def test_naive_gap_bound_is_rejected() -> None:
    with pytest.raises(ValueError):
        CoverageGap(
            "BTCUSD",
            DataType.QUOTE,
            datetime(2024, 3, 1, 12, 0),
            datetime(2024, 3, 1, 12, 1, tzinfo=UTC),
        )


def test_non_positive_stale_after_is_rejected() -> None:
    request = _dataset_request()
    detector = LiveGapDetector(calendar=LakeSessionCalendar(AssetClass.CRYPTO))
    calendar = LakeSessionCalendar(AssetClass.CRYPTO)
    backfill = LakeGapBackfiller(
        client=ScriptedLakeClient(),
        reader=_reader_for({}),
        calendar=calendar,
        asset_class=AssetClass.CRYPTO,
    )
    healer = _healer(_partial_client(request, calls=1), _outage_rows(request))

    with pytest.raises(ValueError):
        LiveGapHealer(detector=detector, backfill=backfill, stale_after=timedelta(0))
    with pytest.raises(ValueError):
        detector.detect(now=RESUME, stale_after=timedelta(0))
    assert healer.stale_after == STALE_AFTER


def test_option_assets_are_unsupported() -> None:
    with pytest.raises(UnsupportedMarketDataError):
        LakeSessionCalendar(AssetClass.OPTION)
    with pytest.raises(UnsupportedMarketDataError):
        LakeGapBackfiller(
            client=ScriptedLakeClient(),
            reader=_reader_for({}),
            calendar=LakeSessionCalendar(AssetClass.CRYPTO),
            asset_class=AssetClass.OPTION,
        )


def test_components_validate_their_boundaries() -> None:
    with pytest.raises(TypeError):
        LiveGapDetector(calendar=cast("CoverageCalendar", object()))
    with pytest.raises(TypeError):
        LakeGapBackfiller(
            client=cast("LakeClientPort", object()),
            reader=_reader_for({}),
            calendar=LakeSessionCalendar(AssetClass.CRYPTO),
            asset_class=AssetClass.CRYPTO,
        )
    healer = _healer(ScriptedLakeClient(), {})
    with pytest.raises(TypeError):
        healer.feed(cast("MarketEvent", object()))
    with pytest.raises(TypeError):
        healer.tick()
