"""Scoped leases for protecting symbol-day lake replacements.

The lease provider is deliberately separate from the T034 atomic committer so
local tests and other storage implementations can supply their own lock.  The
Azure implementation uses a durable, deterministic blob as the lease resource;
the blob is never deleted when a lease is released, so another writer can
acquire the same scope later.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol, runtime_checkable
from urllib.parse import quote

from azure.core.credentials import TokenCredential
from azure.core.exceptions import ResourceExistsError
from azure.storage.blob import BlobLeaseClient, BlobServiceClient, ContainerClient

from lake.contracts import AssetClass, symbol_filename


@dataclass(frozen=True, slots=True)
class SymbolDayScope:
    """The shared lock identity for one asset, symbol, and session date."""

    asset_class: AssetClass
    symbol: str
    session_date: date

    def __post_init__(self) -> None:
        if isinstance(self.session_date, datetime):
            raise TypeError("session_date must be a date, not a datetime")
        if not isinstance(self.symbol, str):
            raise TypeError("symbol must be a string")
        canonical_symbol = self.symbol.strip().upper()
        symbol_filename(canonical_symbol)
        object.__setattr__(self, "asset_class", AssetClass(self.asset_class))
        object.__setattr__(self, "symbol", canonical_symbol)

    @property
    def key(self) -> str:
        """Return a stable, provider-independent scope key."""
        return "/".join(
            (
                self.asset_class.value,
                self.session_date.isoformat(),
                quote(self.symbol, safe="._-"),
            )
        )

    @property
    def blob_name(self) -> str:
        """Return the deterministic relative name used by Azure lock blobs."""
        return (
            f"date={self.session_date.isoformat()}/"
            f"asset_class={self.asset_class.value}/"
            f"{quote(self.symbol, safe='._-')}.lock"
        )


@runtime_checkable
class Lease(Protocol):
    """A held scope lease with explicit renewal and release operations."""

    @property
    def lease_id(self) -> str:
        """Return the provider lease identifier."""

    def renew(self) -> None:
        """Renew the lease while it is still held."""

    def release(self) -> None:
        """Release the lease; successful releases are idempotent."""


@runtime_checkable
class LeaseProvider(Protocol):
    """Injectable source of leases for symbol-day scopes."""

    def acquire(self, scope: SymbolDayScope) -> Lease:
        """Acquire the scope or raise when another writer owns it."""


@contextmanager
def hold_lease(provider: LeaseProvider, scope: SymbolDayScope) -> Iterator[Lease]:
    """Acquire a lease and release it on every exit path."""
    lease = provider.acquire(scope)
    try:
        yield lease
    finally:
        lease.release()


class AzureBlobLease:
    """Safe facade over the Azure SDK lease returned for one lock blob."""

    def __init__(self, lease: BlobLeaseClient) -> None:
        self._lease = lease
        self._lease_id = lease.id
        self._released = False

    @property
    def lease_id(self) -> str:
        """Return the Azure lease ID used to identify this owner."""
        return self._lease_id

    def renew(self) -> None:
        """Renew only while this facade still owns an active lease."""
        self._ensure_active()
        self._lease.renew()

    def release(self) -> None:
        """Release at most once, retaining the ID-bound Azure operation."""
        if self._released:
            return
        self._lease.release()
        self._released = True

    def _ensure_active(self) -> None:
        if self._released:
            raise RuntimeError("cannot renew a released lease")


class AzureBlobLeaseProvider:
    """Acquire symbol-day leases from token-authenticated Azure Blob Storage."""

    _MIN_LEASE_SECONDS = 15
    _MAX_LEASE_SECONDS = 60

    def __init__(
        self,
        container: ContainerClient,
        *,
        lease_duration_seconds: int = 60,
        blob_prefix: str = "locks",
    ) -> None:
        if (
            not self._MIN_LEASE_SECONDS
            <= lease_duration_seconds
            <= self._MAX_LEASE_SECONDS
        ):
            raise ValueError("lease duration must be between 15 and 60 seconds")
        self._container = container
        self._lease_duration_seconds = lease_duration_seconds
        self._blob_prefix = _relative_prefix(blob_prefix)

    @classmethod
    def from_account_url(
        cls,
        account_url: str,
        container_name: str,
        *,
        credential: TokenCredential,
        lease_duration_seconds: int = 60,
        blob_prefix: str = "locks",
    ) -> AzureBlobLeaseProvider:
        """Create a provider using the caller-supplied token credential."""
        service = BlobServiceClient(account_url=account_url, credential=credential)
        return cls(
            service.get_container_client(container_name),
            lease_duration_seconds=lease_duration_seconds,
            blob_prefix=blob_prefix,
        )

    def acquire(self, scope: SymbolDayScope) -> AzureBlobLease:
        """Create the lock blob if needed, then acquire its exclusive lease."""
        blob = self._container.get_blob_client(self._blob_name(scope))
        try:
            blob.upload_blob(b"", overwrite=False)
        except ResourceExistsError:
            pass
        return AzureBlobLease(
            blob.acquire_lease(lease_duration=self._lease_duration_seconds)
        )

    def _blob_name(self, scope: SymbolDayScope) -> str:
        return f"{self._blob_prefix}/{scope.blob_name}"


def _relative_prefix(prefix: str) -> str:
    if (
        not prefix
        or "\\" in prefix
        or prefix.startswith("/")
        or any(part in {"", ".", ".."} for part in prefix.split("/"))
    ):
        raise ValueError("blob prefix must be a non-empty relative POSIX path")
    return prefix.rstrip("/")


__all__ = [
    "AzureBlobLease",
    "AzureBlobLeaseProvider",
    "Lease",
    "LeaseProvider",
    "SymbolDayScope",
    "hold_lease",
]
