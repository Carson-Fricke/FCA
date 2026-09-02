import json
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from azure.core.exceptions import ResourceNotFoundError
from azure.data.tables import TableClient, TableEntity, UpdateMode
from azure.storage.blob import BlobSasPermissions

import lake.storage as storage_module
from lake.catalog import JobRecord, JobState
from lake.storage import (
    AzureBlobSasIssuer,
    AzureBlobStore,
    AzureJobStorage,
    AzureQueueSender,
    BlobStore,
    JobStorage,
    QueueSender,
    SasIssuer,
)


class FakeCredential:
    pass


class FakeBlob:
    def __init__(self, path: str) -> None:
        self.path = path
        self.uploads: list[tuple[bytes, bool]] = []

    def upload_blob(self, payload: bytes, *, overwrite: bool) -> None:
        self.uploads.append((payload, overwrite))


class FakeContainer:
    def __init__(self) -> None:
        self.blobs: dict[str, FakeBlob] = {}

    def get_blob_client(self, path: str) -> FakeBlob:
        return self.blobs.setdefault(path, FakeBlob(path))


class FakeBlobService:
    account_name = "lakeaccount"
    url = "https://lakeaccount.blob.core.windows.net/"

    def __init__(self) -> None:
        self.delegation_calls: list[tuple[datetime, datetime]] = []

    def get_container_client(self, name: str) -> FakeContainer:
        assert name == "lake"
        return FakeContainer()

    def get_user_delegation_key(self, start: datetime, expiry: datetime) -> object:
        self.delegation_calls.append((start, expiry))
        return object()


class FakeQueue:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def send_message(self, content: str) -> None:
        self.messages.append(content)


class FakeTable:
    def __init__(self) -> None:
        self.entities: dict[tuple[str, str], TableEntity] = {}

    def get_entity(self, partition_key: str, row_key: str) -> TableEntity:
        try:
            return self.entities[(partition_key, row_key)]
        except KeyError:
            raise ResourceNotFoundError("missing") from None

    def upsert_entity(self, entity: TableEntity, mode: UpdateMode) -> None:
        assert mode is UpdateMode.REPLACE
        self.entities[(entity["PartitionKey"], entity["RowKey"])] = TableEntity(entity)

    def query_entities(
        self,
        query_filter: str,
        *,
        parameters: dict[str, Any] | None = None,
    ) -> list[TableEntity]:
        assert query_filter == "PartitionKey eq @partition"
        assert parameters == {"partition": "jobs"}
        return list(self.entities.values())


def _job() -> JobRecord:
    timestamp = datetime(2024, 1, 19, 12, tzinfo=UTC)
    return JobRecord(
        job_id="job-1",
        state=JobState.QUEUED,
        created_at=timestamp,
        updated_at=timestamp,
    )


def test_blob_store_replaces_objects_without_exposing_reads() -> None:
    container = FakeContainer()
    adapter = AzureBlobStore(cast(Any, container))

    assert isinstance(adapter, BlobStore)
    adapter.replace("date=2024-01-19/AAPL.parquet", b"new")
    adapter.replace("date=2024-01-19/AAPL.parquet", b"replacement")

    blob = container.blobs["date=2024-01-19/AAPL.parquet"]
    assert blob.uploads == [(b"new", True), (b"replacement", True)]
    assert not hasattr(adapter, "read")


def test_blob_store_rejects_paths_that_are_not_relative_objects() -> None:
    adapter = AzureBlobStore(cast(Any, FakeContainer()))

    for path in ("", "/lake/file", "../file", "lake/../file", "lake\\file"):
        with pytest.raises(ValueError, match="relative POSIX"):
            adapter.replace(path, b"payload")


def test_blob_factory_passes_caller_credential_to_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = FakeCredential()
    service = FakeBlobService()
    calls: list[tuple[str, object]] = []

    class FakeBlobServiceClient:
        def __init__(self, *, account_url: str, credential: object) -> None:
            calls.append((account_url, credential))

        def get_container_client(self, name: str) -> FakeContainer:
            return service.get_container_client(name)

    monkeypatch.setattr(storage_module, "BlobServiceClient", FakeBlobServiceClient)

    adapter = AzureBlobStore.from_account_url(
        "https://lakeaccount.blob.core.windows.net",
        "lake",
        credential=cast(Any, credential),
    )
    adapter.replace("file.parquet", b"payload")

    assert calls == [("https://lakeaccount.blob.core.windows.net", credential)]


def test_queue_sender_serializes_json_and_only_sends() -> None:
    queue = FakeQueue()
    sender = AzureQueueSender(cast(Any, queue))

    assert isinstance(sender, QueueSender)
    sender.send({"symbol": "AAPL", "session_date": "2024-01-19"})

    assert queue.messages == ['{"session_date":"2024-01-19","symbol":"AAPL"}']
    assert not hasattr(sender, "receive")


def test_queue_factory_passes_caller_credential_to_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = FakeCredential()
    calls: list[tuple[str, str, object]] = []

    class FakeQueueClient:
        def __init__(
            self, *, account_url: str, queue_name: str, credential: object
        ) -> None:
            calls.append((account_url, queue_name, credential))

        def send_message(self, content: str) -> None:
            assert json.loads(content)["job_id"] == "job-1"

    monkeypatch.setattr(storage_module, "QueueClient", FakeQueueClient)
    sender = AzureQueueSender.from_account_url(
        "https://lakeaccount.queue.core.windows.net",
        "backfill",
        credential=cast(Any, credential),
    )
    sender.send({"job_id": "job-1"})

    assert calls == [
        ("https://lakeaccount.queue.core.windows.net", "backfill", credential)
    ]


def test_sas_issuer_uses_user_delegation_and_one_read_only_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakeBlobService()
    captured: dict[str, object] = {}
    issued_at = datetime(2024, 1, 19, 12, tzinfo=UTC)

    def fake_generate_blob_sas(**kwargs: object) -> str:
        captured.update(kwargs)
        return "sv=fake-signature"

    monkeypatch.setattr(storage_module, "generate_blob_sas", fake_generate_blob_sas)
    issuer = AzureBlobSasIssuer(
        cast(Any, service),
        "lake",
        ttl_seconds=900,
        clock=lambda: issued_at,
    )

    assert isinstance(issuer, SasIssuer)
    result = issuer.issue("date=2024-01-19/AAPL.parquet")

    assert result == (
        "https://lakeaccount.blob.core.windows.net/lake/"
        "date=2024-01-19/AAPL.parquet?sv=fake-signature"
    )
    assert service.delegation_calls == [
        (issued_at, datetime(2024, 1, 19, 12, 15, tzinfo=UTC))
    ]
    assert captured["account_name"] == "lakeaccount"
    assert captured["container_name"] == "lake"
    assert captured["blob_name"] == "date=2024-01-19/AAPL.parquet"
    assert captured["user_delegation_key"] is not None
    assert captured["start"] == issued_at
    assert captured["expiry"] == datetime(2024, 1, 19, 12, 15, tzinfo=UTC)
    permission = cast(BlobSasPermissions, captured["permission"])
    assert permission.read is True
    assert permission.write is False
    assert permission.delete is False
    assert permission.add is False
    assert permission.create is False
    assert permission.delete_previous_version is False
    assert permission.tag is False


def test_sas_issuer_rejects_long_ttl_and_invalid_object() -> None:
    with pytest.raises(ValueError, match="between 1 and 86400"):
        AzureBlobSasIssuer(cast(Any, FakeBlobService()), "lake", ttl_seconds=86401)

    issuer = AzureBlobSasIssuer(cast(Any, FakeBlobService()), "lake")
    with pytest.raises(ValueError, match="relative POSIX"):
        issuer.issue("../outside.parquet")


def test_job_storage_delegates_to_the_t024_repository_contract() -> None:
    table = FakeTable()
    jobs: JobStorage = AzureJobStorage(cast(TableClient, table))
    record = _job()

    assert jobs.upsert(record) == record
    assert jobs.get(record.job_id) == record
    assert jobs.list(state=JobState.QUEUED) == (record,)


def test_job_factory_passes_caller_credential_to_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    credential = FakeCredential()
    calls: list[tuple[str, str, object]] = []

    class FakeTableClient:
        def __init__(
            self, *, endpoint: str, table_name: str, credential: object
        ) -> None:
            calls.append((endpoint, table_name, credential))

    monkeypatch.setattr(storage_module, "TableClient", FakeTableClient)
    AzureJobStorage.from_endpoint(
        "https://lakeaccount.table.core.windows.net",
        "jobs",
        credential=cast(Any, credential),
    )

    assert calls == [("https://lakeaccount.table.core.windows.net", "jobs", credential)]
