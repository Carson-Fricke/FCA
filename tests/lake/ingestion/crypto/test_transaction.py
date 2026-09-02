import asyncio
import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from lake.catalog import (
    CatalogRepository,
    CoverageRecord,
    CoverageStatus,
    InMemoryCatalogRepository,
)
from lake.contracts import AssetClass, DataType, partition_path
from lake.coverage import CoverageSession, crypto_sessions
from lake.ingestion.common import PartitionWriter, StagedParquetObject
from lake.ingestion.crypto.backfill import enqueue_crypto_backfill, plan_crypto_backfill
from lake.ingestion.crypto.transaction import (
    CryptoIngestTransaction,
)
from lake.ingestion.crypto.worker import CryptoQueueHandler, create_crypto_worker
from lake.providers.alpaca import NormalizedCryptoQuote, NormalizedCryptoTrade
from lake.queue import (
    DeliveryOutcome,
    QueueDelivery,
    QueueMessage,
    QueueWorkerPolicy,
)
from lake.storage import ObjectStore, ParquetMetadata


class FixedClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class FakeClient:
    def __init__(
        self,
        *,
        trades_by_day: Mapping[date, Sequence[NormalizedCryptoTrade]] | None = None,
        quotes_by_day: Mapping[date, Sequence[NormalizedCryptoQuote]] | None = None,
        quote_error: Exception | None = None,
    ) -> None:
        self.trades_by_day = dict(trades_by_day or {})
        self.quotes_by_day = dict(quotes_by_day or {})
        self.quote_error = quote_error
        self.calls: list[tuple[str, str, datetime, datetime]] = []

    async def get_crypto_trades(
        self, pair: str, start: date | datetime | str, end: date | datetime | str
    ) -> Sequence[NormalizedCryptoTrade]:
        assert isinstance(start, datetime)
        assert isinstance(end, datetime)
        self.calls.append(("trade", pair, start, end))
        return self.trades_by_day.get(start.date(), ())

    async def get_crypto_quotes(
        self, pair: str, start: date | datetime | str, end: date | datetime | str
    ) -> Sequence[NormalizedCryptoQuote]:
        assert isinstance(start, datetime)
        assert isinstance(end, datetime)
        self.calls.append(("quote", pair, start, end))
        if self.quote_error is not None:
            raise self.quote_error
        return self.quotes_by_day.get(start.date(), ())


class AtomicFake:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.catalog: CatalogRepository = InMemoryCatalogRepository()
        self.commits: list[
            tuple[tuple[StagedParquetObject, ...], tuple[CoverageRecord, ...]]
        ] = []

    def commit(
        self,
        *,
        objects: Sequence[StagedParquetObject],
        coverage: Sequence[CoverageRecord],
    ) -> None:
        staged_objects = tuple(objects)
        staged_coverage = tuple(coverage)
        assert len({item.metadata.path for item in staged_objects}) == len(
            staged_objects
        )
        assert len({item.key for item in staged_coverage}) == len(staged_coverage)
        self.commits.append((staged_objects, staged_coverage))
        for item in staged_objects:
            self.objects[item.metadata.path] = item.payload
        for record in staged_coverage:
            self.catalog.upsert(record)


class RecordingWriter:
    def __init__(self, store: ObjectStore) -> None:
        self.store = store
        self.calls: list[tuple[date, AssetClass, DataType, str, int]] = []

    def write(
        self,
        records: Sequence[Mapping[str, object]],
        *,
        session_date: date,
        asset_class: AssetClass,
        data_type: DataType,
        symbol: str,
    ) -> ParquetMetadata:
        path = partition_path(
            session_date=session_date,
            asset_class=asset_class,
            data_type=data_type,
            symbol=symbol,
        )
        payload = json.dumps(
            [dict(record) for record in records],
            default=str,
            sort_keys=True,
        ).encode("ascii")
        self.store.replace(path, payload)
        self.calls.append((session_date, asset_class, data_type, symbol, len(payload)))
        return ParquetMetadata(
            path,
            len(records),
            len(payload),
            hashlib.sha256(payload).hexdigest(),
        )


class FakeQueue:
    def __init__(self, message: QueueMessage | None) -> None:
        self.message = message
        self.deleted: list[QueueMessage] = []

    async def receive_one(
        self, *, visibility_timeout_seconds: int
    ) -> QueueMessage | None:
        del visibility_timeout_seconds
        if self.message is None:
            return None
        message = self.message
        self.message = None
        return message

    async def renew(
        self, message: QueueMessage, *, visibility_timeout_seconds: int
    ) -> QueueMessage:
        del visibility_timeout_seconds
        return message

    async def delete(self, message: QueueMessage) -> None:
        self.deleted.append(message)


class FakeWorkerClock:
    async def sleep(self, seconds: float) -> None:
        del seconds
        await asyncio.sleep(0)


def _session(day: date, *, partial: bool = False) -> CoverageSession:
    as_of = (
        datetime.combine(day, datetime.min.time(), tzinfo=UTC).replace(hour=12)
        if partial
        else datetime.combine(day, datetime.min.time(), tzinfo=UTC) + timedelta(days=1)
    )
    return crypto_sessions(
        day,
        day,
        as_of=as_of,
    )[0]


def _trade(day: date, minute: int = 1) -> NormalizedCryptoTrade:
    return NormalizedCryptoTrade(
        timestamp=datetime(day.year, day.month, day.day, 0, minute, tzinfo=UTC),
        symbol="BTC/USD",
        trade_id=minute,
        exchange="CBSE",
        price=Decimal("41234.123456789012345678"),
        size=Decimal("0.010000000000000000"),
        taker_side="B",
    )


def _quote(day: date, minute: int = 1) -> NormalizedCryptoQuote:
    return NormalizedCryptoQuote(
        timestamp=datetime(day.year, day.month, day.day, 0, minute, tzinfo=UTC),
        symbol="BTC/USD",
        bid_exchange="CBSE",
        bid_price=Decimal("41234.12"),
        bid_size=Decimal("0.1"),
        ask_exchange="CBSE",
        ask_price=Decimal("41235.12"),
        ask_size=Decimal("0.2"),
    )


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_crypto_plan_covers_every_utc_day_including_weekends_and_holidays() -> None:
    current = datetime(2024, 12, 2, 12, tzinfo=UTC)
    plan = plan_crypto_backfill(
        InMemoryCatalogRepository(),
        pairs="btc/usd",
        data_types=(DataType.TRADE, DataType.QUOTE),
        start=date(2024, 11, 28),
        end=date(2024, 12, 2),
        as_of=current,
        order="oldest_first",
    )

    assert plan.session_dates == (
        date(2024, 11, 28),
        date(2024, 11, 29),
        date(2024, 11, 30),
        date(2024, 12, 1),
        date(2024, 12, 2),
    )
    assert len(plan) == 5
    assert all(item.queue_message()["asset_class"] == "crypto" for item in plan)


def test_current_partial_day_is_replanned_even_after_an_empty_attempt() -> None:
    current = datetime(2024, 1, 19, 12, tzinfo=UTC)
    catalog = InMemoryCatalogRepository(
        [
            CoverageRecord(
                asset_class=AssetClass.CRYPTO,
                data_type=DataType.TRADE,
                symbol="BTC/USD",
                session_date=current.date(),
                status=CoverageStatus.EMPTY,
            )
        ]
    )

    plan = plan_crypto_backfill(
        catalog,
        pairs="BTC/USD",
        data_types=DataType.TRADE,
        start=current.date(),
        end=current.date(),
        as_of=current,
    )

    assert len(plan) == 1
    assert plan.items[0].session.is_partial
    assert plan.items[0].replace_existing
    assert plan.items[0].queue_message()["end_time"] == "2024-01-19T12:00:00Z"


def test_enqueue_and_queue_handler_use_injected_clock_and_full_utc_bounds() -> None:
    day = date(2024, 1, 18)
    clock = FixedClock(datetime(2024, 1, 20, 12, tzinfo=UTC))
    plan = plan_crypto_backfill(
        InMemoryCatalogRepository(),
        pairs="BTC/USD",
        start=day,
        end=day,
        as_of=clock.value,
    )
    messages: list[dict[str, object]] = []

    class Sender:
        def send(self, message: Mapping[str, object]) -> None:
            messages.append(dict(message))

    assert enqueue_crypto_backfill(plan, Sender()) == 1
    payload = messages[0]
    assert payload["start_time"] == "2024-01-18T00:00:00Z"
    assert payload["end_time"] == "2024-01-19T00:00:00Z"

    client = FakeClient(
        trades_by_day={day: (_trade(day),)},
        quotes_by_day={day: (_quote(day),)},
    )
    committer = AtomicFake()
    writers: list[RecordingWriter] = []

    def writer_factory(store: ObjectStore) -> PartitionWriter:
        writer = RecordingWriter(store)
        writers.append(writer)
        return writer

    delivery = QueueDelivery(
        QueueMessage("message-1", "receipt-1", json.dumps(payload), 1),
        payload,
    )
    queue = FakeQueue(delivery.message)
    result = _run(
        create_crypto_worker(
            queue=queue,
            client=client,
            committer=committer,
            policy=QueueWorkerPolicy(10, 2, 3, False),
            writer_factory=writer_factory,
            current_clock=clock,
            clock=FakeWorkerClock(),
        ).process_one()
    )

    assert result is not None
    assert result.outcome == DeliveryOutcome.SUCCEEDED
    assert queue.deleted == [delivery.message]
    assert [(call[0], call[2], call[3]) for call in client.calls] == [
        ("trade", datetime(2024, 1, 18, tzinfo=UTC), datetime(2024, 1, 19, tzinfo=UTC)),
        ("quote", datetime(2024, 1, 18, tzinfo=UTC), datetime(2024, 1, 19, tzinfo=UTC)),
    ]
    assert {record.session_date for record in committer.catalog.list()} == {day}
    assert len(committer.objects) == 2
    assert [call[2] for call in writers[0].calls] == [DataType.TRADE, DataType.QUOTE]


def test_partial_ingest_refresh_replaces_objects_and_catalog_keys() -> None:
    day = date(2024, 1, 19)
    clock = FixedClock(datetime(2024, 1, 19, 12, tzinfo=UTC))
    client = FakeClient(
        trades_by_day={day: (_trade(day),)},
        quotes_by_day={day: (_quote(day),)},
    )
    committer = AtomicFake()
    writers: list[RecordingWriter] = []

    def writer_factory(store: ObjectStore) -> PartitionWriter:
        writer = RecordingWriter(store)
        writers.append(writer)
        return writer

    transaction = CryptoIngestTransaction(
        client=client,
        committer=committer,
        writer_factory=writer_factory,
        clock=clock,
    )

    first = _run(transaction.run("btc/usd", _session(day, partial=True)))
    assert client.calls[-2:] == [
        ("trade", "BTC/USD", datetime(2024, 1, 19, tzinfo=UTC), clock.value),
        ("quote", "BTC/USD", datetime(2024, 1, 19, tzinfo=UTC), clock.value),
    ]
    first_objects = dict(committer.objects)
    client.trades_by_day[day] = (_trade(day), _trade(day, 2))
    client.quotes_by_day[day] = (_quote(day), _quote(day, 2))
    clock.value = datetime(2024, 1, 19, 18, tzinfo=UTC)
    second = _run(transaction.run("BTC/USD", _session(day, partial=True)))

    assert client.calls[-2:] == [
        ("trade", "BTC/USD", datetime(2024, 1, 19, tzinfo=UTC), clock.value),
        ("quote", "BTC/USD", datetime(2024, 1, 19, tzinfo=UTC), clock.value),
    ]
    assert [item.path for item in first.objects] == [
        item.path for item in second.objects
    ]
    assert first_objects != committer.objects
    assert [record.status.value for record in second.coverage] == ["partial", "partial"]
    assert [record.row_count for record in second.coverage] == [2, 2]
    assert len(committer.catalog.list()) == 2
    assert [call[4] for call in writers[1].calls] == [
        len(committer.objects[second.objects[0].path]),
        len(committer.objects[second.objects[1].path]),
    ]


def test_failed_crypto_fetch_publishes_atomic_error_coverage_without_objects() -> None:
    day = date(2024, 1, 18)
    committer = AtomicFake()
    transaction = CryptoIngestTransaction(
        client=FakeClient(quote_error=RuntimeError("provider unavailable")),
        committer=committer,
        clock=FixedClock(datetime(2024, 1, 20, tzinfo=UTC)),
    )

    result = _run(transaction.run("BTC/USD", _session(day)))

    assert result.objects == ()
    assert committer.commits[0][0] == ()
    assert all(record.status.value == "error" for record in result.coverage)
    assert all(
        "quote: RuntimeError: provider unavailable" == record.error_message
        for record in result.coverage
    )
    assert committer.objects == {}


def test_crypto_handler_is_a_queue_worker_handler_and_rejects_non_crypto_messages() -> (
    None
):
    transaction = CryptoIngestTransaction(
        client=FakeClient(),
        committer=AtomicFake(),
        clock=FixedClock(datetime(2024, 1, 20, tzinfo=UTC)),
    )
    handler = CryptoQueueHandler(transaction)

    with pytest.raises(ValueError, match="asset_class"):
        _run(
            handler(
                QueueDelivery(
                    QueueMessage("message-1", "receipt-1", "{}", 1),
                    {"asset_class": "equity"},
                )
            )
        )
