"""Backtest results store: run artifacts plus a fast run catalog.

The store writes one run's artifacts below the blob prefix
``backtests/{run_id}/`` through the lake blob-store port and keeps one
catalog row per run through a repository port shaped for the existing
Azure Table adapter pattern. This is infrastructure code, not the
synchronous kernel: all I/O happens through the ports, and the in-memory
adapters keep tests free of network access and Azure credentials.

Artifact digests reuse the repository's SHA-256 hex convention, and the
run digest is a domain-separated hash over the canonical JSON of the
per-artifact digests produced by :mod:`harness.configuration.hashing`.
Run identity comes from callers, and every stored value is derived from
supplied bytes: no wall-clock reads, unseeded randomness, or network
calls occur inside this module.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Final, Protocol, runtime_checkable

from harness.configuration.hashing import canonical_configuration_bytes
from harness.core.types.common import text
from lake.storage.ports import BlobStore

_RUN_PREFIX: Final = "backtests"
_RUN_DIGEST_DOMAIN: Final = b"fca:harness-results:run:v1\0"
_RUN_DIGEST_VERSION: Final = "harness.results.run.v1"
_EMPTY_METRICS: Final[Mapping[str, str]] = MappingProxyType({})


class RunStatus(StrEnum):
    """Lifecycle states for one stored backtest run."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"


def _path_segment(value: object, field_name: str) -> str:
    segment = text(value, field_name)
    if "\\" in segment or "/" in segment or segment in {".", ".."}:
        raise ValueError(f"{field_name} must be a single safe path segment")
    return segment


def _object_path(relative_path: str) -> str:
    if (
        not relative_path
        or "\\" in relative_path
        or relative_path.startswith("/")
        or any(part in {"", ".", ".."} for part in relative_path.split("/"))
    ):
        raise ValueError("object path must be a non-empty relative POSIX path")
    return relative_path


def _artifact_name(value: object) -> str:
    return _object_path(text(value, "artifact name"))


def _metric_value(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("metric values must be strings to stay string-exact")
    return text(value, "metric value")


def run_prefix(run_id: str) -> str:
    """Return the blob prefix holding one run's artifacts."""
    return f"{_RUN_PREFIX}/{_path_segment(run_id, 'run_id')}"


def artifact_digest(payload: bytes) -> str:
    """Return the SHA-256 hex digest of one artifact payload."""
    return hashlib.sha256(payload).hexdigest()


def run_digest(run_id: str, digests: Mapping[str, str]) -> str:
    """Return the domain-separated digest over one run's artifact digests."""
    payload = {
        "domain": _RUN_DIGEST_VERSION,
        "run_id": run_id,
        "artifacts": {name: digests[name] for name in sorted(digests)},
    }
    return hashlib.sha256(
        _RUN_DIGEST_DOMAIN + canonical_configuration_bytes(payload)
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class RunCatalogRecord:
    """Catalog row for one backtest run.

    ``run_digest``, ``artifact_count``, and ``total_bytes`` are derived by
    the store from the uploaded artifact bytes. ``configuration_digest``
    comes from :func:`harness.configuration.hashing.configuration_hash`
    over the resolved strategy configuration. Headline ``metrics`` use
    the repository's string-exact decimal encoding.
    """

    run_id: str
    status: RunStatus
    strategy_id: str
    data_start: date | None = None
    data_end: date | None = None
    run_digest: str | None = None
    configuration_digest: str | None = None
    artifact_count: int = 0
    total_bytes: int = 0
    metrics: Mapping[str, str] = _EMPTY_METRICS

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _path_segment(self.run_id, "run_id"))
        object.__setattr__(self, "status", RunStatus(self.status))
        object.__setattr__(self, "strategy_id", text(self.strategy_id, "strategy_id"))
        if isinstance(self.data_start, datetime):
            raise TypeError("data_start must be a date, not a datetime")
        if isinstance(self.data_end, datetime):
            raise TypeError("data_end must be a date, not a datetime")
        if (
            self.data_start is not None
            and self.data_end is not None
            and self.data_start > self.data_end
        ):
            raise ValueError("data_start must be on or before data_end")
        if self.artifact_count < 0 or self.total_bytes < 0:
            raise ValueError("artifact_count and total_bytes must not be negative")
        if not isinstance(self.metrics, Mapping):
            raise TypeError("metrics must be a mapping")
        object.__setattr__(
            self,
            "metrics",
            MappingProxyType(
                {
                    text(key, "metric name"): _metric_value(value)
                    for key, value in self.metrics.items()
                }
            ),
        )


@runtime_checkable
class RunBlobStore(BlobStore, Protocol):
    """Blob boundary for run artifacts.

    Extends the lake :class:`~lake.storage.ports.BlobStore` whole-object
    replacement port with the read and prefix operations the results
    store needs.
    """

    def read(self, relative_path: str) -> bytes: ...

    def list_prefix(self, prefix: str) -> tuple[str, ...]: ...

    def delete_prefix(self, prefix: str) -> int: ...


@runtime_checkable
class RunCatalogRepository(Protocol):
    """Catalog boundary for backtest run metadata rows."""

    def get(self, run_id: str) -> RunCatalogRecord | None: ...

    def upsert(self, record: RunCatalogRecord) -> RunCatalogRecord: ...

    def list(
        self,
        *,
        status: RunStatus | None = None,
        strategy_id: str | None = None,
    ) -> tuple[RunCatalogRecord, ...]: ...

    def delete(self, run_id: str) -> bool: ...


class InMemoryRunBlobStore:
    """Deterministic in-memory blob adapter for tests and local tooling."""

    def __init__(self, objects: Mapping[str, bytes] | None = None) -> None:
        self._objects: dict[str, bytes] = dict(objects) if objects is not None else {}

    def replace(self, relative_path: str, payload: bytes) -> None:
        """Replace one object in full, mirroring the blob-store port."""
        self._objects[_object_path(relative_path)] = bytes(payload)

    def read(self, relative_path: str) -> bytes:
        """Return one object's payload."""
        path = _object_path(relative_path)
        try:
            return self._objects[path]
        except KeyError:
            raise KeyError(f"object not found: {path}") from None

    def list_prefix(self, prefix: str) -> tuple[str, ...]:
        """Return sorted object paths below ``prefix``."""
        return tuple(sorted(path for path in self._objects if path.startswith(prefix)))

    def delete_prefix(self, prefix: str) -> int:
        """Remove every object below ``prefix`` and return how many went away."""
        matches = [path for path in self._objects if path.startswith(prefix)]
        for path in matches:
            del self._objects[path]
        return len(matches)


class InMemoryRunCatalogRepository:
    """Deterministic in-memory run catalog adapter for tests."""

    def __init__(self, records: Iterable[RunCatalogRecord] = ()) -> None:
        self._records = {record.run_id: record for record in records}

    def get(self, run_id: str) -> RunCatalogRecord | None:
        return self._records.get(_path_segment(run_id, "run_id"))

    def upsert(self, record: RunCatalogRecord) -> RunCatalogRecord:
        self._records[record.run_id] = record
        return record

    def list(
        self,
        *,
        status: RunStatus | None = None,
        strategy_id: str | None = None,
    ) -> tuple[RunCatalogRecord, ...]:
        status = RunStatus(status) if status is not None else None
        if strategy_id is not None:
            strategy_id = text(strategy_id, "strategy_id")
        return tuple(
            record
            for record in sorted(self._records.values(), key=lambda item: item.run_id)
            if (status is None or record.status == status)
            and (strategy_id is None or record.strategy_id == strategy_id)
        )

    def delete(self, run_id: str) -> bool:
        return self._records.pop(_path_segment(run_id, "run_id"), None) is not None


@dataclass(frozen=True, slots=True)
class StoredRun:
    """One fetched run: its catalog row and immutable artifact payloads."""

    record: RunCatalogRecord
    artifacts: Mapping[str, bytes]


class BacktestResultsStore:
    """Store run artifacts below ``backtests/{run_id}`` with a catalog row."""

    def __init__(self, blobs: RunBlobStore, catalog: RunCatalogRepository) -> None:
        self._blobs = blobs
        self._catalog = catalog

    def put(
        self,
        record: RunCatalogRecord,
        artifacts: Mapping[str, bytes],
    ) -> RunCatalogRecord:
        """Replace the run's blob prefix with ``artifacts`` and upsert its row.

        The returned record recomputes ``run_digest``, ``artifact_count``,
        and ``total_bytes`` from the supplied payload bytes; the passed
        record is never mutated. Artifact names are relative POSIX paths
        below the run prefix, so stale blobs from a prior upload cannot
        survive a re-upload.
        """
        run_id = record.run_id
        if not artifacts:
            raise ValueError("a run must contain at least one artifact")
        prepared: dict[str, bytes] = {}
        digests: dict[str, str] = {}
        total_bytes = 0
        for name, payload in artifacts.items():
            safe_name = _artifact_name(name)
            if not isinstance(payload, bytes):
                raise TypeError(f"artifact {safe_name!r} payload must be bytes")
            digests[safe_name] = artifact_digest(payload)
            total_bytes += len(payload)
            prepared[f"{run_prefix(run_id)}/{safe_name}"] = payload
        prefix = f"{run_prefix(run_id)}/"
        self._blobs.delete_prefix(prefix)
        for path, payload in prepared.items():
            self._blobs.replace(path, payload)
        return self._catalog.upsert(
            replace(
                record,
                run_digest=run_digest(run_id, digests),
                artifact_count=len(digests),
                total_bytes=total_bytes,
            )
        )

    def get(self, run_id: str) -> RunCatalogRecord | None:
        """Return one run's catalog row, or ``None`` when unknown."""
        return self._catalog.get(run_id)

    def list(
        self,
        *,
        status: RunStatus | None = None,
        strategy_id: str | None = None,
    ) -> tuple[RunCatalogRecord, ...]:
        """List catalog rows sorted by run ID, filtered by status or strategy."""
        return self._catalog.list(status=status, strategy_id=strategy_id)

    def fetch(self, run_id: str) -> StoredRun:
        """Return one run's catalog row together with every artifact payload."""
        record = self.get(run_id)
        if record is None:
            raise KeyError(f"run not found: {run_id}")
        prefix = f"{run_prefix(record.run_id)}/"
        found = {
            path.removeprefix(prefix): self._blobs.read(path)
            for path in self._blobs.list_prefix(prefix)
        }
        return StoredRun(record=record, artifacts=MappingProxyType(found))

    def delete(self, run_id: str) -> bool:
        """Remove the complete run prefix and its catalog row together.

        Blobs go before the row, partial states are cleaned on both sides,
        and ``True`` is returned when either side held anything; deleting
        an unknown run is a no-op returning ``False``.
        """
        run_id = _path_segment(run_id, "run_id")
        blobs_removed = self._blobs.delete_prefix(f"{run_prefix(run_id)}/")
        row_removed = self._catalog.delete(run_id)
        return blobs_removed > 0 or row_removed


__all__ = [
    "BacktestResultsStore",
    "InMemoryRunBlobStore",
    "InMemoryRunCatalogRepository",
    "RunBlobStore",
    "RunCatalogRecord",
    "RunCatalogRepository",
    "RunStatus",
    "StoredRun",
    "artifact_digest",
    "run_digest",
    "run_prefix",
]
