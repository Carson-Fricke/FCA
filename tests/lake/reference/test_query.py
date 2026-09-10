import io
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from lake.catalog import InMemoryCatalogRepository
from lake.reference.edgar import XbrlFact
from lake.reference.ingest import (
    ClassificationIngest,
    FundamentalsIngest,
    ReferenceIngestCommitter,
)
from lake.reference.nasdaq import NasdaqSecurity, NasdaqSnapshot
from lake.reference.query import (
    classification_snapshot,
    fundamentals_facts,
    latest_fundamentals,
)
from lake.reference.schemas import FUNDAMENTALS_SCHEMA
from lake.storage.parquet import LocalFilesystemStore


def _security(symbol: str, test_issue: bool = False) -> NasdaqSecurity:
    return NasdaqSecurity(
        symbol=symbol,
        security_name=f"{symbol} Common Stock",
        listing_exchange="Q",
        market_category="Q",
        etf=False,
        round_lot_size=Decimal("100"),
        test_issue=test_issue,
        financial_status=None if test_issue else "Q",
    )


def _snapshot(snapshot_date: date, symbols: tuple[str, ...]) -> NasdaqSnapshot:
    eastern = ZoneInfo("America/New_York")
    return NasdaqSnapshot(
        file_creation_time=datetime(
            snapshot_date.year,
            snapshot_date.month,
            snapshot_date.day,
            17,
            20,
            tzinfo=eastern,
        ),
        snapshot_date=snapshot_date,
        securities=tuple(
            _security(symbol, test_issue=symbol.startswith("Z")) for symbol in symbols
        ),
    )


def _fact(
    *,
    filed: date,
    val: str,
    tag: str = "Revenues",
    cik: int = 320193,
    accn: str = "0000320193-24-000006",
) -> XbrlFact:
    return XbrlFact(
        cik=cik,
        taxonomy="us-gaap",
        tag=tag,
        unit="USD",
        period_end=date(2023, 12, 30),
        fy=2024,
        fp="Q1",
        form="10-Q",
        accn=accn,
        filed=filed,
        frame=None,
        val=Decimal(val),
    )


def _seed(root: Path) -> None:
    store = LocalFilesystemStore(root)
    classification = ClassificationIngest(
        committer=ReferenceIngestCommitter(
            object_store=store, catalog=InMemoryCatalogRepository()
        )
    )
    classification.run(_snapshot(date(2024, 4, 18), ("AAPL", "MSFT")))
    classification.run(_snapshot(date(2024, 4, 19), ("AAPL", "MSFT", "ZVZZT")))
    fundamentals = FundamentalsIngest(
        committer=ReferenceIngestCommitter(
            object_store=store, catalog=InMemoryCatalogRepository()
        )
    )
    fundamentals.run(
        (
            _fact(filed=date(2024, 2, 2), val="1"),
            _fact(filed=date(2024, 5, 3), val="2", accn="0000320193-24-000008"),
            _fact(
                filed=date(2023, 11, 3),
                val="3",
                tag="Assets",
                accn="0000320193-23-000106",
            ),
        )
    )


def test_classification_serves_only_the_latest_known_snapshot(tmp_path: Path) -> None:
    _seed(tmp_path)

    latest = classification_snapshot(tmp_path, date(2024, 4, 19))
    assert [row["symbol"] for row in latest] == ["AAPL", "MSFT", "ZVZZT"]

    previous = classification_snapshot(tmp_path, date(2024, 4, 18))
    assert [row["symbol"] for row in previous] == ["AAPL", "MSFT"]

    before_any = classification_snapshot(tmp_path, date(2024, 4, 17))
    assert before_any == ()


def test_classification_snapshot_rows_carry_exact_point_in_time_values(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)

    latest = classification_snapshot(tmp_path, date(2024, 4, 19))

    assert latest[0]["security_name"] == "AAPL Common Stock"
    assert latest[0]["file_creation_time"] == datetime(2024, 4, 19, 21, 20, tzinfo=UTC)
    assert latest[2]["test_issue"] is True


def test_fundamentals_facts_exclude_facts_filed_after_the_as_of_instant(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)

    visible = fundamentals_facts(tmp_path, date(2024, 2, 2))
    assert [(row["tag"], row["val"]) for row in visible] == [
        ("Assets", Decimal("3")),
        ("Revenues", Decimal("1")),
    ]

    before_any = fundamentals_facts(tmp_path, date(2024, 2, 1))
    assert [row["tag"] for row in before_any] == ["Assets"]

    after_restatement = fundamentals_facts(tmp_path, date(2024, 5, 3))
    assert [(row["tag"], row["val"]) for row in after_restatement] == [
        ("Assets", Decimal("3")),
        ("Revenues", Decimal("1")),
        ("Revenues", Decimal("2")),
    ]


def test_latest_fundamentals_resolves_restated_values_only_after_filing(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)

    before = latest_fundamentals(tmp_path, date(2024, 5, 2), tag="Revenues")
    assert len(before) == 1
    assert before[0]["val"] == Decimal("1")
    assert before[0]["accn"] == "0000320193-24-000006"

    on_filing = latest_fundamentals(tmp_path, date(2024, 5, 3), tag="Revenues")
    assert len(on_filing) == 1
    assert on_filing[0]["val"] == Decimal("2")
    assert on_filing[0]["accn"] == "0000320193-24-000008"


def test_partition_pruning_never_reads_later_knowledge_dates(tmp_path: Path) -> None:
    _seed(tmp_path)
    future = (
        tmp_path
        / "date=2025-01-01"
        / "asset_class=reference"
        / "data_type=fundamentals"
        / "CIK0000320193.parquet"
    )
    future.parent.mkdir(parents=True)
    future.write_bytes(b"corrupt-but-never-read")

    assert fundamentals_facts(tmp_path, date(2024, 5, 3), cik=320193)


def test_row_level_filed_predicate_defends_mixed_knowledge_objects(
    tmp_path: Path,
) -> None:
    _seed(tmp_path)
    buffer = io.BytesIO()
    pq.write_table(
        pa.Table.from_pylist(
            [
                {
                    "cik": 789019,
                    "taxonomy": "us-gaap",
                    "tag": "Revenues",
                    "unit": "USD",
                    "period_end": date(2023, 12, 30),
                    "fy": 2024,
                    "fp": "Q1",
                    "form": "10-Q",
                    "accn": "0000789019-24-000004",
                    "filed": date(2024, 2, 2),
                    "frame": None,
                    "val": Decimal("1"),
                },
                {
                    "cik": 789019,
                    "taxonomy": "us-gaap",
                    "tag": "Revenues",
                    "unit": "USD",
                    "period_end": date(2023, 12, 30),
                    "fy": 2024,
                    "fp": "Q1",
                    "form": "10-Q",
                    "accn": "0000789019-24-000009",
                    "filed": date(2024, 5, 3),
                    "frame": None,
                    "val": Decimal("9"),
                },
            ],
            schema=FUNDAMENTALS_SCHEMA,
        ),
        buffer,
    )
    store = LocalFilesystemStore(tmp_path)
    store.replace(
        "date=2024-02-02/asset_class=reference/data_type=fundamentals/"
        "CIK0000789019.parquet",
        buffer.getvalue(),
    )

    visible = fundamentals_facts(tmp_path, date(2024, 2, 2), cik=789019)
    assert [row["val"] for row in visible] == [Decimal("1")]

    later = fundamentals_facts(tmp_path, date(2024, 5, 3), cik=789019)
    assert [row["val"] for row in later] == [Decimal("1"), Decimal("9")]


def test_fact_filters_narrow_by_cik_taxonomy_tag_and_unit(tmp_path: Path) -> None:
    _seed(tmp_path)
    as_of = date(2024, 5, 3)

    assert {
        row["tag"] for row in fundamentals_facts(tmp_path, as_of, tag="Assets")
    } == {"Assets"}
    assert {row["cik"] for row in fundamentals_facts(tmp_path, as_of, cik=320193)} == {
        320193
    }
    assert fundamentals_facts(tmp_path, as_of, taxonomy="dei") == ()
    assert fundamentals_facts(tmp_path, as_of, unit="shares") == ()


def test_unknown_roots_and_dates_follow_the_contract(tmp_path: Path) -> None:
    _seed(tmp_path)

    assert fundamentals_facts(tmp_path / "missing", date(2024, 5, 3)) == ()
    assert classification_snapshot(tmp_path / "missing", date(2024, 4, 19)) == ()

    with pytest.raises(TypeError, match="as_of must be a date, not a datetime"):
        fundamentals_facts(tmp_path, datetime(2024, 5, 3, tzinfo=UTC))
    with pytest.raises(TypeError, match="as_of must be a date, not a datetime"):
        classification_snapshot(tmp_path, datetime(2024, 4, 19, tzinfo=UTC))
    with pytest.raises(TypeError, match="as_of must be a date"):
        fundamentals_facts(tmp_path, cast("date", "2024-05-03"))


def test_objects_with_unexpected_schemas_are_rejected(tmp_path: Path) -> None:
    _seed(tmp_path)
    foreign_path = (
        tmp_path
        / "date=2023-11-03"
        / "asset_class=reference"
        / "data_type=fundamentals"
        / "CIK0009999999.parquet"
    )
    foreign_path.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.BytesIO()
    pq.write_table(pa.table({"x": [1]}), buffer)
    foreign_path.write_bytes(buffer.getvalue())

    with pytest.raises(ValueError, match="unexpected schema"):
        fundamentals_facts(tmp_path, date(2024, 5, 3), cik=789019)
