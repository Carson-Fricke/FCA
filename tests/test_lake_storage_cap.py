import asyncio
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from lake.alpaca import NormalizedQuote, NormalizedTrade
from lake.catalog import (
    CoverageRecord,
    CoverageStatus,
    InMemoryCatalogRepository,
)
from lake.coverage import CoverageSession
from lake.equity_backfill import enqueue_equity_backfill, plan_equity_backfill
from lake.equity_ingest import (
    AtomicIngestCommitter,
    EquityIngestTransaction,
    StagedParquetObject,
)
from lake.partition_paths import AssetClass, DataType
from lake.settings import load_settings
from lake.storage_cap import (
    StorageCapacityPaused,
    StorageCapGuard,
    StorageCapPolicy,
    StorageCapState,
    StorageOperation,
    StorageUsage,
    StorageUsageProvider,
    estimate_current_storage,
)

ROOT = Path(__file__).parents[1]


def _record(*, data_type: DataType, byte_size: int) -> CoverageRecord:
    return CoverageRecord(
        asset_class=AssetClass.EQUITY,
        data_type=data_type,
        symbol="AAPL",
        session_date=date(2024, 1, 19),
        status=CoverageStatus.COMPLETE,
        byte_size=byte_size,
    )


def _session() -> CoverageSession:
    return CoverageSession(
        session_date=date(2024, 1, 19),
        open_time=datetime(2024, 1, 19, 14, 30, tzinfo=UTC),
        close_time=datetime(2024, 1, 19, 21, tzinfo=UTC),
    )


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


class Sender:
    def __init__(self, messages: list[dict[str, object]]) -> None:
        self.messages = messages

    def send(self, message: Mapping[str, object]) -> None:
        self.messages.append(dict(message))


def test_catalog_estimate_sums_persisted_partition_bytes() -> None:
    catalog = InMemoryCatalogRepository(
        [
            _record(data_type=DataType.TRADE, byte_size=120),
            _record(data_type=DataType.QUOTE, byte_size=80),
        ]
    )

    assert estimate_current_storage(catalog) == StorageUsage(200)


def test_storage_usage_provider_is_injected_and_status_is_alertable() -> None:
    class Usage:
        def current_storage(self) -> StorageUsage:
            return StorageUsage(100)

    guard = StorageCapGuard(
        storage_usage=Usage(),
        settings=StorageCapPolicy(capacity_bytes=100),
    )

    status = guard.evaluate(StorageOperation.ENQUEUE)

    assert isinstance(Usage(), StorageUsageProvider)
    assert status.state is StorageCapState.PAUSED
    assert status.paused
    assert status.alertable
    assert status.as_alert() == {
        "alertable": True,
        "capacity_bytes": 100,
        "current_bytes": 100,
        "operation": "enqueue",
        "pause_enabled": True,
        "projected_bytes": 100,
        "reason": "capacity_reached",
        "state": "paused",
    }


def test_settings_control_independent_enqueue_and_ingest_pauses() -> None:
    settings = load_settings(ROOT).storage.model_copy(
        update={
            "capacity_bytes": 100,
            "pause_enqueue_at_capacity": False,
            "pause_ingest_at_capacity": True,
        }
    )
    guard = StorageCapGuard(storage_usage=lambda: 100, settings=settings)

    enqueue_status = guard.enforce_enqueue()
    assert enqueue_status.state is StorageCapState.AT_CAPACITY
    with pytest.raises(StorageCapacityPaused) as caught:
        guard.enforce_ingest()
    assert caught.value.status.operation is StorageOperation.INGEST


def test_projected_partition_bytes_cannot_reach_or_cross_the_cap() -> None:
    guard = StorageCapGuard(
        storage_usage=lambda: 90,
        settings=StorageCapPolicy(capacity_bytes=100),
    )

    with pytest.raises(StorageCapacityPaused) as caught:
        guard.enforce_ingest(additional_bytes=10)

    assert caught.value.status.projected_bytes == 100
    assert caught.value.status.reason == "capacity_reached"


def test_enqueue_pauses_before_sending_any_backfill_message() -> None:
    plan = plan_equity_backfill(
        InMemoryCatalogRepository(),
        symbols="AAPL",
        data_types=DataType.TRADE,
        start=date(2024, 1, 19),
        end=date(2024, 1, 19),
        as_of=datetime(2024, 1, 22, tzinfo=UTC),
    )
    sent: list[dict[str, object]] = []
    guard = StorageCapGuard(
        storage_usage=lambda: 100,
        settings=StorageCapPolicy(capacity_bytes=100),
    )

    with pytest.raises(StorageCapacityPaused):
        enqueue_equity_backfill(plan, Sender(sent), storage_guard=guard)

    assert sent == []
    assert guard.last_status is not None
    assert guard.last_status.operation is StorageOperation.ENQUEUE


def test_ingest_pauses_before_provider_fetch_or_atomic_commit() -> None:
    class NeverClient:
        def __init__(self) -> None:
            self.calls = 0

        async def get_trades(
            self,
            symbol: str,
            start: date | datetime | str,
            end: date | datetime | str,
        ) -> Sequence[NormalizedTrade]:
            del symbol, start, end
            self.calls += 1
            return ()

        async def get_quotes(
            self,
            symbol: str,
            start: date | datetime | str,
            end: date | datetime | str,
        ) -> Sequence[NormalizedQuote]:
            del symbol, start, end
            self.calls += 1
            return ()

    class Committer:
        def __init__(self) -> None:
            self.calls = 0

        def commit(
            self,
            *,
            objects: Sequence[StagedParquetObject],
            coverage: Sequence[CoverageRecord],
        ) -> None:
            del objects, coverage
            self.calls += 1

    client = NeverClient()
    committer = Committer()
    assert isinstance(committer, AtomicIngestCommitter)
    transaction = EquityIngestTransaction(
        client=client,
        committer=committer,
        storage_guard=StorageCapGuard(
            storage_usage=lambda: StorageUsage(100),
            settings=StorageCapPolicy(capacity_bytes=100),
        ),
    )

    with pytest.raises(StorageCapacityPaused):
        _run(transaction.run("AAPL", _session()))

    assert client.calls == 0
    assert committer.calls == 0


def test_ingest_rejects_staged_bytes_before_atomic_commit() -> None:
    class EmptyClient:
        async def get_trades(
            self,
            symbol: str,
            start: date | datetime | str,
            end: date | datetime | str,
        ) -> Sequence[NormalizedTrade]:
            del symbol, start, end
            return ()

        async def get_quotes(
            self,
            symbol: str,
            start: date | datetime | str,
            end: date | datetime | str,
        ) -> Sequence[NormalizedQuote]:
            del symbol, start, end
            return ()

    class Committer:
        def __init__(self) -> None:
            self.calls = 0

        def commit(
            self,
            *,
            objects: Sequence[StagedParquetObject],
            coverage: Sequence[CoverageRecord],
        ) -> None:
            del objects, coverage
            self.calls += 1

    committer = Committer()
    transaction = EquityIngestTransaction(
        client=EmptyClient(),
        committer=committer,
        storage_guard=StorageCapGuard(
            storage_usage=lambda: 0,
            settings=StorageCapPolicy(capacity_bytes=1),
        ),
    )

    with pytest.raises(StorageCapacityPaused) as caught:
        _run(transaction.run("AAPL", _session()))

    assert caught.value.status.current_bytes == 0
    assert caught.value.status.projected_bytes > 1
    assert committer.calls == 0
