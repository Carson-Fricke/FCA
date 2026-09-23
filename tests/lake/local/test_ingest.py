import asyncio
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pyarrow.parquet as pq
import pytest

from lake.catalog.models import CoverageStatus
from lake.contracts import AssetClass, DataType
from lake.coverage.application import EnsureJob, EnsureSpec
from lake.local import LocalCatalogRepository, LocalIngestError, LocalLayout
from lake.local.ensure import LocalEnsureLauncher
from lake.local.ingest import (
    run_local_backfill,
    run_local_crypto_backfill,
    run_local_ingest,
)
from lake.providers.alpaca.client import AlpacaClient
from lake.providers.alpaca.records import (
    NormalizedCryptoQuote,
    NormalizedCryptoTrade,
    NormalizedQuote,
    NormalizedTrade,
)


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
            "data_type_rows": {"trade": 1, "quote": 1},
            "error": None,
            "retryable": False,
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


def test_coverage_status_is_partial_only_for_an_open_session() -> None:
    from lake.ingestion.equities.transaction import _coverage_status

    # A completed fetch of a closed session is complete however large it was;
    # only a session that is still trading is partial.
    assert _coverage_status(True, 10) is CoverageStatus.PARTIAL
    assert _coverage_status(False, 10) is CoverageStatus.COMPLETE
    assert _coverage_status(False, 0) is CoverageStatus.EMPTY


def test_local_ensure_launcher_fetches_only_the_requested_data_type(tmp_path) -> None:
    # One ensure asks for one data type, and only that dataset is fetched.
    # The harness issues a separate ensure per data type, so fetching both here
    # made the pair fetch the whole session twice.
    layout = LocalLayout(tmp_path / "local")
    provider = FakeAlpaca()
    launcher = LocalEnsureLauncher(
        layout, environ=_credentials(), equity_provider=provider
    )

    def job(data_type: DataType) -> EnsureJob:
        return EnsureJob(
            job_id=f"ensure-{data_type.value}",
            request=EnsureSpec(
                asset_class=AssetClass.EQUITY,
                data_type=data_type,
                symbol="GLD",
                start_date=date(2024, 1, 19),
                end_date=date(2024, 1, 19),
            ),
            gaps=(),
        )

    asyncio.run(launcher(job(DataType.TRADE)))
    assert provider.calls == [("trade", "GLD")]
    records = LocalCatalogRepository(layout.catalog).list(symbol="GLD")
    assert {record.data_type for record in records} == {DataType.TRADE}
    assert {record.status for record in records} == {CoverageStatus.COMPLETE}

    # Terminality is judged against the requested datasets only: quotes are
    # still missing, yet re-ensuring trades must not refetch them.
    provider.calls.clear()
    asyncio.run(launcher(job(DataType.TRADE)))
    assert provider.calls == []

    # The quote ensure then fills the other half without redoing trades.
    provider.calls.clear()
    asyncio.run(launcher(job(DataType.QUOTE)))
    assert provider.calls == [("quote", "GLD")]
    records = LocalCatalogRepository(layout.catalog).list(symbol="GLD")
    assert {record.data_type for record in records} == {
        DataType.TRADE,
        DataType.QUOTE,
    }
    assert {record.status for record in records} == {CoverageStatus.COMPLETE}


class SessionAlpaca:
    """Return one valid trade and quote per requested session window."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, date]] = []

    async def fetch_trades(
        self,
        symbols: str,
        start: date | datetime | str,
        end: date | datetime | str,
    ) -> Sequence[NormalizedTrade]:
        del end
        assert isinstance(start, datetime)
        self.calls.append(("trade", symbols, start.date()))
        return (
            NormalizedTrade(
                timestamp=start + timedelta(minutes=1),
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
        del end
        assert isinstance(start, datetime)
        self.calls.append(("quote", symbols, start.date()))
        return (
            NormalizedQuote(
                timestamp=start + timedelta(minutes=1),
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


class PoisonedSessionAlpaca(SessionAlpaca):
    """Fail one session exactly like a poisoned provider window would."""

    def __init__(self, poisoned: date) -> None:
        super().__init__()
        self.poisoned = poisoned

    async def fetch_trades(
        self,
        symbols: str,
        start: date | datetime | str,
        end: date | datetime | str,
    ) -> Sequence[NormalizedTrade]:
        assert isinstance(start, datetime)
        if start.date() == self.poisoned:
            raise RuntimeError("poisoned session")
        return await super().fetch_trades(symbols, start, end)


def _backfill_two_sessions(provider: object, tmp_path) -> dict[str, object]:
    return asyncio.run(
        run_local_backfill(
            layout=LocalLayout(tmp_path / "local"),
            symbols=["MSFT"],
            start_date=date(2024, 1, 18),
            end_date=date(2024, 1, 19),
            provider=provider,  # type: ignore[arg-type]
            environ=_credentials(),
            now=datetime(2024, 1, 22, tzinfo=UTC),
        )
    )


def test_backfill_survives_a_poisoned_session_and_surfaces_the_error(tmp_path) -> None:
    provider = PoisonedSessionAlpaca(poisoned=date(2024, 1, 19))

    result = _backfill_two_sessions(provider, tmp_path)

    # The fetchable session still ingested and the poisoned one is reported.
    assert [entry["session_date"] for entry in result["sessions"]] == [
        "2024-01-18",
        "2024-01-19",
    ]
    healthy, poisoned = result["sessions"]
    assert healthy["statuses"] == ["complete", "complete"]
    assert healthy["error"] is None
    assert poisoned["statuses"] == ["error", "error"]
    assert poisoned["error"] == "trade: RuntimeError: poisoned session"
    assert result["errors"] == [poisoned]

    records = LocalCatalogRepository(LocalLayout(tmp_path / "local").catalog).list(
        symbol="MSFT"
    )
    assert {(record.session_date.isoformat(), record.status) for record in records} == {
        ("2024-01-18", CoverageStatus.COMPLETE),
        ("2024-01-19", CoverageStatus.ERROR),
    }
    poisoned_rows = [
        record for record in records if record.session_date.isoformat() == "2024-01-19"
    ]
    assert all(
        "poisoned session" in (record.error_message or "") for record in poisoned_rows
    )

    # Error coverage stays non-terminal: a rerun retries only that session,
    # and with a healthy provider it converges instead of re-failing forever.
    healed = _backfill_two_sessions(SessionAlpaca(), tmp_path)
    assert [entry["session_date"] for entry in healed["sessions"]] == ["2024-01-19"]
    assert [entry["statuses"] for entry in healed["sessions"]] == [
        ["complete", "complete"]
    ]
    healed_records = LocalCatalogRepository(
        LocalLayout(tmp_path / "local").catalog
    ).list(symbol="MSFT")
    assert {record.status for record in healed_records} == {CoverageStatus.COMPLETE}


def test_backfill_raises_only_when_nothing_could_be_processed(tmp_path) -> None:
    class AlwaysPoisoned(SessionAlpaca):
        async def fetch_trades(
            self,
            symbols: str,
            start: date | datetime | str,
            end: date | datetime | str,
        ) -> Sequence[NormalizedTrade]:
            del symbols, start, end
            raise RuntimeError("provider unavailable")

    with pytest.raises(LocalIngestError, match="provider unavailable"):
        _backfill_two_sessions(AlwaysPoisoned(), tmp_path)

    records = LocalCatalogRepository(LocalLayout(tmp_path / "local").catalog).list(
        symbol="MSFT"
    )
    assert {record.status for record in records} == {CoverageStatus.ERROR}


def test_backfill_runs_sessions_concurrently(tmp_path) -> None:
    in_flight = {"now": 0, "max": 0}

    class SlowAlpaca(SessionAlpaca):
        async def fetch_trades(
            self,
            symbols: str,
            start: date | datetime | str,
            end: date | datetime | str,
        ) -> Sequence[NormalizedTrade]:
            in_flight["now"] += 1
            in_flight["max"] = max(in_flight["max"], in_flight["now"])
            try:
                await asyncio.sleep(0.05)
                return await super().fetch_trades(symbols, start, end)
            finally:
                in_flight["now"] -= 1

    result = _backfill_two_sessions(SlowAlpaca(), tmp_path)

    assert len(result["sessions"]) == 2
    assert in_flight["max"] > 1


def test_backfill_batches_catalog_persistence(tmp_path, monkeypatch) -> None:
    # Sessions no longer each rewrite the whole catalog JSON: rows buffer and
    # flush in batches, so a two-session backfill performs one catalog update.
    monkeypatch.setattr("lake.local.ingest._CATALOG_FLUSH_RECORDS", 64)
    layout = LocalLayout(tmp_path / "local")
    upserts = {"count": 0}
    original = LocalCatalogRepository.upsert_many

    def counting_upsert(self, records):  # type: ignore[no-untyped-def]
        upserts["count"] += 1
        return original(self, records)

    monkeypatch.setattr(LocalCatalogRepository, "upsert_many", counting_upsert)

    result = _backfill_two_sessions(SessionAlpaca(), tmp_path)

    assert [entry["statuses"] for entry in result["sessions"]] == [
        ["complete", "complete"],
        ["complete", "complete"],
    ]
    assert upserts["count"] == 1
    records = LocalCatalogRepository(layout.catalog).list(symbol="MSFT")
    assert len(records) == 4
    assert {record.status for record in records} == {CoverageStatus.COMPLETE}


class SessionCrypto:
    """Return one valid crypto trade and quote per requested UTC day.

    The signatures deliberately carry no limit parameter: the ensure adapter
    must request a window only and never leak a record limit into the client.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, date]] = []

    async def fetch_crypto_trades(
        self,
        pairs: str,
        start: date | datetime | str,
        end: date | datetime | str,
    ) -> Sequence[NormalizedCryptoTrade]:
        del end
        assert isinstance(start, datetime)
        self.calls.append(("trade", pairs, start.date()))
        return (
            NormalizedCryptoTrade(
                timestamp=start + timedelta(minutes=1),
                symbol=pairs,
                trade_id=1,
                exchange="CBSE",
                price=Decimal("42000.5"),
                size=Decimal("0.5"),
                taker_side="B",
            ),
        )

    async def fetch_crypto_quotes(
        self,
        pairs: str,
        start: date | datetime | str,
        end: date | datetime | str,
    ) -> Sequence[NormalizedCryptoQuote]:
        del end
        assert isinstance(start, datetime)
        self.calls.append(("quote", pairs, start.date()))
        return (
            NormalizedCryptoQuote(
                timestamp=start + timedelta(minutes=1),
                symbol=pairs,
                bid_exchange="CBSE",
                bid_price=Decimal("42000.25"),
                bid_size=Decimal("0.5"),
                ask_exchange="CBSE",
                ask_price=Decimal("42001"),
                ask_size=Decimal("0.5"),
            ),
        )


class PoisonedSessionCrypto(SessionCrypto):
    def __init__(self, poisoned: date) -> None:
        super().__init__()
        self.poisoned = poisoned

    async def fetch_crypto_trades(
        self,
        pairs: str,
        start: date | datetime | str,
        end: date | datetime | str,
    ) -> Sequence[NormalizedCryptoTrade]:
        assert isinstance(start, datetime)
        if start.date() == self.poisoned:
            raise RuntimeError("poisoned day")
        return await super().fetch_crypto_trades(pairs, start, end)


def test_crypto_backfill_survives_a_poisoned_day_and_never_passes_a_limit(
    tmp_path,
) -> None:
    layout = LocalLayout(tmp_path / "local")
    provider = PoisonedSessionCrypto(poisoned=date(2024, 1, 2))

    result = asyncio.run(
        run_local_crypto_backfill(
            layout=layout,
            pairs=["BTC/USD"],
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 2),
            provider=provider,  # type: ignore[arg-type]
            environ=_credentials(),
            now=datetime(2024, 1, 3, 12, tzinfo=UTC),
        )
    )

    healthy, poisoned = result["sessions"]
    assert healthy["statuses"] == ["complete", "complete"]
    assert healthy["error"] is None
    assert poisoned["statuses"] == ["error", "error"]
    assert poisoned["error"] == "trade: RuntimeError: poisoned day"
    assert result["errors"] == [poisoned]
    records = LocalCatalogRepository(layout.catalog).list(
        asset_class=AssetClass.CRYPTO, symbol="BTC/USD"
    )
    assert {(record.session_date.isoformat(), record.status) for record in records} == {
        ("2024-01-01", CoverageStatus.COMPLETE),
        ("2024-01-02", CoverageStatus.ERROR),
    }


class FakeResponse:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.status_code = 200
        self.headers: dict[str, str] = {}

    def json(self) -> object:
        return self.payload


class CryptoPageTransport:
    """Serve BTC/USD pages keyed by endpoint, window start, and page token."""

    def __init__(self, pages: dict[tuple[str, str, str | None], object]) -> None:
        self.pages = pages
        self.calls: list[dict[str, object]] = []

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str | int],
        headers: dict[str, str],
        timeout: float,
    ) -> FakeResponse:
        del method, headers, timeout
        self.calls.append({"url": url, "params": dict(params)})
        key = (url, str(params["start"]), params.get("page_token"))
        return FakeResponse(self.pages[key])  # type: ignore[index]


def test_crypto_ensure_paginates_within_the_provider_limit_bounds(tmp_path) -> None:
    # The ensure path passes windows only: the client owns page sizing, so a
    # window larger than one page paginates and every request keeps the limit
    # inside Alpaca's 1..10000 bound (the falsify-execution regression).
    trades_url = "https://data.alpaca.markets/v1beta3/crypto/us/trades"
    quotes_url = "https://data.alpaca.markets/v1beta3/crypto/us/quotes"
    transport = CryptoPageTransport(
        {
            (trades_url, "2024-01-01T00:00:00Z", None): {
                "trades": {
                    "BTC/USD": [{"t": "2024-01-01T00:01:00Z", "p": "42000", "s": "0.5"}]
                },
                "next_page_token": "p1",
            },
            (trades_url, "2024-01-01T00:00:00Z", "p1"): {
                "trades": {
                    "BTC/USD": [
                        {"t": "2024-01-01T00:02:00Z", "p": "42001", "s": "0.25"}
                    ]
                },
                "next_page_token": None,
            },
            (quotes_url, "2024-01-01T00:00:00Z", None): {
                "quotes": {
                    "BTC/USD": [
                        {
                            "t": "2024-01-01T00:01:00Z",
                            "bp": "41999",
                            "bs": "0.5",
                            "ap": "42001",
                            "as": "0.5",
                        }
                    ]
                },
                "next_page_token": None,
            },
            (trades_url, "2024-01-02T00:00:00Z", None): {
                "trades": {
                    "BTC/USD": [{"t": "2024-01-02T00:01:00Z", "p": "42002", "s": "0.5"}]
                },
                "next_page_token": "p2",
            },
            (trades_url, "2024-01-02T00:00:00Z", "p2"): {
                "trades": {
                    "BTC/USD": [
                        {"t": "2024-01-02T00:02:00Z", "p": "42003", "s": "0.25"}
                    ]
                },
                "next_page_token": None,
            },
            (quotes_url, "2024-01-02T00:00:00Z", None): {
                "quotes": {
                    "BTC/USD": [
                        {
                            "t": "2024-01-02T00:01:00Z",
                            "bp": "42001",
                            "bs": "0.5",
                            "ap": "42003",
                            "as": "0.5",
                        }
                    ]
                },
                "next_page_token": None,
            },
        }
    )

    result = asyncio.run(
        run_local_crypto_backfill(
            layout=LocalLayout(tmp_path / "local"),
            pairs=["BTC/USD"],
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 2),
            provider=AlpacaClient(transport=transport),  # type: ignore[arg-type]
            environ=_credentials(),
            now=datetime(2024, 1, 3, 12, tzinfo=UTC),
        )
    )

    assert result["errors"] == []
    assert len(transport.calls) == 6
    for call in transport.calls:
        limit = call["params"]["limit"]
        assert isinstance(limit, int) and not isinstance(limit, bool)
        assert 1 <= limit <= 10_000
    records = LocalCatalogRepository(LocalLayout(tmp_path / "local").catalog).list(
        asset_class=AssetClass.CRYPTO, symbol="BTC/USD"
    )
    assert {record.status for record in records} == {CoverageStatus.COMPLETE}


class BlipAlpaca(SessionAlpaca):
    def __init__(
        self,
        blipped: date,
        *,
        failures: int,
        error: Exception | None = None,
    ) -> None:
        super().__init__()
        self.blipped = blipped
        self.failures = failures
        self.error = error or httpx.ConnectError("temporary DNS failure")
        self.trade_calls: list[date] = []

    async def fetch_trades(
        self,
        symbols: str,
        start: date | datetime | str,
        end: date | datetime | str,
    ) -> Sequence[NormalizedTrade]:
        assert isinstance(start, datetime)
        day = start.date()
        self.trade_calls.append(day)
        if day == self.blipped and self.failures > 0:
            self.failures -= 1
            raise self.error
        return await super().fetch_trades(symbols, start, end)


def test_backfill_retries_transiently_failed_sessions_in_run(tmp_path, monkeypatch):
    monkeypatch.setattr("lake.local.ingest._RETRY_SETTLE_SECONDS", 0)
    monkeypatch.setattr("lake.ingestion.common.TRANSIENT_RETRY_BASE_DELAY_SECONDS", 0.0)
    provider = BlipAlpaca(date(2024, 1, 19), failures=3)

    result = _backfill_two_sessions(provider, tmp_path)

    assert result["errors"] == []
    assert [entry["session_date"] for entry in result["sessions"]] == [
        "2024-01-18",
        "2024-01-19",
    ]
    assert [entry["statuses"] for entry in result["sessions"]] == [
        ["complete", "complete"],
        ["complete", "complete"],
    ]
    assert all(entry["retryable"] is False for entry in result["sessions"])
    assert provider.trade_calls.count(date(2024, 1, 19)) == 4
    assert provider.trade_calls.count(date(2024, 1, 18)) == 1

    records = LocalCatalogRepository(LocalLayout(tmp_path / "local").catalog).list(
        symbol="MSFT"
    )
    assert {record.status for record in records} == {CoverageStatus.COMPLETE}


def test_backfill_does_not_retry_permanent_session_failures(tmp_path, monkeypatch):
    monkeypatch.setattr("lake.local.ingest._RETRY_SETTLE_SECONDS", 0)
    provider = BlipAlpaca(
        date(2024, 1, 19),
        failures=1,
        error=RuntimeError("poisoned session"),
    )

    result = _backfill_two_sessions(provider, tmp_path)

    healthy, poisoned = result["sessions"]
    assert healthy["statuses"] == ["complete", "complete"]
    assert poisoned["statuses"] == ["error", "error"]
    assert poisoned["error"] == "trade: RuntimeError: poisoned session"
    assert poisoned["retryable"] is False
    assert result["errors"] == [poisoned]
    assert provider.trade_calls.count(date(2024, 1, 19)) == 1
