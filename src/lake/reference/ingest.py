"""Idempotent point-in-time reference dataset ingestion.

Classification snapshots replace their whole partition object: each daily
directory is one complete, deterministic archive row set. Fundamentals merge
into existing per-CIK, per-``filed`` objects so incremental
``companyconcept`` fetches never drop facts from other tags already stored
for the same knowledge date. Both paths publish staged objects and coverage
rows through one commit boundary, mirroring the equity ingest transaction.
"""

from __future__ import annotations

import hashlib
import io
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Final, Protocol, cast

import pyarrow as pa
import pyarrow.parquet as pq

from lake.catalog.models import CoverageRecord, CoverageStatus
from lake.contracts import AssetClass, DataType
from lake.reference.configuration import ReferenceSettings
from lake.reference.edgar import (
    XbrlFact,
    company_facts_url,
    parse_companyfacts,
    read_companyfacts_zip,
)
from lake.reference.http import ReferenceFetchClient
from lake.reference.nasdaq import NasdaqSnapshot, parse_nasdaq_traded
from lake.reference.partitions import (
    CLASSIFICATION_OBJECT_NAME,
    cik_object_name,
    cik_symbol,
    reference_partition_path,
)
from lake.reference.schemas import (
    FUNDAMENTALS_SCHEMA,
    classification_table,
    fundamentals_table,
)
from lake.storage.parquet import ObjectStore, ParquetMetadata

CLASSIFICATION_COVERAGE_SYMBOL: Final = "DIRECTORY"

_ROW_FIELDS: Final = tuple(field.name for field in FUNDAMENTALS_SCHEMA)


class ReferenceCoverageSink(Protocol):
    """Catalog surface needed to persist reference coverage rows."""

    def upsert(self, record: CoverageRecord) -> CoverageRecord: ...


class ReferenceObjectStore(ObjectStore, Protocol):
    """Store surface that can also read previously committed objects."""

    def read(self, relative_path: str) -> bytes:
        """Return object bytes or raise ``FileNotFoundError`` when absent."""


@dataclass(frozen=True, slots=True)
class StagedReferenceObject:
    """A complete Parquet payload waiting for an atomic reference commit."""

    metadata: ParquetMetadata
    payload: bytes


@dataclass(frozen=True, slots=True)
class ClassificationIngestResult:
    """Published metadata for one symbol directory snapshot."""

    snapshot_date: date
    file_creation_time: datetime
    coverage: tuple[CoverageRecord, ...]
    objects: tuple[ParquetMetadata, ...]


@dataclass(frozen=True, slots=True)
class FundamentalsIngestResult:
    """Published metadata for one fundamentals ingest batch."""

    ciks: tuple[int, ...]
    coverage: tuple[CoverageRecord, ...]
    objects: tuple[ParquetMetadata, ...]


class ReferenceIngestCommitter:
    """Publish staged objects before atomically replacing coverage rows."""

    def __init__(
        self,
        *,
        object_store: ReferenceObjectStore,
        catalog: ReferenceCoverageSink,
    ) -> None:
        self.object_store = object_store
        self._catalog = catalog

    def commit(
        self,
        *,
        objects: Sequence[StagedReferenceObject],
        coverage: Sequence[CoverageRecord],
    ) -> None:
        """Replace objects, then replace their logical coverage keys."""
        for staged in objects:
            self.object_store.replace(staged.metadata.path, staged.payload)
        for record in coverage:
            self._catalog.upsert(record)


class ClassificationIngest:
    """Stage and publish one complete symbol directory snapshot."""

    def __init__(self, *, committer: ReferenceIngestCommitter) -> None:
        self._committer = committer

    def run(self, snapshot: NasdaqSnapshot) -> ClassificationIngestResult:
        """Normalize, stage, and commit the snapshot for its knowledge date."""
        records = [
            {
                "file_creation_time": snapshot.file_creation_time,
                "symbol": security.symbol,
                "security_name": security.security_name,
                "listing_exchange": security.listing_exchange,
                "market_category": security.market_category,
                "etf": security.etf,
                "round_lot_size": security.round_lot_size,
                "test_issue": security.test_issue,
                "financial_status": security.financial_status,
            }
            for security in snapshot.securities
        ]
        table = classification_table(records)
        path = reference_partition_path(
            knowledge_date=snapshot.snapshot_date,
            data_type=DataType.CLASSIFICATION,
            name=CLASSIFICATION_OBJECT_NAME,
        )
        staged = _stage(table, path)
        coverage = (
            CoverageRecord(
                asset_class=AssetClass.REFERENCE,
                data_type=DataType.CLASSIFICATION,
                symbol=CLASSIFICATION_COVERAGE_SYMBOL,
                session_date=snapshot.snapshot_date,
                status=(
                    CoverageStatus.COMPLETE if table.num_rows else CoverageStatus.EMPTY
                ),
                object_path=staged.metadata.path,
                row_count=staged.metadata.rows,
                byte_size=staged.metadata.bytes,
                checksum=staged.metadata.checksum,
            ),
        )
        self._committer.commit(objects=(staged,), coverage=coverage)
        return ClassificationIngestResult(
            snapshot_date=snapshot.snapshot_date,
            file_creation_time=snapshot.file_creation_time,
            coverage=coverage,
            objects=(staged.metadata,),
        )


class FundamentalsIngest:
    """Merge and publish XBRL facts per CIK and ``filed`` knowledge date."""

    def __init__(self, *, committer: ReferenceIngestCommitter) -> None:
        self._committer = committer

    def run(self, facts: Sequence[XbrlFact]) -> FundamentalsIngestResult:
        """Merge facts into per-CIK, per-filed objects and commit coverage."""
        grouped: dict[tuple[int, date], list[XbrlFact]] = {}
        for fact in facts:
            grouped.setdefault((fact.cik, fact.filed), []).append(fact)

        objects: list[StagedReferenceObject] = []
        coverage: list[CoverageRecord] = []
        for cik, filed_date in sorted(grouped):
            path = reference_partition_path(
                knowledge_date=filed_date,
                data_type=DataType.FUNDAMENTALS,
                name=cik_object_name(cik),
            )
            rows = _merged_rows(
                _existing_rows(self._committer.object_store, path),
                grouped[(cik, filed_date)],
            )
            table = fundamentals_table(rows)
            staged = _stage(table, path)
            objects.append(staged)
            coverage.append(
                CoverageRecord(
                    asset_class=AssetClass.REFERENCE,
                    data_type=DataType.FUNDAMENTALS,
                    symbol=cik_symbol(cik),
                    session_date=filed_date,
                    status=CoverageStatus.COMPLETE,
                    object_path=staged.metadata.path,
                    row_count=staged.metadata.rows,
                    byte_size=staged.metadata.bytes,
                    checksum=staged.metadata.checksum,
                )
            )
        self._committer.commit(objects=objects, coverage=coverage)
        return FundamentalsIngestResult(
            ciks=tuple(sorted({cik for cik, _ in grouped})),
            coverage=tuple(coverage),
            objects=tuple(item.metadata for item in objects),
        )


async def ingest_classification(
    *,
    client: ReferenceFetchClient,
    settings: ReferenceSettings,
    object_store: ReferenceObjectStore,
    catalog: ReferenceCoverageSink,
) -> dict[str, object]:
    """Fetch the current symbol directory and publish its snapshot partition."""
    payload = await client.get_bytes(settings.classification_url)
    snapshot = parse_nasdaq_traded(payload)
    result = ClassificationIngest(
        committer=ReferenceIngestCommitter(object_store=object_store, catalog=catalog)
    ).run(snapshot)
    return _classification_summary(result)


async def ingest_fundamentals_increment(
    *,
    client: ReferenceFetchClient,
    settings: ReferenceSettings,
    ciks: Sequence[int],
    tags: Sequence[str] | None = None,
    object_store: ReferenceObjectStore,
    catalog: ReferenceCoverageSink,
) -> dict[str, object]:
    """Fetch per-CIK companyfacts documents and merge their facts."""
    facts: list[XbrlFact] = []
    for cik in ciks:
        url = company_facts_url(settings.company_facts_url_template, cik)
        document = cast("Mapping[str, object]", await client.get_json(url))
        facts.extend(parse_companyfacts(document))
    result = _run_fundamentals(filter_facts(facts, tags=tags), object_store, catalog)
    return _fundamentals_summary(result)


def ingest_fundamentals_zip(
    *,
    zip_payload: bytes,
    ciks: Sequence[int] | None = None,
    tags: Sequence[str] | None = None,
    object_store: ReferenceObjectStore,
    catalog: ReferenceCoverageSink,
) -> dict[str, object]:
    """Backfill fundamentals from a local or fetched companyfacts zip."""
    facts: list[XbrlFact] = []
    for _, bundle in read_companyfacts_zip(zip_payload):
        facts.extend(bundle)
    result = _run_fundamentals(
        filter_facts(facts, ciks=ciks, tags=tags), object_store, catalog
    )
    return _fundamentals_summary(result)


def _run_fundamentals(
    facts: Sequence[XbrlFact],
    object_store: ReferenceObjectStore,
    catalog: ReferenceCoverageSink,
) -> FundamentalsIngestResult:
    return FundamentalsIngest(
        committer=ReferenceIngestCommitter(object_store=object_store, catalog=catalog)
    ).run(facts)


def _stage(table: pa.Table, path: str) -> StagedReferenceObject:
    """Serialize one table and return its staged, deterministic payload."""
    output = io.BytesIO()
    pq.write_table(
        table,
        output,
        compression="zstd",
        use_compliant_nested_type=False,
        version="2.6",
    )
    payload = output.getvalue()
    metadata = ParquetMetadata(
        path=path,
        rows=table.num_rows,
        bytes=len(payload),
        checksum=hashlib.sha256(payload).hexdigest(),
    )
    return StagedReferenceObject(metadata=metadata, payload=payload)


def _existing_rows(
    store: ReferenceObjectStore, path: str
) -> tuple[Mapping[str, object], ...]:
    try:
        payload = store.read(path)
    except FileNotFoundError:
        return ()
    table = pq.read_table(io.BytesIO(payload))
    if not table.schema.equals(FUNDAMENTALS_SCHEMA, check_metadata=False):
        raise ValueError(
            f"existing fundamentals object has an unexpected schema: {path}"
        )
    return tuple(table.to_pylist())


def _merged_rows(
    existing: Iterable[Mapping[str, object]], facts: Iterable[XbrlFact]
) -> list[Mapping[str, object]]:
    rows: dict[tuple[object, ...], Mapping[str, object]] = {}
    for row in existing:
        rows[_row_key(row)] = row
    for fact in facts:
        row = _fact_row(fact)
        rows[_row_key(row)] = row
    return [rows[key] for key in sorted(rows)]


def _row_key(row: Mapping[str, object]) -> tuple[object, ...]:
    return tuple(_key_value(row[name]) for name in _ROW_FIELDS)


def _key_value(value: object) -> object:
    # Tuples are both dedup identity and sort key; nullable columns such as
    # fy, fp, and frame need a total order that tolerates None.
    if value is None:
        return (1, "")
    return (0, value)


def _fact_row(fact: XbrlFact) -> dict[str, object]:
    return {
        "cik": fact.cik,
        "taxonomy": fact.taxonomy,
        "tag": fact.tag,
        "unit": fact.unit,
        "period_end": fact.period_end,
        "fy": fact.fy,
        "fp": fact.fp,
        "form": fact.form,
        "accn": fact.accn,
        "filed": fact.filed,
        "frame": fact.frame,
        "val": fact.val,
    }


def filter_facts(
    facts: Iterable[XbrlFact],
    *,
    ciks: Sequence[int] | None = None,
    tags: Sequence[str] | None = None,
) -> tuple[XbrlFact, ...]:
    """Keep facts for the selected CIKs and XBRL tags, if filters are given."""
    selected_ciks = None if ciks is None else frozenset(ciks)
    selected_tags = None if tags is None else frozenset(tags)
    return tuple(
        fact
        for fact in facts
        if (selected_ciks is None or fact.cik in selected_ciks)
        and (selected_tags is None or fact.tag in selected_tags)
    )


def _classification_summary(result: ClassificationIngestResult) -> dict[str, object]:
    return {
        "coverage": _coverage_summary(result.coverage),
        "file_creation_time": result.file_creation_time.isoformat(),
        "objects": _object_summary(result.objects),
        "snapshot_date": result.snapshot_date.isoformat(),
    }


def _fundamentals_summary(result: FundamentalsIngestResult) -> dict[str, object]:
    return {
        "ciks": list(result.ciks),
        "coverage": _coverage_summary(result.coverage),
        "objects": _object_summary(result.objects),
    }


def _coverage_summary(coverage: Sequence[CoverageRecord]) -> list[dict[str, object]]:
    return [
        {
            "rows": record.row_count,
            "session_date": record.session_date.isoformat(),
            "status": record.status.value,
            "symbol": record.symbol,
        }
        for record in coverage
    ]


def _object_summary(objects: Sequence[ParquetMetadata]) -> list[dict[str, object]]:
    return [
        {
            "bytes": item.bytes,
            "checksum": item.checksum,
            "path": item.path,
            "rows": item.rows,
        }
        for item in objects
    ]


__all__ = [
    "CLASSIFICATION_COVERAGE_SYMBOL",
    "ClassificationIngest",
    "ClassificationIngestResult",
    "FundamentalsIngest",
    "FundamentalsIngestResult",
    "ReferenceCoverageSink",
    "ReferenceIngestCommitter",
    "ReferenceObjectStore",
    "StagedReferenceObject",
    "filter_facts",
    "ingest_classification",
    "ingest_fundamentals_increment",
    "ingest_fundamentals_zip",
]
