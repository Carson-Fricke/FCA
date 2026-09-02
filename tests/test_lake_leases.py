from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Event, Lock
from typing import Any, cast

import pytest
from azure.core.exceptions import ResourceExistsError
from azure.storage.blob import ContainerClient

import lake.leases as leases_module
from lake.catalog import CoverageRecord, CoverageStatus
from lake.equity_ingest import AtomicIngestCommitter, StagedParquetObject
from lake.leases import (
    AzureBlobLeaseProvider,
    Lease,
    LeasedAtomicIngestCommitter,
    LeaseProvider,
    SymbolDayScope,
    scope_for_coverage,
)
from lake.partition_paths import AssetClass, DataType


class FakeLease:
    def __init__(self, lock: Lock) -> None:
        self._lock = lock
        self._released = False
        self.renewals = 0

    @property
    def lease_id(self) -> str:
        return "fake-lease"

    def renew(self) -> None:
        self.renewals += 1

    def release(self) -> None:
        if not self._released:
            self._released = True
            self._lock.release()


class FakeLeaseProvider:
    def __init__(self) -> None:
        self.locks: dict[SymbolDayScope, Lock] = {}
        self.acquire_attempted = Event()
        self.acquire_count = 0
        self.scopes: list[SymbolDayScope] = []

    def acquire(self, scope: SymbolDayScope) -> Lease:
        self.acquire_count += 1
        if self.acquire_count > 1:
            self.acquire_attempted.set()
        self.scopes.append(scope)
        lock = self.locks.setdefault(scope, Lock())
        lock.acquire()
        return FakeLease(lock)

    def lock_for(self, scope: SymbolDayScope) -> Lock:
        return self.locks[scope]


class RecordingCommitter:
    def __init__(self) -> None:
        self.entered = Event()
        self.release = Event()
        self._active = 0
        self._active_lock = Lock()
        self.max_active = 0
        self.commits = 0

    def commit(
        self,
        *,
        objects: list[StagedParquetObject],
        coverage: list[CoverageRecord],
    ) -> None:
        assert objects == []
        assert len(coverage) == 2
        with self._active_lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
            self.commits += 1
            self.entered.set()
        try:
            assert self.release.wait(timeout=2)
        finally:
            with self._active_lock:
                self._active -= 1


class FakeBlobLease:
    id = "azure-lease-id"

    def __init__(self) -> None:
        self.renew_calls = 0
        self.release_calls = 0

    def renew(self) -> None:
        self.renew_calls += 1

    def release(self) -> None:
        self.release_calls += 1


class FakeBlob:
    def __init__(self, path: str) -> None:
        self.path = path
        self.exists = False
        self.uploads: list[tuple[bytes, bool]] = []
        self.leases: list[FakeBlobLease] = []
        self.lease_durations: list[int] = []

    def upload_blob(self, payload: bytes, *, overwrite: bool) -> None:
        if self.exists and not overwrite:
            raise ResourceExistsError("lock exists")
        self.exists = True
        self.uploads.append((payload, overwrite))

    def acquire_lease(self, *, lease_duration: int) -> FakeBlobLease:
        self.lease_durations.append(lease_duration)
        lease = FakeBlobLease()
        self.leases.append(lease)
        return lease


class FakeContainer:
    def __init__(self) -> None:
        self.blobs: dict[str, FakeBlob] = {}

    def get_blob_client(self, path: str) -> FakeBlob:
        return self.blobs.setdefault(path, FakeBlob(path))


def _coverage() -> list[CoverageRecord]:
    return [
        CoverageRecord(
            asset_class=AssetClass.EQUITY,
            data_type=data_type,
            symbol="AAPL",
            session_date=date(2024, 1, 19),
            status=CoverageStatus.COMPLETE,
        )
        for data_type in (DataType.TRADE, DataType.QUOTE)
    ]


def test_scope_canonicalizes_symbols_and_rejects_mixed_commits() -> None:
    scope = scope_for_coverage(_coverage())

    assert scope == SymbolDayScope(AssetClass.EQUITY, "aapl", date(2024, 1, 19))
    assert scope.key == "equity/2024-01-19/AAPL"
    assert scope.blob_name == "date=2024-01-19/asset_class=equity/AAPL.lock"

    mixed = _coverage()
    mixed[1] = CoverageRecord(
        asset_class=AssetClass.EQUITY,
        data_type=DataType.QUOTE,
        symbol="MSFT",
        session_date=date(2024, 1, 19),
        status=CoverageStatus.COMPLETE,
    )
    with pytest.raises(ValueError, match="one symbol-day"):
        scope_for_coverage(mixed)


def test_leased_committer_allows_only_one_writer_inside_commit() -> None:
    provider = FakeLeaseProvider()
    committer = RecordingCommitter()
    leased = LeasedAtomicIngestCommitter(
        cast(AtomicIngestCommitter, committer),
        cast(LeaseProvider, provider),
    )
    coverage = _coverage()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(leased.commit, objects=[], coverage=coverage)
        assert committer.entered.wait(timeout=2)
        second = executor.submit(leased.commit, objects=[], coverage=coverage)
        assert provider.acquire_attempted.wait(timeout=2)
        assert not second.done()
        committer.release.set()
        first.result(timeout=2)
        second.result(timeout=2)

    assert committer.commits == 2
    assert committer.max_active == 1
    assert (
        provider.scopes
        == [SymbolDayScope(AssetClass.EQUITY, "AAPL", date(2024, 1, 19))] * 2
    )


def test_leased_committer_releases_before_propagating_commit_failure() -> None:
    provider = FakeLeaseProvider()

    class FailingCommitter:
        def commit(
            self,
            *,
            objects: list[StagedParquetObject],
            coverage: list[CoverageRecord],
        ) -> None:
            raise RuntimeError("commit failed")

    leased = LeasedAtomicIngestCommitter(
        cast(AtomicIngestCommitter, FailingCommitter()),
        cast(LeaseProvider, provider),
    )

    with pytest.raises(RuntimeError, match="commit failed"):
        leased.commit(objects=[], coverage=_coverage())

    scope = SymbolDayScope(AssetClass.EQUITY, "AAPL", date(2024, 1, 19))
    assert provider.lock_for(scope).acquire(blocking=False)
    provider.lock_for(scope).release()


def test_azure_blob_lease_ensures_lock_blob_and_releases_safely() -> None:
    container = FakeContainer()
    provider = AzureBlobLeaseProvider(
        cast(ContainerClient, container), lease_duration_seconds=30
    )
    scope = SymbolDayScope(AssetClass.EQUITY, "AAPL", date(2024, 1, 19))

    lease = provider.acquire(scope)
    lease.renew()
    lease.release()
    lease.release()
    second_lease = provider.acquire(scope)
    second_lease.release()

    blob = container.blobs["locks/date=2024-01-19/asset_class=equity/AAPL.lock"]
    assert blob.uploads == [(b"", False)]
    assert blob.lease_durations == [30, 30]
    assert lease.lease_id == "azure-lease-id"
    assert blob.leases[0].renew_calls == 1
    assert blob.leases[0].release_calls == 1
    assert blob.leases[1].release_calls == 1
    with pytest.raises(RuntimeError, match="released lease"):
        lease.renew()


def test_azure_blob_lease_factory_passes_token_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = object()
    container = FakeContainer()
    calls: list[tuple[str, object]] = []

    class FakeBlobServiceClient:
        def __init__(self, *, account_url: str, credential: object) -> None:
            calls.append((account_url, credential))

        def get_container_client(self, name: str) -> FakeContainer:
            assert name == "lake"
            return container

    monkeypatch.setattr(leases_module, "BlobServiceClient", FakeBlobServiceClient)

    provider = AzureBlobLeaseProvider.from_account_url(
        "https://lakeaccount.blob.core.windows.net",
        "lake",
        credential=cast(Any, credential),
    )
    provider.acquire(SymbolDayScope(AssetClass.EQUITY, "AAPL", date(2024, 1, 19)))

    assert calls == [
        ("https://lakeaccount.blob.core.windows.net", credential),
    ]
