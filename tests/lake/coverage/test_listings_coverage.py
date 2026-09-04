"""Focused tests for listings-aware coverage classification."""

from datetime import UTC, date, datetime

import pytest

from lake.catalog import (
    CoverageRecord,
    CoverageStatus,
    DelistingType,
    InMemoryCatalogRepository,
    InMemoryListingRepository,
    ListingRecord,
    ListingType,
)
from lake.contracts import AssetClass, DataType
from lake.coverage import CoverageReport, CoverageState, calculate_coverage, plan_gaps

RENAME_DAY = date(2022, 6, 9)


def _episode(
    symbol: str,
    *,
    listed: date,
    delisted: date | None = None,
    entity_id: str = "META",
    listing_type: ListingType = ListingType.IPO,
    delisting_type: DelistingType | None = None,
) -> ListingRecord:
    return ListingRecord(
        entity_id=entity_id,
        symbol=symbol,
        listed_date=listed,
        delisted_date=delisted,
        listing_type=listing_type,
        delisting_type=delisting_type,
    )


FB = _episode(
    "FB",
    listed=date(2012, 5, 18),
    delisted=date(2022, 6, 8),
    delisting_type=DelistingType.RENAME,
)
META = _episode(
    "META",
    listed=RENAME_DAY,
    listing_type=ListingType.RENAME,
)
LATER = _episode(
    "LATE",
    listed=date(2022, 6, 13),
    entity_id="LATE",
)


def _listings(*records: ListingRecord) -> InMemoryListingRepository:
    return InMemoryListingRepository(records)


def _report(
    symbols: str | tuple[str, ...],
    *,
    start: date = date(2022, 6, 1),
    end: date = date(2022, 6, 15),
    as_of: datetime = datetime(2022, 6, 15, 21, tzinfo=UTC),
    listings: InMemoryListingRepository | None = None,
    catalog: InMemoryCatalogRepository | None = None,
) -> CoverageReport:
    return calculate_coverage(
        catalog or InMemoryCatalogRepository(),
        asset_class=AssetClass.EQUITY,
        symbols=symbols,
        data_types=DataType.TRADE,
        start=start,
        end=end,
        as_of=as_of,
        listings=listings,
    )


def _states(symbol: str, report: CoverageReport) -> dict[date, CoverageState]:
    return {
        entry.session_date: entry.state for entry in report if entry.symbol == symbol
    }


def test_post_delisting_sessions_report_delisted_and_pre_delisting_stay_ensureable() -> (
    None
):
    report = _report("FB", listings=_listings(FB, META))

    states = _states("FB", report)

    assert states[date(2022, 6, 6)] is CoverageState.MISSING
    assert states[date(2022, 6, 7)] is CoverageState.MISSING
    assert states[date(2022, 6, 8)] is CoverageState.MISSING
    assert states[RENAME_DAY] is CoverageState.DELISTED
    assert states[date(2022, 6, 10)] is CoverageState.DELISTED
    assert states[date(2022, 6, 15)] is CoverageState.DELISTED

    gaps = report.gap_plan()
    assert all(entry.symbol == "FB" for entry in gaps)
    assert [entry.session_date for entry in gaps] == [
        date(2022, 6, 1),
        date(2022, 6, 2),
        date(2022, 6, 3),
        date(2022, 6, 6),
        date(2022, 6, 7),
        date(2022, 6, 8),
    ]


def test_rename_chain_produces_no_duplicate_actionable_coverage() -> None:
    fb = _report("FB", listings=_listings(FB, META))
    meta = _report("META", listings=_listings(FB, META))

    fb_actionable = {entry.session_date for entry in fb if entry.is_actionable}
    meta_actionable = {entry.session_date for entry in meta if entry.is_actionable}

    assert fb_actionable == {
        date(2022, 6, 1),
        date(2022, 6, 2),
        date(2022, 6, 3),
        date(2022, 6, 6),
        date(2022, 6, 7),
        date(2022, 6, 8),
    }
    assert meta_actionable == {
        date(2022, 6, 9),
        date(2022, 6, 10),
        date(2022, 6, 13),
        date(2022, 6, 14),
        date(2022, 6, 15),
    }
    assert not fb_actionable & meta_actionable
    assert fb_actionable | meta_actionable == set(entry.session_date for entry in fb)


def test_pre_listing_sessions_are_delisted_once_the_listing_is_known() -> None:
    report = _report("LATE", listings=_listings(LATER))

    states = _states("LATE", report)

    assert states[date(2022, 6, 10)] is CoverageState.DELISTED
    assert states[date(2022, 6, 13)] is CoverageState.MISSING
    assert states[date(2022, 6, 15)] is CoverageState.MISSING


def test_future_dated_delisting_is_never_exposed_to_an_earlier_as_of() -> None:
    pending = _episode(
        "PEND",
        listed=date(2018, 1, 2),
        delisted=date(2022, 12, 30),
        entity_id="PEND",
        delisting_type=DelistingType.VOLUNTARY,
    )
    listings = _listings(pending)

    before = _states(
        "PEND",
        _report(
            "PEND",
            start=date(2022, 6, 1),
            end=date(2022, 6, 15),
            listings=listings,
        ),
    )
    after = _states(
        "PEND",
        _report(
            "PEND",
            start=date(2022, 12, 1),
            end=date(2023, 1, 5),
            as_of=datetime(2023, 1, 5, 21, tzinfo=UTC),
            listings=listings,
        ),
    )

    assert all(state is CoverageState.MISSING for state in before.values())
    assert all(
        state is CoverageState.MISSING
        for day, state in after.items()
        if day <= date(2022, 12, 30)
    )
    assert all(
        state is CoverageState.DELISTED
        for day, state in after.items()
        if day > date(2022, 12, 30)
    )


def test_without_listings_every_uncovered_session_stays_missing() -> None:
    report = _report("FB", listings=None)

    assert all(entry.state is CoverageState.MISSING for entry in report)
    assert not report.is_satisfied


def test_delisted_sessions_are_terminal_and_keep_persisted_rows() -> None:
    catalog = InMemoryCatalogRepository(
        [
            CoverageRecord(
                asset_class=AssetClass.EQUITY,
                data_type=DataType.TRADE,
                symbol="FB",
                session_date=date(2022, 6, 10),
                status=CoverageStatus.COMPLETE,
            )
        ]
    )
    report = calculate_coverage(
        catalog,
        asset_class=AssetClass.EQUITY,
        symbols="FB",
        data_types=DataType.TRADE,
        start=date(2022, 6, 9),
        end=date(2022, 6, 13),
        as_of=datetime(2022, 6, 15, 21, tzinfo=UTC),
        listings=_listings(FB, META),
    )

    by_date = {entry.session_date: entry for entry in report}

    assert by_date[date(2022, 6, 10)].state is CoverageState.COMPLETE
    assert by_date[RENAME_DAY].state is CoverageState.DELISTED
    assert by_date[date(2022, 6, 13)].state is CoverageState.DELISTED
    assert report.is_satisfied
    assert report.gap_plan().gaps == ()
    assert [entry.session_date for entry in report.delisted] == [
        RENAME_DAY,
        date(2022, 6, 13),
    ]


def test_delisted_state_is_distinct_and_terminal() -> None:
    assert CoverageState.DELISTED.value == "delisted"


def test_plan_gaps_honors_listing_knowledge() -> None:
    gaps = plan_gaps(
        InMemoryCatalogRepository(),
        asset_class=AssetClass.EQUITY,
        symbols="FB",
        data_types=DataType.TRADE,
        start=date(2022, 6, 9),
        end=date(2022, 6, 15),
        as_of=datetime(2022, 6, 15, 21, tzinfo=UTC),
        listings=_listings(FB, META),
    )

    assert gaps.gaps == ()


def test_as_of_before_the_listing_keeps_every_session_ensureable() -> None:
    report = _report(
        "META",
        as_of=datetime(2020, 1, 2, 21, tzinfo=UTC),
        listings=_listings(FB, META),
    )

    assert all(entry.state is CoverageState.MISSING for entry in report)


@pytest.mark.parametrize(
    ("as_of", "expected"),
    [
        (datetime(2022, 6, 7, 21, tzinfo=UTC), CoverageState.MISSING),
        (datetime(2022, 6, 8, 21, tzinfo=UTC), CoverageState.DELISTED),
    ],
)
def test_delisted_classification_arrives_with_the_delisting_instant(
    as_of: datetime, expected: CoverageState
) -> None:
    report = _report(
        "FB",
        start=date(2022, 6, 15),
        end=date(2022, 6, 15),
        as_of=as_of,
        listings=_listings(FB),
    )

    assert _states("FB", report)[date(2022, 6, 15)] is expected
