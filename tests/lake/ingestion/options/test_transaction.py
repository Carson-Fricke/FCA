import asyncio
import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from lake.catalog import (
    ContractRecord,
    CoverageRecord,
    CoverageStatus,
    InMemoryCatalogRepository,
    InMemoryContractRepository,
    OptionType,
)
from lake.catalog.front_sets import (
    ContractsCatalog,
    DailyFrontSet,
    InMemoryFrontSetCatalog,
)
from lake.configuration import load_universes
from lake.contracts import AssetClass, DataType, partition_path
from lake.coverage import CoverageSession
from lake.ingestion.common import StagedParquetObject
from lake.ingestion.options.backfill import (
    enqueue_option_backfill,
    plan_option_backfill,
)
from lake.ingestion.options.selector import OptionChainSelector
from lake.ingestion.options.transaction import (
    OptionIngestTransaction,
    OptionMarketDataClient,
)
from lake.ingestion.options.worker import create_option_worker
from lake.providers.alpaca import NormalizedOptionQuote, NormalizedOptionTrade
from lake.queue import QueueMessage, QueueWorkerPolicy
from lake.storage import ParquetMetadata

ROOT = Path(__file__).parents[4]
SESSION_DATE = date(2024, 1, 19)
SESSION = CoverageSession(
    session_date=SESSION_DATE,
    open_time=datetime(2024, 1, 19, 14, 30, tzinfo=UTC),
    close_time=datetime(2024, 1, 19, 21, tzinfo=UTC),
)


def _contract(
    symbol: str,
    underlying: str = "SPY",
    expiration: date = SESSION_DATE,
    strike: str = "500",
) -> ContractRecord:
    return ContractRecord(
        symbol=symbol,
        underlying=underlying,
        expiration_date=expiration,
        strike_price=Decimal(strike),
        option_type=OptionType.CALL,
    )


SPY_CONTRACT = _contract("SPY240119C00500000")


def _trade(
    timestamp: str, *, symbol: str = SPY_CONTRACT.symbol
) -> NormalizedOptionTrade:
    return NormalizedOptionTrade(
        timestamp=datetime.fromisoformat(timestamp.replace("Z", "+00:00")),
        symbol=symbol,
        underlying="SPY",
        expiration_date=SESSION_DATE,
        strike_price=Decimal("500"),
        option_type=OptionType.CALL,
        exchange="XOPR",
        price=Decimal("2.50"),
        size=3,
        condition="I",
    )


def _quote(
    timestamp: str, *, symbol: str = SPY_CONTRACT.symbol
) -> NormalizedOptionQuote:
    return NormalizedOptionQuote(
        timestamp=datetime.fromisoformat(timestamp.replace("Z", "+00:00")),
        symbol=symbol,
        underlying="SPY",
        expiration_date=SESSION_DATE,
        strike_price=Decimal("500"),
        option_type=OptionType.CALL,
        bid_exchange="XOPR",
        bid_price=Decimal("2.40"),
        bid_size=4,
        ask_exchange="XOPR",
        ask_price=Decimal("2.60"),
        ask_size=5,
        condition="A",
    )


class FakeOptionClient:
    def __init__(
        self,
        trades: Sequence[NormalizedOptionTrade] = (),
        quotes: Sequence[NormalizedOptionQuote] = (),
        *,
        trade_error: Exception | None = None,
        quote_error: Exception | None = None,
    ) -> None:
        self.trades = tuple(trades)
        self.quotes = tuple(quotes)
        self.trade_error = trade_error
        self.quote_error = quote_error
        self.calls: list[
            tuple[str, tuple[str, ...], date | datetime | str, date | datetime | str]
        ] = []

    async def get_option_trades(
        self,
        contracts: Sequence[ContractRecord],
        start: date | datetime | str,
        end: date | datetime | str,
    ) -> Sequence[NormalizedOptionTrade]:
        self.calls.append(
            (
                "trade",
                tuple(item.symbol for item in contracts),
                start,
                end,
            )
        )
        if self.trade_error is not None:
            raise self.trade_error
        return self.trades

    async def get_option_quotes(
        self,
        contracts: Sequence[ContractRecord],
        start: date | datetime | str,
        end: date | datetime | str,
    ) -> Sequence[NormalizedOptionQuote]:
        self.calls.append(
            (
                "quote",
                tuple(item.symbol for item in contracts),
                start,
                end,
            )
        )
        if self.quote_error is not None:
            raise self.quote_error
        return self.quotes


class FakeWriter:
    def __init__(self, store: Any) -> None:
        self.store = store
        self.calls: list[tuple[tuple[Mapping[str, object], ...], DataType, str]] = []

    def write(
        self,
        records: Sequence[Mapping[str, object]],
        *,
        session_date: date,
        asset_class: AssetClass,
        data_type: DataType,
        symbol: str,
    ) -> ParquetMetadata:
        rows = tuple(records)
        self.calls.append((rows, data_type, symbol))
        path = partition_path(
            session_date=session_date,
            asset_class=asset_class,
            data_type=data_type,
            symbol=symbol,
        )
        payload = json.dumps(rows, default=str, sort_keys=True).encode()
        self.store.replace(path, payload)
        return ParquetMetadata(
            path, len(rows), len(payload), hashlib.sha256(payload).hexdigest()
        )


class AtomicFake:
    def __init__(self, coverage: InMemoryCatalogRepository) -> None:
        self.coverage = coverage
        self.objects: dict[str, bytes] = {}
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
            self.coverage.upsert(record)


class FakeQueue:
    def __init__(self, *messages: QueueMessage) -> None:
        self.messages = list(messages)
        self.deleted: list[QueueMessage] = []

    async def receive_one(
        self, *, visibility_timeout_seconds: int
    ) -> QueueMessage | None:
        return self.messages.pop(0) if self.messages else None

    async def renew(
        self,
        message: QueueMessage,
        *,
        visibility_timeout_seconds: int,
    ) -> QueueMessage:
        return message

    async def delete(self, message: QueueMessage) -> None:
        self.deleted.append(message)


class FakeClock:
    def __init__(self) -> None:
        self.sleeps: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        await asyncio.sleep(0)


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _writer_factory(writers: list[FakeWriter]):
    def factory(store: Any) -> FakeWriter:
        writer = FakeWriter(store)
        writers.append(writer)
        return writer

    return factory


def test_option_planning_persists_daily_front_sets_and_resumes_terminal_coverage() -> (
    None
):
    contract_repository = InMemoryContractRepository([SPY_CONTRACT])
    front_sets = InMemoryFrontSetCatalog()
    catalog = ContractsCatalog(contract_repository, front_sets)
    coverage = InMemoryCatalogRepository()
    selector = OptionChainSelector.from_universes(load_universes(ROOT))

    first = plan_option_backfill(
        catalog,
        coverage,
        selector,
        underlyings="SPY",
        spots={"SPY": Decimal("500")},
        start=SESSION_DATE,
        end=SESSION_DATE,
        as_of=datetime(2024, 1, 20, tzinfo=UTC),
    )

    assert first.front_sets[0].symbols == (SPY_CONTRACT.symbol,)
    assert first.items[0].queue_message() == {
        "asset_class": "option",
        "data_types": ["trade", "quote"],
        "end_time": "2024-01-19T21:00:00Z",
        "expiration_date": "2024-01-19",
        "option_type": "call",
        "replace_existing": False,
        "session_date": "2024-01-19",
        "start_time": "2024-01-19T14:30:00Z",
        "strike_price": "500",
        "symbol": SPY_CONTRACT.symbol,
        "underlying": "SPY",
    }
    assert catalog.get_front_set("SPY", SESSION_DATE) == first.front_sets[0]

    for data_type, status in (
        (DataType.TRADE, CoverageStatus.EMPTY),
        (DataType.QUOTE, CoverageStatus.COMPLETE),
    ):
        coverage.upsert(
            CoverageRecord(
                asset_class=AssetClass.OPTION,
                data_type=data_type,
                symbol=SPY_CONTRACT.symbol,
                session_date=SESSION_DATE,
                status=status,
            )
        )

    resumed = plan_option_backfill(
        catalog,
        coverage,
        selector,
        underlyings="SPY",
        spots={"SPY": Decimal("500")},
        start=SESSION_DATE,
        end=SESSION_DATE,
        as_of=datetime(2024, 1, 20, tzinfo=UTC),
    )

    assert not resumed.items
    assert not resumed.has_more
    assert resumed.front_sets == first.front_sets


def test_empty_option_session_is_terminal_and_replacement_is_deterministic() -> None:
    coverage = InMemoryCatalogRepository()
    committer = AtomicFake(coverage)
    writers: list[FakeWriter] = []
    client = FakeOptionClient()
    transaction = OptionIngestTransaction(
        client=client,
        committer=committer,
        writer_factory=_writer_factory(writers),
    )

    empty = _run(transaction.run(SPY_CONTRACT, SESSION))

    assert [record.status for record in empty.coverage] == [
        CoverageStatus.EMPTY,
        CoverageStatus.EMPTY,
    ]
    assert [item.path for item in empty.objects] == [
        "date=2024-01-19/asset_class=option/data_type=trade/SPY240119C00500000.parquet",
        "date=2024-01-19/asset_class=option/data_type=quote/SPY240119C00500000.parquet",
    ]
    first_objects = dict(committer.objects)

    client.trades = (_trade("2024-01-19T14:31:00Z"),)
    client.quotes = (_quote("2024-01-19T14:32:00Z"),)
    complete = _run(transaction.run(SPY_CONTRACT, SESSION))

    assert [record.status for record in complete.coverage] == [
        CoverageStatus.COMPLETE,
        CoverageStatus.COMPLETE,
    ]
    assert tuple(item.path for item in complete.objects) == tuple(
        empty_item.path for empty_item in empty.objects
    )
    assert first_objects != committer.objects
    assert len(committer.commits) == 2
    assert len(coverage.list(asset_class=AssetClass.OPTION)) == 2


def test_shared_queue_worker_deletes_duplicate_delivery_after_idempotent_option_commit() -> (
    None
):
    coverage = InMemoryCatalogRepository()
    committer = AtomicFake(coverage)
    front_sets = InMemoryFrontSetCatalog()
    front_sets.upsert(DailyFrontSet("SPY", SESSION_DATE, (SPY_CONTRACT,)))
    client = FakeOptionClient(
        trades=(_trade("2024-01-19T14:31:00Z"),),
        quotes=(_quote("2024-01-19T14:32:00Z"),),
    )
    payload = {
        "asset_class": "option",
        "data_types": ["trade", "quote"],
        "end_time": "2024-01-19T21:00:00Z",
        "expiration_date": "2024-01-19",
        "option_type": "call",
        "session_date": "2024-01-19",
        "start_time": "2024-01-19T14:30:00Z",
        "strike_price": "500",
        "symbol": SPY_CONTRACT.symbol,
        "underlying": "SPY",
    }
    queue = FakeQueue(
        QueueMessage("message-1", "receipt-1", json.dumps(payload), 1),
        QueueMessage("message-2", "receipt-2", json.dumps(payload), 1),
    )
    clock = FakeClock()
    worker = create_option_worker(
        queue=queue,
        client=client,
        committer=committer,
        front_sets=front_sets,
        policy=QueueWorkerPolicy(10, 2, 3, False),
        writer_factory=FakeWriter,
        clock=clock,
    )

    results = _run(worker.run(max_messages=2))

    assert [result.outcome for result in results] == ["succeeded", "succeeded"]
    assert len(queue.deleted) == 2
    assert len(committer.commits) == 2
    assert len(coverage.list(asset_class=AssetClass.OPTION)) == 2
    assert [call[0] for call in client.calls] == ["trade", "quote"] * 2


def test_option_provider_failure_publishes_error_coverage_without_objects() -> None:
    coverage = InMemoryCatalogRepository()
    committer = AtomicFake(coverage)
    writers: list[FakeWriter] = []
    transaction = OptionIngestTransaction(
        client=FakeOptionClient(quote_error=RuntimeError("OPRA unavailable")),
        committer=committer,
        writer_factory=_writer_factory(writers),
    )

    result = _run(transaction.run(SPY_CONTRACT, SESSION))

    assert result.objects == ()
    assert writers == []
    assert all(record.status is CoverageStatus.ERROR for record in result.coverage)
    assert all(
        record.error_message == "quote: RuntimeError: OPRA unavailable"
        for record in result.coverage
    )


def test_option_print_stamped_exactly_at_session_close_is_published() -> None:
    # The fetch passes close_time as Alpaca's inclusive end, so the closing
    # auction print stamped exactly at 21:00:00Z belongs to the session on
    # both the trade and quote paths.
    coverage = InMemoryCatalogRepository()
    committer = AtomicFake(coverage)
    writers: list[FakeWriter] = []
    transaction = OptionIngestTransaction(
        client=FakeOptionClient(
            trades=(_trade("2024-01-19T21:00:00Z"),),
            quotes=(_quote("2024-01-19T21:00:00Z"),),
        ),
        committer=committer,
        writer_factory=_writer_factory(writers),
    )

    result = _run(transaction.run(SPY_CONTRACT, SESSION))

    assert [record.status for record in result.coverage] == [
        CoverageStatus.COMPLETE,
        CoverageStatus.COMPLETE,
    ]
    assert [record.row_count for record in result.coverage] == [1, 1]


def test_option_print_stamped_after_session_close_is_rejected() -> None:
    coverage = InMemoryCatalogRepository()
    committer = AtomicFake(coverage)
    writers: list[FakeWriter] = []
    transaction = OptionIngestTransaction(
        client=FakeOptionClient(
            trades=(_trade("2024-01-19T21:00:00.500000Z"),),
            quotes=(_quote("2024-01-19T14:31:00Z"),),
        ),
        committer=committer,
        writer_factory=_writer_factory(writers),
    )

    result = _run(transaction.run(SPY_CONTRACT, SESSION))

    assert result.objects == ()
    assert committer.objects == {}
    assert all(record.status is CoverageStatus.ERROR for record in result.coverage)
    assert "outside session" in (result.coverage[0].error_message or "")


def test_option_plan_enqueue_uses_injected_sender() -> None:
    catalog = ContractsCatalog(InMemoryContractRepository([SPY_CONTRACT]))
    coverage = InMemoryCatalogRepository()
    selector = OptionChainSelector.from_universes(load_universes(ROOT))
    plan = plan_option_backfill(
        catalog,
        coverage,
        selector,
        underlyings="SPY",
        spots={"SPY": Decimal("500")},
        start=SESSION_DATE,
        end=SESSION_DATE,
        as_of=datetime(2024, 1, 20, tzinfo=UTC),
    )
    messages: list[Mapping[str, object]] = []

    class Sender:
        def send(self, message: Mapping[str, object]) -> None:
            messages.append(message)

    assert enqueue_option_backfill(plan, Sender()) == 1
    assert messages == [plan.items[0].queue_message()]


def test_option_client_protocol_is_injected_without_provider_construction() -> None:
    assert isinstance(FakeOptionClient(), OptionMarketDataClient)


def test_option_planner_uses_injected_clock_and_rejects_two_time_sources() -> None:
    catalog = ContractsCatalog(InMemoryContractRepository([SPY_CONTRACT]))
    coverage = InMemoryCatalogRepository()
    selector = OptionChainSelector.from_universes(load_universes(ROOT))

    class Clock:
        def __call__(self) -> datetime:
            return datetime(2024, 1, 20, tzinfo=UTC)

    plan = plan_option_backfill(
        catalog,
        coverage,
        selector,
        underlyings="SPY",
        spots={"SPY": Decimal("500")},
        start=SESSION_DATE,
        end=SESSION_DATE,
        clock=Clock(),
    )

    assert len(plan) == 1
    with pytest.raises(ValueError, match="must not both"):
        plan_option_backfill(
            catalog,
            coverage,
            selector,
            underlyings="SPY",
            spots={"SPY": Decimal("500")},
            start=SESSION_DATE,
            end=SESSION_DATE,
            as_of=datetime(2024, 1, 20, tzinfo=UTC),
            clock=Clock(),
        )
