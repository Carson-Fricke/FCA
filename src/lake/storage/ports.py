"""Provider-independent storage ports."""

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from lake.catalog.ports import JobRepository
from lake.storage.parquet import ObjectStore


@runtime_checkable
class BlobStore(ObjectStore, Protocol):
    """Whole-object replacement boundary used by the Parquet writer."""


@runtime_checkable
class ReadableBlobStore(BlobStore, Protocol):
    """Blob boundary that also owns whole prefixes.

    Extends the replace-only :class:`BlobStore` used by the Parquet writer
    with the read, prefix-list, and prefix-delete operations required by
    consumers that manage entire object prefixes, such as the backtest
    results store.
    """

    def read(self, relative_path: str) -> bytes:
        """Return the full payload of one object."""

    def list_prefix(self, prefix: str) -> tuple[str, ...]:
        """Return the object paths below ``prefix``."""

    def delete_prefix(self, prefix: str) -> int:
        """Remove every object below ``prefix`` and return how many went away."""


@runtime_checkable
class QueueSender(Protocol):
    def send(self, message: Mapping[str, object]) -> None: ...


@runtime_checkable
class SasIssuer(Protocol):
    def issue(self, object_path: str) -> str: ...


@runtime_checkable
class JobStorage(JobRepository, Protocol):
    """Job persistence boundary shared by memory and Azure implementations."""


__all__ = ["BlobStore", "JobStorage", "QueueSender", "ReadableBlobStore", "SasIssuer"]
