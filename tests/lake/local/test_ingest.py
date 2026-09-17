import asyncio
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal

import pyarrow.parquet as pq
import pytest

from lake.catalog.models import CoverageStatus
from lake.contracts import AssetClass, DataType
from lake.coverage.application import EnsureJob, EnsureSpec
from lake.local import LocalCatalogRepository, LocalIngestError, LocalLayout
from lake.local.ensure import LocalEnsureLauncher
from lake.local.ingest import run_local_backfill, run_local_ingest
from lake.providers.alpaca import FetchedRecords
from lake.providers.alpaca.records import NormalizedQuote, NormalizedTrade


class FakeAlpaca:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def fetch_trades(
        self,
        symbols: str,
        start: date | datetime | str,
        end: date | datetime | str,
    ) -> Sequence[NormalizedTrade]:
        del start, end
        self.calls.append(("trade", symbols))
        return (
            NormalizedTrade(
                timestamp=datetime(2024, 1, 19, 14, 31, tzinfo=UTC),
                symbol=symbols,
                trade_id=1,
                exchange="XNAS",
                price=Decimal("190.12"),
                size=10,
                conditions=("@",),
                tape="C",
                update=None,
            ),
        )

    async def fetch_quotes(
        self,
        symbols: str,
        start: date | datetime | str,
        end: date | datetime | str,
    ) -> Sequence[NormalizedQuote]:
        del start, end
        self.calls.append(("quote", symbols))
        return (
            NormalizedQuote(
                timestamp=datetime(2024, 1, 19, 14, 31, tzinfo=UTC),
                symbol=symbols,
                bid_exchange="XNAS",
                bid_price=Decimal("190.11"),
                bid_size=10,
                ask_exchange="XNAS",
                ask_price=Decimal("190.13"),
                ask_size=20,
                conditions=(),
                tape="C",
            ),
        )


def _credentials() -> dict[str, str]:
    return {
        "LAKE_PROVIDER__API_KEY": "fake-key",
        "LAKE_PROVIDER__API_SECRET": "fake-secret",
    }


def test_fake_alpaca_ingest_persists_parquet_and_catalog(tmp_path) -> None:
    layout = LocalLayout(tmp_path / "local")
    provider = FakeAlpaca()

    result = asyncio.run(
        run_local_ingest(
            layout=layout,
            symbol="aapl",
            session_date=date(2024, 1, 19),
            provider=provider,
            environ=_credentials(),
            now=datetime(2024, 1, 22, tzinfo=UTC),
        )
    )

    assert provider.calls == [("trade", "AAPL"), ("quote", "AAPL")]
    assert result["symbol"] == "AAPL"
    assert result["session_date"] == "2024-01-19"
    assert result["coverage"] == [
        {"data_type": "trade", "rows": 1, "status": "complete"},
        {"data_type": "quote", "rows": 1, "status": "complete"},
    ]
    catalog = LocalCatalogRepository(layout.catalog)
    records = catalog.list(symbol="AAPL")
    assert [record.status for record in records] == [
        CoverageStatus.COMPLETE,
        CoverageStatus.COMPLETE,
    ]
    assert [record.data_type for record in records] == [DataType.QUOTE, DataType.TRADE]
    for record in records:
        assert record.object_path is not None
        table = pq.read_table(layout.objects / record.object_path)
        assert table.num_rows == 1
        assert table.column("symbol").to_pylist() == ["AAPL"]


def test_missing_credentials_fail_before_provider_or_state_use(tmp_path) -> None:
    layout = LocalLayout(tmp_path / "must-not-exist")
    provider = FakeAlpaca()

    with pytest.raises(LocalIngestError, match="API_KEY.*API_SECRET"):
        asyncio.run(
            run_local_ingest(
                layout=layout,
                provider=provider,
                environ={},
                now=datetime(2024, 1, 22, tzinfo=UTC),
            )
        )

    assert provider.calls == []
    assert not layout.root.exists()


def test_ingest_accepts_symbols_outside_the_configured_universe(tmp_path) -> None:
    layout = LocalLayout(tmp_path / "local")
    provider = FakeAlpaca()

    result = asyncio.run(
        run_local_ingest(
            layout=layout,
            symbol="MSFT",
            session_date=date(2024, 1, 19),
            provider=provider,
            environ=_credentials(),
            now=datetime(2024, 1, 22, tzinfo=UTC),
        )
    )

    assert provider.calls == [("trade", "MSFT"), ("quote", "MSFT")]
    assert result["symbol"] == "MSFT"


def test_fake_provider_failure_is_persisted_but_returns_a_clear_error(tmp_path) -> None:
    class FailingAlpaca(FakeAlpaca):
        async def fetch_quotes(
            self,
            symbols: str,
            start: date | datetime | str,
            end: date | datetime | str,
        ) -> Sequence[NormalizedQuote]:
            del symbols, start, end
            raise RuntimeError("provider unavailable")

    layout = LocalLayout(tmp_path / "local")
    with pytest.raises(LocalIngestError, match="provider unavailable"):
        asyncio.run(
            run_local_ingest(
                layout=layout,
                session_date=date(2024, 1, 19),
                provider=FailingAlpaca(),
                environ=_credentials(),
                now=datetime(2024, 1, 22, tzinfo=UTC),
            )
        )

    records = LocalCatalogRepository(layout.catalog).list(
        asset_class=AssetClass.EQUITY,
        symbol="AAPL",
    )
    assert len(records) == 2
    assert all(record.status is CoverageStatus.ERROR for record in records)
    assert all(
        "provider unavailable" in (record.error_message or "") for record in records
    )


def test_backfill_fetches_arbitrary_symbol_once_and_resumes(tmp_path) -> None:
    layout = LocalLayout(tmp_path / "local")
    provider = FakeAlpaca()

    first = asyncio.run(
        run_local_backfill(
            layout=layout,
            symbols=["msft"],
            start_date=date(2024, 1, 19),
            end_date=date(2024, 1, 19),
            provider=provider,
            environ=_credentials(),
            now=datetime(2024, 1, 22, tzinfo=UTC),
        )
    )

    assert provider.calls == [("trade", "MSFT"), ("quote", "MSFT")]
    assert first["symbols"] == ["MSFT"]
    assert first["sessions"] == [
        {
            "data_type_rows": {"quote": 1, "trade": 1},
            "session_date": "2024-01-19",
            "statuses": ["complete", "complete"],
            "symbol": "MSFT",
        }
    ]
    records = LocalCatalogRepository(layout.catalog).list(symbol="MSFT")
    assert {record.status for record in records} == {CoverageStatus.COMPLETE}

    # A rerun reads the catalog checkpoint and does not refetch.
    second = asyncio.run(
        run_local_backfill(
            layout=layout,
            symbols=["MSFT"],
            start_date=date(2024, 1, 19),
            end_date=date(2024, 1, 19),
            provider=provider,
            environ=_credentials(),
            now=datetime(2024, 1, 22, tzinfo=UTC),
        )
    )

    assert second["sessions"] == []
    assert len(provider.calls) == 2


def test_truncated_fetch_is_recorded_partial(tmp_path) -> None:
    class TruncatingAlpaca(FakeAlpaca):
        async def fetch_trades(self, symbols, start, end):
            return FetchedRecords(
                await super().fetch_trades(symbols, start, end), truncated=True
            )

    layout = LocalLayout(tmp_path / "local")

    result = asyncio.run(
        run_local_ingest(
            layout=layout,
            symbol="AAPL",
            session_date=date(2024, 1, 19),
            provider=TruncatingAlpaca(),
            environ=_credentials(),
            now=datetime(2024, 1, 22, tzinfo=UTC),
        )
    )

    assert result["coverage"] == [
        {"data_type": "trade", "rows": 1, "status": "partial"},
        {"data_type": "quote", "rows": 1, "status": "complete"},
    ]
    statuses = {
        record.data_type: record.status
        for record in LocalCatalogRepository(layout.catalog).list(symbol="AAPL")
    }
    assert statuses[DataType.TRADE] is CoverageStatus.PARTIAL
    assert statuses[DataType.QUOTE] is CoverageStatus.COMPLETE


def test_local_ensure_launcher_fetches_the_requested_symbol(tmp_path) -> None:
    layout = LocalLayout(tmp_path / "local")
    provider = FakeAlpaca()
    launcher = LocalEnsureLauncher(
        layout, environ=_credentials(), equity_provider=provider
    )
    job = EnsureJob(
        job_id="ensure-test",
        request=EnsureSpec(
            asset_class=AssetClass.EQUITY,
            data_type=DataType.TRADE,
            symbol="GLD",
            start_date=date(2024, 1, 19),
            end_date=date(2024, 1, 19),
        ),
        gaps=(),
    )

    asyncio.run(launcher(job))

    assert provider.calls == [("trade", "GLD"), ("quote", "GLD")]
    records = LocalCatalogRepository(layout.catalog).list(symbol="GLD")
    assert {record.status for record in records} == {CoverageStatus.COMPLETE}
