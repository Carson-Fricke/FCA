import asyncio
import io
import json
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pyarrow.parquet as pq
import pytest

from lake.catalog import (
    CatalogRepository,
    CoverageRecord,
    CoverageStatus,
    InMemoryCatalogRepository,
)
from lake.contracts import AssetClass, DataType, schema_for
from lake.coverage import CoverageSession
from lake.ingestion.equities.transaction import (
    AtomicIngestCommitter,
    EquityIngestTransaction,
    StagedParquetObject,
)
from lake.providers.alpaca import NormalizedQuote, NormalizedTrade

ROOT = Path(__file__).parents[4]
GOLDEN_FIXTURES = ROOT / "tests" / "fixtures" / "equities" / "golden"


class FakeClient:
    def __init__(
        self,
        trades: Sequence[NormalizedTrade] = (),
        quotes: Sequence[NormalizedQuote] = (),
        *,
        trade_error: Exception | None = None,
        quote_error: Exception | None = None,
    ) -> None:
        self.trades = tuple(trades)
        self.quotes = tuple(quotes)
        self.trade_error = trade_error
        self.quote_error = quote_error
        self.calls: list[
            tuple[str, str, date | datetime | str, date | datetime | str]
        ] = []

    async def get_trades(
        self, symbol: str, start: date | datetime | str, end: date | datetime | str
    ) -> Sequence[NormalizedTrade]:
        self.calls.append(("trade", symbol, start, end))
        if self.trade_error is not None:
            raise self.trade_error
        return self.trades

    async def get_quotes(
        self, symbol: str, start: date | datetime | str, end: date | datetime | str
    ) -> Sequence[NormalizedQuote]:
        self.calls.append(("quote", symbol, start, end))
        if self.quote_error is not None:
            raise self.quote_error
        return self.quotes


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
        # Validate the full batch before exposing either object or row.
        if len({item.metadata.path for item in staged_objects}) != len(staged_objects):
            raise AssertionError("duplicate object path")
        if len({item.key for item in staged_coverage}) != len(staged_coverage):
            raise AssertionError("duplicate coverage key")
        self.commits.append((staged_objects, staged_coverage))
        for item in staged_objects:
            self.objects[item.metadata.path] = item.payload
        for record in staged_coverage:
            self.catalog.upsert(record)


def _session(*, partial: bool = False) -> CoverageSession:
    return CoverageSession(
        session_date=date(2024, 1, 19),
        open_time=datetime(2024, 1, 19, 14, 30, tzinfo=UTC),
        close_time=datetime(2024, 1, 19, 21, tzinfo=UTC),
        is_partial=partial,
    )


def _trade(
    timestamp: str,
    trade_id: int,
    *,
    symbol: str = "AAPL",
) -> NormalizedTrade:
    return NormalizedTrade(
        timestamp=datetime.fromisoformat(timestamp.replace("Z", "+00:00")),
        symbol=symbol,
        trade_id=trade_id,
        exchange="XNAS",
        price=Decimal("190.125"),
        size=100,
        conditions=("@",),
        tape="C",
        update=None,
    )


def _quote(timestamp: str, *, symbol: str = "AAPL") -> NormalizedQuote:
    return NormalizedQuote(
        timestamp=datetime.fromisoformat(timestamp.replace("Z", "+00:00")),
        symbol=symbol,
        bid_exchange="XNAS",
        bid_price=Decimal("190.12"),
        bid_size=100,
        ask_exchange="XNAS",
        ask_price=Decimal("190.13"),
        ask_size=200,
        conditions=(),
        tape="C",
    )


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _json_rows(payload: bytes) -> list[dict[str, object]]:
    rows = pq.read_table(io.BytesIO(payload)).to_pylist()
    return [
        {
            key: value.isoformat().replace("+00:00", "Z")
            if isinstance(value, datetime)
            else str(value)
            if isinstance(value, Decimal)
            else value
            for key, value in row.items()
        }
        for row in rows
    ]


def test_transaction_fetches_stages_and_atomically_publishes_both_datasets() -> None:
    session = _session()
    client = FakeClient(
        trades=[
            _trade("2024-01-19T14:31:00Z", 2),
            _trade("2024-01-19T14:30:00Z", 1),
        ],
        quotes=[_quote("2024-01-19T14:30:00.123456Z")],
    )
    committer = AtomicFake()
    transaction = EquityIngestTransaction(client=client, committer=committer)

    result = _run(transaction.run(" aapl ", session))

    assert client.calls == [
        ("trade", "AAPL", session.open_time, session.close_time),
        ("quote", "AAPL", session.open_time, session.close_time),
    ]
    assert [record.data_type for record in result.coverage] == [
        DataType.TRADE,
        DataType.QUOTE,
    ]
    assert [record.status for record in result.coverage] == [
        CoverageStatus.COMPLETE,
        CoverageStatus.COMPLETE,
    ]
    assert [item.path for item in result.objects] == [
        "date=2024-01-19/asset_class=equity/data_type=trade/AAPL.parquet",
        "date=2024-01-19/asset_class=equity/data_type=quote/AAPL.parquet",
    ]
    assert len(committer.commits) == 1
    assert [record.row_count for record in result.coverage] == [2, 1]

    trade_table = pq.read_table(io.BytesIO(committer.objects[result.objects[0].path]))
    quote_table = pq.read_table(io.BytesIO(committer.objects[result.objects[1].path]))
    assert trade_table.schema.equals(
        schema_for(AssetClass.EQUITY, DataType.TRADE), check_metadata=True
    )
    assert quote_table.schema.equals(
        schema_for(AssetClass.EQUITY, DataType.QUOTE), check_metadata=True
    )
    assert trade_table.column("trade_id").to_pylist() == [1, 2]


@pytest.mark.parametrize("symbol", ["AAPL", "SPY"])
def test_equity_parquet_round_trip_matches_golden_rows(symbol: str) -> None:
    committer = AtomicFake()
    transaction = EquityIngestTransaction(
        client=FakeClient(
            trades=[
                _trade("2024-01-19T14:31:00Z", 2, symbol=symbol),
                _trade("2024-01-19T14:30:00Z", 1, symbol=symbol),
            ],
            quotes=[
                _quote("2024-01-19T14:30:00.123456Z", symbol=symbol),
            ],
        ),
        committer=committer,
    )

    result = _run(transaction.run(symbol, _session()))
    golden: Any = json.loads(
        (GOLDEN_FIXTURES / f"{symbol.lower()}.json").read_text(encoding="utf-8")
    )

    assert {
        metadata.path.split("data_type=", maxsplit=1)[1].split("/", maxsplit=1)[
            0
        ]: _json_rows(committer.objects[metadata.path])
        for metadata in result.objects
    } == golden
    assert all(
        record.checksum == metadata.checksum
        and record.byte_size == metadata.bytes
        and record.row_count == metadata.rows
        for record, metadata in zip(result.coverage, result.objects, strict=True)
    )


def test_repeated_transaction_replaces_the_same_bytes_and_catalog_keys() -> None:
    committer = AtomicFake()
    transaction = EquityIngestTransaction(
        client=FakeClient(
            trades=[_trade("2024-01-19T14:30:00Z", 1)],
            quotes=[_quote("2024-01-19T14:30:00Z")],
        ),
        committer=committer,
    )

    first = _run(transaction.run("AAPL", _session()))
    first_objects = dict(committer.objects)
    second = _run(transaction.run("AAPL", _session()))
    second_objects = dict(committer.objects)

    assert first.objects == second.objects
    assert first.coverage == second.coverage
    assert first_objects == second_objects
    assert len(committer.commits) == 2
    assert len(committer.catalog.list()) == 2


def test_fetch_failure_publishes_only_atomic_error_metadata() -> None:
    committer = AtomicFake()
    transaction = EquityIngestTransaction(
        client=FakeClient(
            trades=[_trade("2024-01-19T14:30:00Z", 1)],
            quotes=[_quote("2024-01-19T14:30:00Z")],
            quote_error=RuntimeError("provider unavailable"),
        ),
        committer=committer,
    )

    result = _run(transaction.run("AAPL", _session()))

    assert result.objects == ()
    assert committer.commits[0][0] == ()
    assert [record.status for record in result.coverage] == [
        CoverageStatus.ERROR,
        CoverageStatus.ERROR,
    ]
    assert all(
        record.error_message == "quote: RuntimeError: provider unavailable"
        for record in result.coverage
    )
    assert committer.objects == {}
    assert set(committer.catalog.list()) == set(result.coverage)


def test_partial_and_empty_sessions_are_recorded_with_empty_parquet_objects() -> None:
    partial_committer = AtomicFake()
    partial = EquityIngestTransaction(
        client=FakeClient(),
        committer=partial_committer,
    )

    partial_result = _run(partial.run("AAPL", _session(partial=True)))

    assert [record.status for record in partial_result.coverage] == [
        CoverageStatus.PARTIAL,
        CoverageStatus.PARTIAL,
    ]
    assert [record.row_count for record in partial_result.coverage] == [0, 0]
    assert all(record.object_path for record in partial_result.coverage)

    closed_committer = AtomicFake()
    closed = EquityIngestTransaction(client=FakeClient(), committer=closed_committer)
    empty_result = _run(closed.run("AAPL", _session()))

    assert [record.status for record in empty_result.coverage] == [
        CoverageStatus.EMPTY,
        CoverageStatus.EMPTY,
    ]
    assert all(record.row_count == 0 for record in empty_result.coverage)
    assert all(item.payload for item in closed_committer.commits[0][0])


def test_invalid_provider_record_is_not_published_as_a_success() -> None:
    committer = AtomicFake()
    transaction = EquityIngestTransaction(
        client=FakeClient(
            trades=[_trade("2024-01-19T14:30:00Z", 1, symbol="MSFT")],
            quotes=[_quote("2024-01-19T14:30:00Z")],
        ),
        committer=committer,
    )

    result = _run(transaction.run("AAPL", _session()))

    assert result.objects == ()
    assert committer.commits[0][0] == ()
    assert all(record.status is CoverageStatus.ERROR for record in result.coverage)
    assert "does not match" in (result.coverage[0].error_message or "")
    assert committer.objects == {}


def test_atomic_committer_is_a_runtime_injected_boundary() -> None:
    assert isinstance(AtomicFake(), AtomicIngestCommitter)


def test_print_stamped_exactly_at_session_close_is_published() -> None:
    # The fetch passes close_time as Alpaca's inclusive end, so the closing
    # auction print stamped exactly at 16:00:00 ET belongs to the session on
    # both the trade and quote paths.
    committer = AtomicFake()
    transaction = EquityIngestTransaction(
        client=FakeClient(
            trades=[_trade("2024-01-19T21:00:00Z", 1)],
            quotes=[_quote("2024-01-19T21:00:00Z")],
        ),
        committer=committer,
    )

    result = _run(transaction.run("AAPL", _session()))

    assert [record.status for record in result.coverage] == [
        CoverageStatus.COMPLETE,
        CoverageStatus.COMPLETE,
    ]
    assert [record.row_count for record in result.coverage] == [1, 1]


def test_print_stamped_after_session_close_is_rejected() -> None:
    committer = AtomicFake()
    transaction = EquityIngestTransaction(
        client=FakeClient(
            trades=[_trade("2024-01-19T21:00:00.500000Z", 1)],
            quotes=[_quote("2024-01-19T14:30:00Z")],
        ),
        committer=committer,
    )

    result = _run(transaction.run("AAPL", _session()))

    assert result.objects == ()
    assert committer.commits[0][0] == ()
    assert all(record.status is CoverageStatus.ERROR for record in result.coverage)
    assert "outside session" in (result.coverage[0].error_message or "")


class _BlipClient(FakeClient):
    def __init__(
        self,
        failures: int,
        trades: Sequence[NormalizedTrade] = (),
        quotes: Sequence[NormalizedQuote] = (),
    ) -> None:
        super().__init__(trades=trades, quotes=quotes)
        self.failures = failures

    async def get_trades(
        self, symbol: str, start: date | datetime | str, end: date | datetime | str
    ) -> Sequence[NormalizedTrade]:
        self.calls.append(("trade", symbol, start, end))
        if self.failures > 0:
            self.failures -= 1
            raise httpx.ConnectError("temporary DNS failure")
        return self.trades


def test_transient_fetch_blip_is_retried_within_the_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "lake.ingestion.common.TRANSIENT_RETRY_BASE_DELAY_SECONDS", 0.0
    )
    session = _session()
    client = _BlipClient(
        2,
        trades=[_trade("2024-01-19T14:30:00Z", 1)],
        quotes=[_quote("2024-01-19T14:30:00Z")],
    )
    committer = AtomicFake()
    transaction = EquityIngestTransaction(client=client, committer=committer)

    result = _run(transaction.run("AAPL", session))

    kinds = [call[0] for call in client.calls]
    assert kinds.count("trade") == 3
    assert kinds.count("quote") == 1
    assert result.retryable is False
    assert [record.status for record in result.coverage] == [
        CoverageStatus.COMPLETE,
        CoverageStatus.COMPLETE,
    ]
    assert [record.row_count for record in result.coverage] == [1, 1]
    assert len(committer.commits) == 1


def test_permanent_fetch_failure_is_not_retried() -> None:
    session = _session()
    client = FakeClient(trade_error=ValueError("bad request"))
    committer = AtomicFake()
    transaction = EquityIngestTransaction(client=client, committer=committer)

    result = _run(transaction.run("AAPL", session))

    kinds = [call[0] for call in client.calls]
    assert kinds.count("trade") == 1
    assert kinds.count("quote") == 1
    assert result.retryable is False
    assert all(record.status is CoverageStatus.ERROR for record in result.coverage)
    assert result.coverage[0].error_message == "trade: ValueError: bad request"
    assert result.objects == ()


def test_exhausted_transient_fetch_failure_is_marked_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "lake.ingestion.common.TRANSIENT_RETRY_BASE_DELAY_SECONDS", 0.0
    )
    session = _session()
    client = _BlipClient(
        3,
        trades=[_trade("2024-01-19T14:30:00Z", 1)],
        quotes=[_quote("2024-01-19T14:30:00Z")],
    )
    committer = AtomicFake()
    transaction = EquityIngestTransaction(client=client, committer=committer)

    result = _run(transaction.run("AAPL", session))

    kinds = [call[0] for call in client.calls]
    assert kinds.count("trade") == 3
    assert kinds.count("quote") == 1
    assert result.retryable is True
    assert all(record.status is CoverageStatus.ERROR for record in result.coverage)
    assert (result.coverage[0].error_message or "").startswith("trade: ConnectError")
    assert result.objects == ()
    assert committer.commits[0][0] == ()


