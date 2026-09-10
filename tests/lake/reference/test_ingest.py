import asyncio
import io
import json
import zipfile
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import pyarrow as pa
import pyarrow.parquet as pq

from lake.catalog import InMemoryCatalogRepository
from lake.catalog.models import CoverageRecord, CoverageStatus
from lake.reference.configuration import (
    ReferenceHttpSettings,
    ReferenceRetrySettings,
    ReferenceSettings,
)
from lake.reference.edgar import XbrlFact, parse_companyfacts
from lake.reference.ingest import (
    ClassificationIngest,
    FundamentalsIngest,
    ReferenceIngestCommitter,
    filter_facts,
    ingest_classification,
    ingest_fundamentals_increment,
    ingest_fundamentals_zip,
)
from lake.reference.nasdaq import NasdaqSecurity, NasdaqSnapshot
from lake.reference.partitions import cik_object_name

FIXTURES = Path(__file__).parent / "fixtures"
NASDAQ_TRADED = FIXTURES / "nasdaqtraded.txt"
COMPANY_FACTS = FIXTURES / "companyfacts.json"

_SNAPSHOT_PATH = (
    "date=2024-04-19/asset_class=reference/data_type=classification/SNAPSHOT.parquet"
)


class FakeStore:
    """In-memory ObjectStore double recording the commit order."""

    def __init__(self, events: list[str]) -> None:
        self._events = events
        self.objects: dict[str, bytes] = {}

    def replace(self, relative_path: str, payload: bytes) -> None:
        self._events.append(f"replace:{relative_path}")
        self.objects[relative_path] = payload

    def read(self, relative_path: str) -> bytes:
        try:
            return self.objects[relative_path]
        except KeyError as error:
            raise FileNotFoundError(relative_path) from error


class FakeCatalog:
    """Coverage sink delegating to the real in-memory upsert semantics."""

    def __init__(
        self, repository: InMemoryCatalogRepository, events: list[str]
    ) -> None:
        self.repository = repository
        self._events = events
        self.records: list[CoverageRecord] = []

    def upsert(self, record: CoverageRecord) -> CoverageRecord:
        self._events.append(f"coverage:{record.symbol}")
        self.records.append(record)
        return self.repository.upsert(record)


def _settings(**overrides: str) -> ReferenceSettings:
    values: dict[str, Any] = {
        "schema_version": 1,
        "user_agent": "Test Company admin@test.example",
        "classification_url": "https://example.test/nasdaqtraded.txt",
        "company_facts_url_template": (
            "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:0>10}.json"
        ),
        "company_concept_url_template": (
            "https://data.sec.gov/api/xbrl/companyconcept/"
            "CIK{cik:0>10}/{taxonomy}/{tag}.json"
        ),
        "bulk_company_facts_url": "https://www.sec.gov/Archives/edgar/companyfacts.zip",
        "http": ReferenceHttpSettings(
            requests_per_second=10,
            timeout_seconds=30,
            retry=ReferenceRetrySettings(
                max_attempts=5,
                honor_retry_after=True,
                backoff_seconds=0.5,
                max_backoff_seconds=30.0,
            ),
        ),
    }
    values.update(overrides)
    return ReferenceSettings(**values)


def _snapshot(
    snapshot_date: date = date(2024, 4, 19),
) -> NasdaqSnapshot:
    return NasdaqSnapshot(
        file_creation_time=datetime(
            2024, 4, 19, 17, 20, tzinfo=ZoneInfo("America/New_York")
        ),
        snapshot_date=snapshot_date,
        securities=(
            NasdaqSecurity(
                symbol="AAPL",
                security_name="Apple Inc. Common Stock",
                listing_exchange="Q",
                market_category="Q",
                etf=False,
                round_lot_size=Decimal("100"),
                test_issue=False,
                financial_status="Q",
            ),
            NasdaqSecurity(
                symbol="ZVZZT",
                security_name="ZVZZT Widget Test Security",
                listing_exchange="Z",
                market_category="Z",
                etf=False,
                round_lot_size=Decimal("100"),
                test_issue=True,
                financial_status=None,
            ),
        ),
    )


def _facts() -> tuple[XbrlFact, ...]:
    return parse_companyfacts(COMPANY_FACTS.read_bytes())


class FakeHTTPClient:
    """Offline stand-in for the fair-access client."""

    def __init__(
        self,
        *,
        bytes_payloads: Mapping[str, bytes] | None = None,
        json_payloads: Mapping[str, bytes] | None = None,
    ) -> None:
        self.bytes_payloads = dict(bytes_payloads or {})
        self.json_payloads = dict(json_payloads or {})
        self.urls: list[str] = []

    async def get_bytes(self, url: str) -> bytes:
        self.urls.append(url)
        return self.bytes_payloads[url]

    async def get_json(self, url: str) -> object:
        self.urls.append(url)
        return json.loads(self.json_payloads[url].decode("utf-8"))


def _committer(store: FakeStore, catalog: FakeCatalog) -> ReferenceIngestCommitter:
    return ReferenceIngestCommitter(object_store=store, catalog=catalog)


def test_classification_ingest_publishes_snapshot_and_coverage() -> None:
    events: list[str] = []
    store = FakeStore(events)
    catalog = FakeCatalog(InMemoryCatalogRepository(), events)
    path = (
        "date=2024-04-19/asset_class=reference/data_type=classification/"
        "SNAPSHOT.parquet"
    )

    result = ClassificationIngest(committer=_committer(store, catalog)).run(_snapshot())

    assert result.snapshot_date == date(2024, 4, 19)
    assert result.objects[0].path == path
    assert result.objects[0].rows == 2
    assert result.coverage[0].asset_class.value == "reference"
    assert result.coverage[0].data_type.value == "classification"
    assert result.coverage[0].symbol == "DIRECTORY"
    assert result.coverage[0].status is CoverageStatus.COMPLETE
    assert result.coverage[0].row_count == 2
    assert result.coverage[0].checksum == result.objects[0].checksum

    table = pq.read_table(io.BytesIO(store.objects[path]))
    assert table.column("symbol").to_pylist() == ["AAPL", "ZVZZT"]
    assert table.column("test_issue").to_pylist() == [False, True]
    assert events == [f"replace:{path}", "coverage:DIRECTORY"]


def test_classification_ingest_is_idempotent() -> None:
    events: list[str] = []
    store = FakeStore(events)
    catalog = FakeCatalog(InMemoryCatalogRepository(), events)
    ingest = ClassificationIngest(committer=_committer(store, catalog))

    first = ingest.run(_snapshot())
    first_bytes = dict(store.objects)
    first_catalog = catalog.repository.list()

    second = ingest.run(_snapshot())
    second_bytes = dict(store.objects)
    second_catalog = catalog.repository.list()

    assert first.coverage == second.coverage
    assert first.objects == second.objects
    assert first_bytes == second_bytes
    assert first_catalog == second_catalog
    assert [record.row_count for record in second_catalog] == [2]


def test_classification_ingest_with_no_securities_reports_empty() -> None:
    events: list[str] = []
    store = FakeStore(events)
    catalog = FakeCatalog(InMemoryCatalogRepository(), events)
    snapshot = NasdaqSnapshot(
        file_creation_time=datetime(
            2024, 4, 19, 17, 20, tzinfo=ZoneInfo("America/New_York")
        ),
        snapshot_date=date(2024, 4, 19),
        securities=(),
    )

    result = ClassificationIngest(committer=_committer(store, catalog)).run(snapshot)

    assert result.coverage[0].status is CoverageStatus.EMPTY
    assert result.coverage[0].row_count == 0


def test_fundamentals_ingest_publishes_per_cik_per_filed_objects() -> None:
    events: list[str] = []
    store = FakeStore(events)
    catalog = FakeCatalog(InMemoryCatalogRepository(), events)

    result = FundamentalsIngest(committer=_committer(store, catalog)).run(_facts())

    assert result.ciks == (320193,)
    paths = [item.path for item in result.objects]
    assert paths == [
        "date=2023-11-03/asset_class=reference/data_type=fundamentals/"
        + cik_object_name(320193)
        + ".parquet",
        "date=2024-02-02/asset_class=reference/data_type=fundamentals/"
        + cik_object_name(320193)
        + ".parquet",
        "date=2024-05-03/asset_class=reference/data_type=fundamentals/"
        + cik_object_name(320193)
        + ".parquet",
    ]
    assert [
        (record.symbol, record.session_date.isoformat()) for record in result.coverage
    ] == [
        ("0000320193", "2023-11-03"),
        ("0000320193", "2024-02-02"),
        ("0000320193", "2024-05-03"),
    ]
    rows = pq.read_table(io.BytesIO(store.objects[paths[0]])).to_pylist()
    assert {(row["taxonomy"], row["tag"]) for row in rows} == {
        ("dei", "EntityCommonStockSharesOutstanding"),
        ("us-gaap", "Assets"),
    }


def test_fundamentals_ingest_is_idempotent() -> None:
    events: list[str] = []
    store = FakeStore(events)
    catalog = FakeCatalog(InMemoryCatalogRepository(), events)
    ingest = FundamentalsIngest(committer=_committer(store, catalog))

    first = ingest.run(_facts())
    first_bytes = dict(store.objects)
    first_catalog = catalog.repository.list()

    second = ingest.run(_facts())
    second_bytes = dict(store.objects)
    second_catalog = catalog.repository.list()

    assert first.coverage == second.coverage
    assert first.objects == second.objects
    assert first_bytes == second_bytes
    assert first_catalog == second_catalog


def test_fundamentals_ingest_merges_other_tags_into_the_same_knowledge_object() -> None:
    events: list[str] = []
    store = FakeStore(events)
    catalog = FakeCatalog(InMemoryCatalogRepository(), events)
    ingest = FundamentalsIngest(committer=_committer(store, catalog))
    path = (
        "date=2024-02-02/asset_class=reference/data_type=fundamentals/"
        "CIK0000320193.parquet"
    )
    revenues = [fact for fact in _facts() if fact.filed == date(2024, 2, 2)]
    assets = [
        XbrlFact(
            cik=fact.cik,
            taxonomy=fact.taxonomy,
            tag="Assets",
            unit=fact.unit,
            period_end=fact.period_end,
            fy=fact.fy,
            fp=fact.fp,
            form=fact.form,
            accn=fact.accn,
            filed=fact.filed,
            frame=fact.frame,
            val=fact.val,
        )
        for fact in revenues
    ]

    ingest.run(revenues)
    ingest.run(assets)
    rows = pq.read_table(io.BytesIO(store.objects[path])).to_pylist()
    assert {(row["taxonomy"], row["tag"]) for row in rows} == {
        ("us-gaap", "Revenues"),
        ("us-gaap", "Assets"),
    }

    ingest.run(revenues)
    rows = pq.read_table(io.BytesIO(store.objects[path])).to_pylist()
    assert len(rows) == 2


def test_ingest_fundamentals_zip_backfills_every_cik() -> None:
    buffer = io.BytesIO()
    document = json.loads(COMPANY_FACTS.read_text(encoding="utf-8"))
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("companyfacts/CIK0000320193.json", json.dumps(document))
        archive.writestr("company_tickers.json", "{}")

    result = ingest_fundamentals_zip(
        zip_payload=buffer.getvalue(),
        object_store=FakeStore([]),
        catalog=FakeCatalog(InMemoryCatalogRepository(), []),
    )

    assert result["ciks"] == [320193]
    coverage = cast("list[dict[str, object]]", result["coverage"])
    assert [entry["rows"] for entry in coverage] == [2, 1, 1]


def test_filter_facts_narrows_by_cik_and_tag() -> None:
    facts = _facts()
    other = tuple(
        XbrlFact(
            cik=789019,
            taxonomy=fact.taxonomy,
            tag=fact.tag,
            unit=fact.unit,
            period_end=fact.period_end,
            fy=fact.fy,
            fp=fact.fp,
            form=fact.form,
            accn=fact.accn,
            filed=fact.filed,
            frame=fact.frame,
            val=fact.val,
        )
        for fact in facts
    )
    combined = facts + other

    assert filter_facts(combined) == tuple(combined)
    assert filter_facts(combined, ciks=(320193,)) == facts
    assert filter_facts(combined, tags=("Revenues",)) == tuple(
        fact for fact in combined if fact.tag == "Revenues"
    )
    assert filter_facts(combined, ciks=(789019,), tags=("Assets",)) == (
        tuple(fact for fact in other if fact.tag == "Assets")
    )


def test_ingest_classification_uses_the_client_without_network() -> None:
    client = FakeHTTPClient(
        bytes_payloads={
            _settings().classification_url: NASDAQ_TRADED.read_bytes(),
        }
    )
    store = FakeStore([])
    catalog = FakeCatalog(InMemoryCatalogRepository(), [])

    summary = asyncio.run(
        ingest_classification(
            client=client,
            settings=_settings(),
            object_store=store,
            catalog=catalog,
        )
    )

    assert client.urls == [_settings().classification_url]
    assert summary["snapshot_date"] == "2024-04-19"
    assert summary["file_creation_time"] == "2024-04-19T17:20:00-04:00"
    assert summary["coverage"] == [
        {
            "rows": 4,
            "session_date": "2024-04-19",
            "status": "complete",
            "symbol": "DIRECTORY",
        }
    ]


def test_ingest_fundamentals_increment_fetches_and_merges_without_network() -> None:
    client = FakeHTTPClient(
        json_payloads={
            _settings().company_facts_url_template.format(cik=320193): (
                COMPANY_FACTS.read_bytes()
            )
        }
    )
    store = FakeStore([])
    catalog = FakeCatalog(InMemoryCatalogRepository(), [])

    summary = asyncio.run(
        ingest_fundamentals_increment(
            client=client,
            settings=_settings(),
            ciks=(320193,),
            object_store=store,
            catalog=catalog,
        )
    )

    assert client.urls == [
        "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json"
    ]
    assert summary["ciks"] == [320193]
    coverage = cast("list[dict[str, object]]", summary["coverage"])
    assert [entry["session_date"] for entry in coverage] == [
        "2023-11-03",
        "2024-02-02",
        "2024-05-03",
    ]


def test_ingest_fundamentals_increment_tag_filter_limits_stored_rows() -> None:
    client = FakeHTTPClient(
        json_payloads={
            _settings().company_facts_url_template.format(cik=320193): (
                COMPANY_FACTS.read_bytes()
            )
        }
    )
    store = FakeStore([])
    catalog = FakeCatalog(InMemoryCatalogRepository(), [])

    summary = asyncio.run(
        ingest_fundamentals_increment(
            client=client,
            settings=_settings(),
            ciks=(320193,),
            tags=("Revenues",),
            object_store=store,
            catalog=catalog,
        )
    )

    coverage = cast("list[dict[str, object]]", summary["coverage"])
    assert [entry["rows"] for entry in coverage] == [1, 1]
    stored = pq.read_table(
        io.BytesIO(
            store.objects[
                "date=2024-02-02/asset_class=reference/data_type=fundamentals/"
                "CIK0000320193.parquet"
            ]
        )
    )
    assert stored.column("tag").to_pylist() == ["Revenues"]


def test_committer_replaces_objects_before_replacing_coverage() -> None:
    events: list[str] = []
    store = FakeStore(events)
    catalog = FakeCatalog(InMemoryCatalogRepository(), events)

    FundamentalsIngest(committer=_committer(store, catalog)).run(_facts())

    assert events[0].startswith("replace:")
    assert events[-1].startswith("coverage:")


def test_existing_object_with_unexpected_schema_is_rejected() -> None:
    events: list[str] = []
    store = FakeStore(events)
    catalog = FakeCatalog(InMemoryCatalogRepository(), [])
    path = (
        "date=2024-02-02/asset_class=reference/data_type=fundamentals/"
        "CIK0000320193.parquet"
    )
    foreign = io.BytesIO()
    pq.write_table(pa.table({"x": [1]}), foreign)
    store.objects[path] = foreign.getvalue()

    try:
        FundamentalsIngest(committer=_committer(store, catalog)).run(_facts())
    except Exception as error:
        assert "unexpected schema" in str(error)
    else:
        raise AssertionError("foreign object must be rejected")


def test_facts_sequence_is_sorted_and_deduplicated_before_staging() -> None:
    events: list[str] = []
    store = FakeStore(events)
    catalog = FakeCatalog(InMemoryCatalogRepository(), [])
    facts = _facts() + _facts()

    result = FundamentalsIngest(committer=_committer(store, catalog)).run(
        facts + tuple(reversed(facts))
    )

    assert result.objects[0].rows == 2
