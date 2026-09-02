import asyncio
import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import pytest

from lake.catalog import CoverageRecord, InMemoryCatalogRepository
from lake.coverage import CoverageSession
from lake.ingestion.equities.catchup import plan_equity_catch_up
from lake.ingestion.equities.transaction import (
    EquityIngestTransaction,
    StagedParquetObject,
)
from lake.providers.alpaca import NormalizedQuote, NormalizedTrade
from lake.queue import DeliveryOutcome, QueueDelivery, QueueMessage, QueueWorker
from lake.queue.worker import QueueWorkerPolicy


class FakeClient:
    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self.calls = 0

    async def get_trades(
        self,
        symbol: str,
        start: date | datetime | str,
        end: date | datetime | str,
    ) -> Sequence[NormalizedTrade]:
        del end
        self.calls += 1
        assert symbol == self.symbol
        assert isinstance(start, datetime)
        return (
            NormalizedTrade(
                timestamp=start,
                symbol=symbol,
                trade_id=1,
                exchange="XNAS",
                price=Decimal("190.125"),
                size=100,
                conditions=("@",),
                tape="C",
                update=None,
            ),
        )

    async def get_quotes(
        self,
        symbol: str,
        start: date | datetime | str,
        end: date | datetime | str,
    ) -> Sequence[NormalizedQuote]:
        del end
        self.calls += 1
        assert symbol == self.symbol
        assert isinstance(start, datetime)
        return (
            NormalizedQuote(
                timestamp=start,
                symbol=symbol,
                bid_exchange="XNAS",
                bid_price=Decimal("190.12"),
                bid_size=100,
                ask_exchange="XNAS",
                ask_price=Decimal("190.13"),
                ask_size=200,
                conditions=(),
                tape="C",
            ),
        )


class AtomicFake:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.catalog = InMemoryCatalogRepository()
        self.commits = 0

    def commit(
        self,
        *,
        objects: Sequence[StagedParquetObject],
        coverage: Sequence[CoverageRecord],
    ) -> None:
        self.commits += 1
        for item in objects:
            self.objects[item.metadata.path] = item.payload
        for record in coverage:
            self.catalog.upsert(record)


class FakeQueue:
    def __init__(self, *messages: QueueMessage) -> None:
        self.messages = list(messages)
        self.deleted: list[QueueMessage] = []

    async def receive_one(
        self, *, visibility_timeout_seconds: int
    ) -> QueueMessage | None:
        assert visibility_timeout_seconds == 10
        return self.messages.pop(0) if self.messages else None

    async def renew(
        self,
        message: QueueMessage,
        *,
        visibility_timeout_seconds: int,
    ) -> QueueMessage:
        raise AssertionError(
            f"unexpected renewal for {message.message_id}: {visibility_timeout_seconds}"
        )

    async def delete(self, message: QueueMessage) -> None:
        self.deleted.append(message)


def _text(payload: Mapping[str, object], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.mark.parametrize("symbol", ["AAPL", "SPY"])
def test_duplicate_delivery_replaces_equity_objects_and_catalog_rows(
    symbol: str,
) -> None:
    plan = plan_equity_catch_up(
        InMemoryCatalogRepository(),
        symbols=symbol,
        start=date(2024, 1, 19),
        end=date(2024, 1, 19),
        as_of=datetime.fromisoformat("2024-01-22T00:00:00+00:00"),
    )
    payload = plan.items[0].queue_message()
    content = json.dumps(payload, sort_keys=True)
    messages = (
        QueueMessage("message-1", "receipt-1", content, 1),
        QueueMessage("message-2", "receipt-2", content, 1),
    )
    queue = FakeQueue(*messages)
    client = FakeClient(symbol)
    committer = AtomicFake()
    transaction = EquityIngestTransaction(client=client, committer=committer)

    async def handle(delivery: QueueDelivery) -> None:
        assert delivery.payload["asset_class"] == "equity"
        assert delivery.payload["data_types"] == ["quote", "trade"]
        session = CoverageSession(
            session_date=date.fromisoformat(_text(delivery.payload, "session_date")),
            open_time=datetime.fromisoformat(
                _text(delivery.payload, "start_time").replace("Z", "+00:00")
            ),
            close_time=datetime.fromisoformat(
                _text(delivery.payload, "end_time").replace("Z", "+00:00")
            ),
        )
        await transaction.run(_text(delivery.payload, "symbol"), session)

    worker = QueueWorker(
        queue=queue,
        handler=handle,
        policy=QueueWorkerPolicy(10, 2, 3, False),
    )

    results = _run(worker.run(max_messages=2))

    assert [result.outcome for result in results] == [
        DeliveryOutcome.SUCCEEDED,
        DeliveryOutcome.SUCCEEDED,
    ]
    assert queue.deleted == list(messages)
    assert client.calls == 4
    assert committer.commits == 2
    assert len(committer.objects) == 2
    assert len(committer.catalog.list()) == 2
    assert {record.symbol for record in committer.catalog.list()} == {symbol}
