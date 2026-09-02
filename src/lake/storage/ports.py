"""Provider-independent storage ports."""

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from lake.catalog.ports import JobRepository
from lake.storage.parquet import ObjectStore


@runtime_checkable
class BlobStore(ObjectStore, Protocol):
    """Whole-object replacement boundary used by the Parquet writer."""


@runtime_checkable
class QueueSender(Protocol):
    def send(self, message: Mapping[str, object]) -> None: ...


@runtime_checkable
class SasIssuer(Protocol):
    def issue(self, object_path: str) -> str: ...


@runtime_checkable
class JobStorage(JobRepository, Protocol):
    """Job persistence boundary shared by memory and Azure implementations."""


__all__ = ["BlobStore", "JobStorage", "QueueSender", "SasIssuer"]
