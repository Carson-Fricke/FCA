"""Provider-free storage-cap estimates and runtime pause guardrails.

The catalog's persisted Parquet byte sizes are a useful conservative estimate
for current lake usage. Deployments may instead inject an authoritative usage
reader. Neither path creates a provider or Azure client.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, cast, runtime_checkable

from lake.catalog import CatalogRepository, CoverageRecord
from lake.settings import StorageSettings


@dataclass(frozen=True, slots=True)
class StorageUsage:
    """An injected or catalog-derived estimate of bytes currently in storage."""

    current_bytes: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.current_bytes, bool)
            or not isinstance(self.current_bytes, int)
            or self.current_bytes < 0
        ):
            raise ValueError("current storage bytes must not be negative")

    @property
    def used_bytes(self) -> int:
        """Alias that makes the meaning clear at reporting call sites."""
        return self.current_bytes


@runtime_checkable
class StorageUsageProvider(Protocol):
    """Boundary for reading current storage usage without owning the reader."""

    def current_storage(self) -> StorageUsage | int:
        """Return the current storage estimate in bytes."""


StorageUsageReader = StorageUsageProvider | Callable[[], StorageUsage | int]


def estimate_current_storage(
    records: Iterable[CoverageRecord] | CatalogRepository,
) -> StorageUsage:
    """Estimate current storage by summing catalog byte sizes."""
    values = (
        cast(CatalogRepository, records).list()
        if hasattr(records, "list")
        else cast(Iterable[CoverageRecord], records)
    )
    return StorageUsage(sum(record.byte_size for record in values))


class StorageOperation(StrEnum):
    """Operations that have independent configured pause switches."""

    ENQUEUE = "enqueue"
    INGEST = "ingest"


class StorageCapState(StrEnum):
    """Stable states suitable for metrics, logs, and alert rules."""

    AVAILABLE = "available"
    AT_CAPACITY = "at_capacity"
    OVER_CAPACITY = "over_capacity"
    PAUSED = "paused"


@dataclass(frozen=True, slots=True)
class StorageCapPolicy:
    """Explicit capacity and operation-specific pause decisions."""

    capacity_bytes: int
    pause_enqueue_at_capacity: bool = True
    pause_ingest_at_capacity: bool = True

    def __post_init__(self) -> None:
        if (
            isinstance(self.capacity_bytes, bool)
            or not isinstance(self.capacity_bytes, int)
            or self.capacity_bytes < 1
        ):
            raise ValueError("storage capacity must be positive")
        if not isinstance(self.pause_enqueue_at_capacity, bool):
            raise TypeError("pause_enqueue_at_capacity must be a boolean")
        if not isinstance(self.pause_ingest_at_capacity, bool):
            raise TypeError("pause_ingest_at_capacity must be a boolean")

    @classmethod
    def from_settings(cls, settings: StorageSettings) -> StorageCapPolicy:
        """Build the runtime policy from T026's typed storage settings."""
        return cls(
            capacity_bytes=settings.capacity_bytes,
            pause_enqueue_at_capacity=settings.pause_enqueue_at_capacity,
            pause_ingest_at_capacity=settings.pause_ingest_at_capacity,
        )

    def pauses(self, operation: StorageOperation) -> bool:
        """Return whether the supplied operation is stopped at the cap."""
        normalized = StorageOperation(operation)
        return (
            self.pause_enqueue_at_capacity
            if normalized is StorageOperation.ENQUEUE
            else self.pause_ingest_at_capacity
        )


@dataclass(frozen=True, slots=True)
class StorageCapStatus:
    """Observable result of one capacity evaluation."""

    operation: StorageOperation
    state: StorageCapState
    current_bytes: int
    projected_bytes: int
    capacity_bytes: int
    pause_enabled: bool
    reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "operation", StorageOperation(self.operation))
        object.__setattr__(self, "state", StorageCapState(self.state))
        if (
            isinstance(self.current_bytes, bool)
            or not isinstance(self.current_bytes, int)
            or isinstance(self.projected_bytes, bool)
            or not isinstance(self.projected_bytes, int)
            or self.current_bytes < 0
            or self.projected_bytes < 0
        ):
            raise ValueError("storage status bytes must not be negative")
        if (
            isinstance(self.capacity_bytes, bool)
            or not isinstance(self.capacity_bytes, int)
            or self.capacity_bytes < 1
        ):
            raise ValueError("storage status capacity must be positive")
        if not isinstance(self.pause_enabled, bool):
            raise TypeError("pause_enabled must be a boolean")

    @property
    def paused(self) -> bool:
        """Whether this operation is currently prevented from proceeding."""
        return self.state is StorageCapState.PAUSED

    @property
    def alertable(self) -> bool:
        """Whether the estimate has reached or would cross the configured cap."""
        return self.projected_bytes >= self.capacity_bytes

    @property
    def remaining_bytes(self) -> int:
        """Return non-negative capacity remaining before the projected write."""
        return max(0, self.capacity_bytes - self.projected_bytes)

    def as_alert(self) -> dict[str, object]:
        """Return stable, non-secret fields for an alert or structured log."""
        return {
            "alertable": self.alertable,
            "capacity_bytes": self.capacity_bytes,
            "current_bytes": self.current_bytes,
            "operation": self.operation.value,
            "pause_enabled": self.pause_enabled,
            "projected_bytes": self.projected_bytes,
            "reason": self.reason,
            "state": self.state.value,
        }


class StorageCapacityPaused(RuntimeError):
    """Raised when a configured operation is stopped by the capacity guard."""

    def __init__(self, status: StorageCapStatus) -> None:
        self.status = status
        super().__init__(
            f"storage capacity guard paused {status.operation.value}: "
            f"{status.projected_bytes} of {status.capacity_bytes} bytes "
            f"({status.reason or 'capacity limit'})"
        )


class StorageCapGuard:
    """Evaluate and enforce capacity using injected usage and typed settings."""

    def __init__(
        self,
        *,
        storage_usage: StorageUsageReader,
        settings: StorageCapPolicy | StorageSettings,
    ) -> None:
        self._storage_usage = storage_usage
        self._policy = (
            settings
            if isinstance(settings, StorageCapPolicy)
            else StorageCapPolicy.from_settings(settings)
        )
        self._last_status: StorageCapStatus | None = None

    @property
    def policy(self) -> StorageCapPolicy:
        """Return the immutable policy used for every evaluation."""
        return self._policy

    @property
    def last_status(self) -> StorageCapStatus | None:
        """Return the most recent status, if an operation has been evaluated."""
        return self._last_status

    def evaluate(
        self,
        operation: StorageOperation,
        *,
        additional_bytes: int = 0,
    ) -> StorageCapStatus:
        """Evaluate current plus staged bytes without changing storage."""
        normalized_operation = StorageOperation(operation)
        if (
            isinstance(additional_bytes, bool)
            or not isinstance(additional_bytes, int)
            or additional_bytes < 0
        ):
            raise ValueError("additional storage bytes must not be negative")
        current_bytes = _read_current_bytes(self._storage_usage)
        projected_bytes = current_bytes + additional_bytes
        reaches_capacity = projected_bytes >= self._policy.capacity_bytes
        would_exceed = projected_bytes > self._policy.capacity_bytes
        pause_enabled = self._policy.pauses(normalized_operation)
        if pause_enabled and reaches_capacity:
            state = StorageCapState.PAUSED
        elif would_exceed:
            state = StorageCapState.OVER_CAPACITY
        elif reaches_capacity:
            state = StorageCapState.AT_CAPACITY
        else:
            state = StorageCapState.AVAILABLE
        reason = (
            "capacity_would_be_exceeded"
            if would_exceed
            else "capacity_reached"
            if reaches_capacity
            else None
        )
        status = StorageCapStatus(
            operation=normalized_operation,
            state=state,
            current_bytes=current_bytes,
            projected_bytes=projected_bytes,
            capacity_bytes=self._policy.capacity_bytes,
            pause_enabled=pause_enabled,
            reason=reason,
        )
        self._last_status = status
        return status

    def enforce_enqueue(self) -> StorageCapStatus:
        """Allow enqueue or raise an explicit, alertable pause exception."""
        return self._enforce(StorageOperation.ENQUEUE)

    def enforce_ingest(self, *, additional_bytes: int = 0) -> StorageCapStatus:
        """Allow ingest or raise before staged bytes become visible."""
        return self._enforce(StorageOperation.INGEST, additional_bytes=additional_bytes)

    def _enforce(
        self,
        operation: StorageOperation,
        *,
        additional_bytes: int = 0,
    ) -> StorageCapStatus:
        status = self.evaluate(operation, additional_bytes=additional_bytes)
        if status.paused:
            raise StorageCapacityPaused(status)
        return status


def _read_current_bytes(reader: StorageUsageReader) -> int:
    value = (
        cast(StorageUsageProvider, reader).current_storage()
        if hasattr(reader, "current_storage")
        else cast(Callable[[], StorageUsage | int], reader)()
    )
    current = value.current_bytes if isinstance(value, StorageUsage) else value
    if isinstance(current, bool) or not isinstance(current, int) or current < 0:
        raise ValueError("storage usage reader must return non-negative integer bytes")
    return current


__all__ = [
    "StorageCapacityPaused",
    "StorageCapGuard",
    "StorageCapPolicy",
    "StorageCapState",
    "StorageCapStatus",
    "StorageOperation",
    "StorageUsage",
    "StorageUsageProvider",
    "estimate_current_storage",
]
