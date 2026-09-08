"""Token-authenticated Azure storage adapters for the market lake.

Each adapter accepts an Azure SDK client or can construct one from an endpoint
and a caller-supplied :class:`~azure.core.credentials.TokenCredential`.  The
small interfaces keep storage wiring testable without requiring Azure access.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Self
from urllib.parse import quote

from azure.core.credentials import TokenCredential
from azure.core.exceptions import ResourceNotFoundError
from azure.data.tables import TableClient
from azure.storage.blob import (
    BlobSasPermissions,
    BlobServiceClient,
    ContainerClient,
    generate_blob_sas,
)
from azure.storage.queue import QueueClient

from lake.catalog.azure import AzureTableJobRepository
from lake.catalog.models import JobRecord, JobState
from lake.catalog.ports import JobRepository
from lake.storage.ports import BlobStore, JobStorage, QueueSender, SasIssuer


def _object_path(object_path: str) -> str:
    if (
        not object_path
        or "\\" in object_path
        or object_path.startswith("/")
        or any(part in {"", ".", ".."} for part in object_path.split("/"))
    ):
        raise ValueError("object path must be a non-empty relative POSIX path")
    path = PurePosixPath(object_path)
    if path.is_absolute() or not path.parts:
        raise ValueError("object path must be relative")
    return object_path


class AzureBlobStore:
    """Blob replacement adapter backed by a caller-configured container."""

    def __init__(self, container: ContainerClient) -> None:
        self._container = container

    @classmethod
    def from_account_url(
        cls,
        account_url: str,
        container_name: str,
        *,
        credential: TokenCredential,
    ) -> Self:
        """Create a blob store with the supplied token credential."""
        service = BlobServiceClient(account_url=account_url, credential=credential)
        return cls(service.get_container_client(container_name))

    def replace(self, relative_path: str, payload: bytes) -> None:
        """Replace one blob in full, without exposing a read operation."""
        path = _object_path(relative_path)
        self._container.get_blob_client(path).upload_blob(payload, overwrite=True)


class AzureReadableBlobStore(AzureBlobStore):
    """Blob adapter that also reads, lists, and deletes whole prefixes.

    Backs consumers owning entire object prefixes, such as the backtest
    results store: the replace-only :class:`AzureBlobStore` stays read-free
    for the Parquet writer, while this subclass adds the read, prefix-list,
    and prefix-delete surface required to fetch and clean up run artifacts.
    """

    def read(self, relative_path: str) -> bytes:
        """Return one blob's payload."""
        path = _object_path(relative_path)
        try:
            return self._container.download_blob(path).readall()
        except ResourceNotFoundError:
            raise KeyError(f"object not found: {path}") from None

    def list_prefix(self, prefix: str) -> tuple[str, ...]:
        """Return blob names below ``prefix`` in sorted order."""
        return tuple(
            sorted(
                blob.name
                for blob in self._container.list_blobs(name_starts_with=prefix)
            )
        )

    def delete_prefix(self, prefix: str) -> int:
        """Delete every blob below ``prefix`` and return how many went away."""
        names = [
            blob.name for blob in self._container.list_blobs(name_starts_with=prefix)
        ]
        for name in names:
            self._container.delete_blob(name)
        return len(names)


class AzureQueueSender:
    """JSON message sender backed by one Azure Storage Queue."""

    def __init__(self, queue: QueueClient) -> None:
        self._queue = queue

    @classmethod
    def from_account_url(
        cls,
        account_url: str,
        queue_name: str,
        *,
        credential: TokenCredential,
    ) -> AzureQueueSender:
        """Create a queue sender with the supplied token credential."""
        return cls(
            QueueClient(
                account_url=account_url,
                queue_name=queue_name,
                credential=credential,
            )
        )

    def send(self, message: Mapping[str, object]) -> None:
        """Serialize and send a deterministic JSON message."""
        body = json.dumps(
            dict(message),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        self._queue.send_message(body)


class AzureBlobSasIssuer:
    """Issue read-only user-delegation SAS URLs for individual blob objects."""

    _MAX_TTL_SECONDS = 86_400

    def __init__(
        self,
        service: BlobServiceClient,
        container_name: str,
        *,
        ttl_seconds: int = 900,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not 1 <= ttl_seconds <= self._MAX_TTL_SECONDS:
            raise ValueError("SAS TTL must be between 1 and 86400 seconds")
        if not container_name:
            raise ValueError("container name must not be empty")
        self._service = service
        self._container_name = container_name
        self._ttl = timedelta(seconds=ttl_seconds)
        self._clock = clock or (lambda: datetime.now(UTC))

    @classmethod
    def from_account_url(
        cls,
        account_url: str,
        container_name: str,
        *,
        credential: TokenCredential,
        ttl_seconds: int = 900,
        clock: Callable[[], datetime] | None = None,
    ) -> AzureBlobSasIssuer:
        """Create an issuer whose delegation key is obtained with a token."""
        service = BlobServiceClient(account_url=account_url, credential=credential)
        return cls(
            service,
            container_name,
            ttl_seconds=ttl_seconds,
            clock=clock,
        )

    def issue(self, object_path: str) -> str:
        """Issue a read-only SAS URL scoped to exactly one blob path."""
        path = _object_path(object_path)
        issued_at = self._utc_now()
        expires_at = issued_at + self._ttl
        delegation_key = self._service.get_user_delegation_key(
            issued_at,
            expires_at,
        )
        account_name = self._service.account_name
        if not account_name:
            raise ValueError("blob service URL must contain an account name")
        token = generate_blob_sas(
            account_name=account_name,
            container_name=self._container_name,
            blob_name=path,
            user_delegation_key=delegation_key,
            permission=BlobSasPermissions(read=True),
            start=issued_at,
            expiry=expires_at,
        )
        return self._url(path, token)

    def _utc_now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("SAS clock must return a timezone-aware datetime")
        return value.astimezone(UTC)

    def _url(self, object_path: str, token: str) -> str:
        container = quote(self._container_name, safe="-._~")
        blob = quote(object_path, safe="/-._~=")
        return f"{self._service.url.rstrip('/')}/{container}/{blob}?{token}"


class AzureJobStorage:
    """Job storage facade over the T024 Azure Table repository adapter."""

    def __init__(self, table: TableClient) -> None:
        self._repository: JobRepository = AzureTableJobRepository(table)

    @classmethod
    def from_endpoint(
        cls,
        table_endpoint: str,
        table_name: str,
        *,
        credential: TokenCredential,
    ) -> AzureJobStorage:
        """Create job storage with the supplied token credential."""
        table = TableClient(
            endpoint=table_endpoint,
            table_name=table_name,
            credential=credential,
        )
        return cls(table)

    def get(self, job_id: str) -> JobRecord | None:
        return self._repository.get(job_id)

    def upsert(self, record: JobRecord) -> JobRecord:
        return self._repository.upsert(record)

    def list(self, *, state: JobState | None = None) -> tuple[JobRecord, ...]:
        return self._repository.list(state=state)


AzureBlobObjectStore = AzureBlobStore
AzureQueueMessageSender = AzureQueueSender
AzureJobStore = AzureJobStorage


def as_job_storage(repository: JobRepository) -> JobStorage:
    """Adapt an existing job repository for dependency-injected callers."""
    return repository


__all__ = [
    "AzureBlobObjectStore",
    "AzureBlobSasIssuer",
    "AzureBlobStore",
    "AzureJobStorage",
    "AzureJobStore",
    "AzureQueueMessageSender",
    "AzureQueueSender",
    "AzureReadableBlobStore",
    "BlobStore",
    "JobStorage",
    "QueueSender",
    "SasIssuer",
    "as_job_storage",
]
