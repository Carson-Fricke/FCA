"""Focused tests for the hosted Azure API composition."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from datetime import UTC, date, datetime
from typing import Any

import pytest

from lake.catalog.azure import AzureTableCatalogRepository, AzureTableJobRepository
from lake.catalog.models import JobRecord, JobState
from lake.contracts import AssetClass, DataType
from lake.coverage.application import EnsureJob, EnsureSpec
from lake.hosted.runtime import (
    HostedEnsureError,
    HostedRuntime,
    hosted_overrides,
)
from lake.http.application import create_app
from lake.http.models import EnsureRequest
from lake.storage.azure import AzureBlobSasIssuer

_ENV = {
    "LAKE_STORAGE_ACCOUNT_NAME": "hostedaccount",
    "AZURE_CLIENT_ID": "00000000-0000-0000-0000-000000000000",
    "LAKE_PROVIDER__API_KEY": "fake-key",
    "LAKE_PROVIDER__API_SECRET": "fake-secret",
}


def _clock() -> datetime:
    return datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def _runtime(**environ: str) -> HostedRuntime:
    return HostedRuntime.from_environment(
        environ={**_ENV, **environ},
        clock=_clock,
    )


def _equity_request(start: str, end: str) -> EnsureRequest:
    return EnsureRequest(
        asset_class=AssetClass.EQUITY,
        data_type=DataType.TRADE,
        symbol="aapl",
        start_date=date.fromisoformat(start),
        end_date=date.fromisoformat(end),
    )


def _job() -> EnsureJob:
    return EnsureJob(
        job_id="ensure-test",
        request=EnsureSpec(
            asset_class=AssetClass.EQUITY,
            data_type=DataType.TRADE,
            symbol="AAPL",
            start_date=date(2026, 8, 28),
            end_date=date(2026, 8, 28),
        ),
        gaps=(),
    )


class _JobRepository:
    def __init__(self) -> None:
        self.records: dict[str, JobRecord] = {}

    def get(self, job_id: str) -> JobRecord | None:
        return self.records.get(job_id)

    def upsert(self, record: JobRecord) -> JobRecord:
        self.records[record.job_id] = record
        return record

    def put(self, record: JobRecord) -> JobRecord:
        return self.upsert(record)

    def list(self, *, state: JobState | None = None) -> tuple[JobRecord, ...]:
        return tuple(
            record
            for record in self.records.values()
            if state is None or record.state == state
        )


def test_hosted_overrides_are_empty_without_storage_environment() -> None:
    assert hosted_overrides(environ={}) == {}


def test_hosted_runtime_builds_azure_boundaries_from_environment() -> None:
    runtime = _runtime()

    assert isinstance(runtime.catalog, AzureTableCatalogRepository)
    assert isinstance(runtime.jobs, AzureTableJobRepository)
    assert isinstance(runtime.sas_issuer, AzureBlobSasIssuer)


def test_hosted_overrides_wire_the_hosted_boundaries() -> None:
    overrides = hosted_overrides(environ=_ENV)

    assert isinstance(overrides["catalog_repository"], AzureTableCatalogRepository)
    assert isinstance(overrides["job_repository"], AzureTableJobRepository)
    assert isinstance(overrides["sas_issuer"], AzureBlobSasIssuer)
    assert callable(overrides["ensure_callback"])
    assert callable(overrides["job_launcher"])


def test_create_app_uses_hosted_boundaries_when_configured() -> None:
    application = create_app(
        bearer_token="test-secret",
        **hosted_overrides(environ=_ENV),
    )

    assert isinstance(
        application.state.catalog_repository,
        AzureTableCatalogRepository,
    )
    assert isinstance(
        application.state.job_repository,
        AzureTableJobRepository,
    )
    assert isinstance(application.state.sas_issuer, AzureBlobSasIssuer)
    assert callable(application.state.ensure_callback)
    assert callable(application.state.job_launcher)


def test_ensure_sessions_ingests_each_closed_equity_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    ingested: list[tuple[str, date]] = []

    async def fake_ingest(symbol: str, sessions: tuple[Any, ...]) -> None:
        for session in sessions:
            ingested.append((symbol, session.session_date))

    monkeypatch.setattr(runtime, "_ingest_sessions", fake_ingest)

    asyncio.run(runtime.ensure_sessions(_equity_request("2026-08-28", "2026-08-31")))

    assert ingested == [
        ("AAPL", date(2026, 8, 28)),
        ("AAPL", date(2026, 8, 31)),
    ]


def test_ensure_sessions_requires_provider_credentials() -> None:
    environ = {
        name: value
        for name, value in _ENV.items()
        if not name.startswith("LAKE_PROVIDER__")
    }
    runtime = HostedRuntime.from_environment(environ=environ, clock=_clock)

    with pytest.raises(HostedEnsureError, match="LAKE_PROVIDER__API_KEY"):
        asyncio.run(
            runtime.ensure_sessions(_equity_request("2026-08-28", "2026-08-28"))
        )


def test_non_equity_ensure_requests_are_rejected() -> None:
    runtime = _runtime()
    request = EnsureRequest(
        asset_class=AssetClass.CRYPTO,
        data_type=DataType.TRADE,
        symbol="BTC/USD",
        start_date=date(2026, 8, 28),
        end_date=date(2026, 8, 28),
    )

    with pytest.raises(HostedEnsureError, match="equity"):
        asyncio.run(runtime.ensure_sessions(request))


def test_job_launcher_marks_the_durable_job_succeeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    jobs = _JobRepository()
    monkeypatch.setattr(runtime, "jobs", jobs)

    async def fake_ingest(symbol: str, sessions: tuple[Any, ...]) -> None:
        del symbol, sessions

    monkeypatch.setattr(runtime, "_ingest_sessions", fake_ingest)

    launch: Callable[[EnsureJob], Coroutine[Any, Any, None]] = runtime.job_launcher()
    asyncio.run(launch(_job()))

    assert jobs.records["ensure-test"].state is JobState.SUCCEEDED


def test_job_launcher_propagates_ingest_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _runtime()
    jobs = _JobRepository()
    monkeypatch.setattr(runtime, "jobs", jobs)

    async def failing_ingest(symbol: str, sessions: tuple[Any, ...]) -> None:
        del symbol, sessions
        raise HostedEnsureError("provider unavailable")

    monkeypatch.setattr(runtime, "_ingest_sessions", failing_ingest)

    launch: Callable[[EnsureJob], Coroutine[Any, Any, None]] = runtime.job_launcher()
    with pytest.raises(HostedEnsureError, match="provider unavailable"):
        asyncio.run(launch(_job()))

    assert jobs.records == {}
