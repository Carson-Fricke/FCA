"""Queued, fenced single-writer coordination for canonical workspace CLEO state."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import uuid
from dataclasses import dataclass
from io import BufferedRandom
from pathlib import Path
from types import TracebackType
from typing import Protocol, runtime_checkable

from thesis.cleo.profile import WorkspaceCleoProfile
from thesis.cleo.recovery import (
    CleoMutation,
    JournalConflictError,
    JournalEntry,
    JsonValue,
    MutationJournal,
    create_sqlite_snapshot,
    install_sqlite_snapshot,
    normalize_json_value,
)

_MUTATION_TABLE = "_thesis_cleo_mutations"
_SQLITE_BUSY_TIMEOUT_MILLISECONDS = 5_000


class CleoCoordinatorError(RuntimeError):
    """Base error for coordinator ownership or mutation failures."""


class CleoOwnershipError(CleoCoordinatorError):
    """Another coordinator owns the canonical workspace CLEO database."""


@dataclass(frozen=True, slots=True)
class CleoPublication:
    """A validated canonical snapshot and mutation record ready for Git publication."""

    idempotency_key: str
    operation: str
    payload_hash: str
    database_path: Path
    database_sha256: str
    mutation_record_path: Path
    fence_token: int


@dataclass(frozen=True, slots=True)
class CleoMutationResult:
    """The durable disposition of a submitted CLEO mutation."""

    idempotency_key: str
    applied: bool
    published: bool
    mutation_receipt: JsonValue
    publication_receipt: JsonValue
    database_sha256: str


@runtime_checkable
class CleoMutationExecutor(Protocol):
    """Apply one logical mutation to a coordinator-owned staging database."""

    def apply(
        self,
        database_path: Path,
        mutation: CleoMutation,
    ) -> JsonValue:
        """Mutate only ``database_path`` and return after closing database handles."""


@runtime_checkable
class CleoPublisher(Protocol):
    """Idempotent boundary to the workspace Git publication coordinator."""

    def publish(self, publication: CleoPublication) -> JsonValue:
        """Publish once by idempotency key, returning the same receipt on retries."""


@dataclass(slots=True)
class _Request:
    mutation: CleoMutation
    future: asyncio.Future[CleoMutationResult]


class CleoCoordinator:
    """Serialize proposals and own all writes to one canonical CLEO database."""

    def __init__(
        self,
        *,
        profile: WorkspaceCleoProfile,
        executor: CleoMutationExecutor,
        publisher: CleoPublisher,
    ) -> None:
        self._profile = profile
        self._executor = executor
        self._publisher = publisher
        self._journal = MutationJournal(profile.journal_directory)
        self._queue: asyncio.Queue[_Request | None] = asyncio.Queue()
        self._start_lock = asyncio.Lock()
        self._worker: asyncio.Task[None] | None = None
        self._ownership: _WorkspaceOwnership | None = None
        self._closing = False
        self._recovery_results: tuple[CleoMutationResult, ...] = ()

    @property
    def fence_token(self) -> int | None:
        """Return this running coordinator's monotonic ownership token."""
        return None if self._ownership is None else self._ownership.fence_token

    @property
    def recovery_results(self) -> tuple[CleoMutationResult, ...]:
        """Return mutations reconciled by the most recent successful startup."""
        return self._recovery_results

    async def __aenter__(self) -> CleoCoordinator:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    async def start(self) -> tuple[CleoMutationResult, ...]:
        """Acquire canonical ownership and reconcile interrupted publications."""
        async with self._start_lock:
            if self._worker is not None:
                return self._recovery_results
            if self._closing:
                raise CleoCoordinatorError("a closed coordinator cannot be restarted")
            self._profile.assert_ready()
            ownership = _WorkspaceOwnership(self._profile)
            ownership.acquire()
            self._ownership = ownership
            try:
                self._recovery_results = await asyncio.to_thread(self._recover_sync)
                self._worker = asyncio.create_task(
                    self._run(),
                    name=f"cleo-coordinator-{ownership.fence_token}",
                )
            except BaseException:
                self._ownership = None
                ownership.release()
                raise
            return self._recovery_results

    async def submit(self, mutation: CleoMutation) -> CleoMutationResult:
        """Queue one proposal and wait for its durable publication disposition."""
        await self.start()
        async with self._start_lock:
            if self._closing:
                raise CleoCoordinatorError("coordinator is closing")
            loop = asyncio.get_running_loop()
            future: asyncio.Future[CleoMutationResult] = loop.create_future()
            self._queue.put_nowait(_Request(mutation=mutation, future=future))
        return await asyncio.shield(future)

    async def close(self) -> None:
        """Drain accepted queue work, stop the writer, and release ownership."""
        async with self._start_lock:
            worker = self._worker
            if worker is None:
                self._closing = True
                return
            if not self._closing:
                self._closing = True
                self._queue.put_nowait(None)
        try:
            await worker
        finally:
            async with self._start_lock:
                self._worker = None
                ownership = self._ownership
                self._ownership = None
                if ownership is not None:
                    ownership.release()

    async def _run(self) -> None:
        while True:
            request = await self._queue.get()
            try:
                if request is None:
                    return
                try:
                    result = await asyncio.to_thread(
                        self._submit_sync,
                        request.mutation,
                    )
                except BaseException as error:
                    if not request.future.done():
                        request.future.set_exception(error)
                else:
                    if not request.future.done():
                        request.future.set_result(result)
            finally:
                self._queue.task_done()

    def _recover_sync(self) -> tuple[CleoMutationResult, ...]:
        return tuple(self._process_sync(entry) for entry in self._journal.pending())

    def _submit_sync(self, mutation: CleoMutation) -> CleoMutationResult:
        # A failed publication remains the head of the serialized outbox.
        self._recover_sync()
        entry = self._journal.accept(
            mutation,
            fence_token=self._require_fence_token(),
        )
        return self._process_sync(entry)

    def _process_sync(self, entry: JournalEntry) -> CleoMutationResult:
        applied, mutation_receipt = self._apply_once(entry)
        completed = self._journal.publication(entry)
        if completed is not None:
            return CleoMutationResult(
                idempotency_key=entry.mutation.idempotency_key,
                applied=applied,
                published=True,
                mutation_receipt=mutation_receipt,
                publication_receipt=completed.receipt,
                database_sha256=completed.database_sha256,
            )

        snapshot = install_sqlite_snapshot(
            self._profile.database_path,
            self._profile.database_path,
            temporary_directory=self._profile.snapshot_directory,
        )
        publication = CleoPublication(
            idempotency_key=entry.mutation.idempotency_key,
            operation=entry.mutation.operation,
            payload_hash=entry.payload_hash,
            database_path=snapshot.path,
            database_sha256=snapshot.sha256,
            mutation_record_path=entry.accepted_path,
            fence_token=self._require_fence_token(),
        )
        publication_receipt = normalize_json_value(self._publisher.publish(publication))
        completed = self._journal.mark_published(
            entry,
            database_sha256=snapshot.sha256,
            receipt=publication_receipt,
        )
        return CleoMutationResult(
            idempotency_key=entry.mutation.idempotency_key,
            applied=applied,
            published=True,
            mutation_receipt=mutation_receipt,
            publication_receipt=completed.receipt,
            database_sha256=completed.database_sha256,
        )

    def _apply_once(self, entry: JournalEntry) -> tuple[bool, JsonValue]:
        existing = self._canonical_receipt(entry)
        if existing is not None:
            return False, existing

        staging_path = self._profile.snapshot_directory / (
            f"mutation-{uuid.uuid4().hex}.db"
        )
        try:
            create_sqlite_snapshot(self._profile.database_path, staging_path)
            receipt = normalize_json_value(
                self._executor.apply(staging_path, entry.mutation)
            )
            self._record_canonical_mutation(staging_path, entry, receipt)
            install_sqlite_snapshot(
                staging_path,
                self._profile.database_path,
                temporary_directory=self._profile.snapshot_directory,
            )
            return True, receipt
        finally:
            _discard_database(staging_path)

    def _canonical_receipt(self, entry: JournalEntry) -> JsonValue | None:
        connection = sqlite3.connect(
            f"{self._profile.database_path.resolve().as_uri()}?mode=ro",
            uri=True,
            timeout=5.0,
        )
        try:
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (_MUTATION_TABLE,),
            ).fetchone()
            if table is None:
                return None
            existing = connection.execute(
                f"SELECT payload_hash, receipt_json FROM {_MUTATION_TABLE} "
                "WHERE idempotency_key = ?",
                (entry.mutation.idempotency_key,),
            ).fetchone()
            if existing is None:
                return None
            if existing[0] != entry.payload_hash:
                raise JournalConflictError(
                    f"idempotency key {entry.mutation.idempotency_key!r} has "
                    "conflicting canonical mutation state"
                )
            return _decode_receipt(str(existing[1]))
        finally:
            connection.close()

    def _record_canonical_mutation(
        self,
        staging_path: Path,
        entry: JournalEntry,
        receipt: JsonValue,
    ) -> None:
        connection = sqlite3.connect(
            staging_path,
            isolation_level=None,
            timeout=5.0,
        )
        try:
            connection.execute(
                f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MILLISECONDS}"
            )
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {_MUTATION_TABLE} (
                    idempotency_key TEXT PRIMARY KEY,
                    payload_hash TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    receipt_json TEXT NOT NULL,
                    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    fence_token INTEGER NOT NULL
                )
                """
            )
            receipt_json = json.dumps(
                receipt,
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            connection.execute(
                f"INSERT INTO {_MUTATION_TABLE} "
                "(idempotency_key, payload_hash, operation, receipt_json, fence_token) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    entry.mutation.idempotency_key,
                    entry.payload_hash,
                    entry.mutation.operation,
                    receipt_json,
                    self._require_fence_token(),
                ),
            )
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _require_fence_token(self) -> int:
        ownership = self._ownership
        if ownership is None:
            raise CleoOwnershipError("coordinator does not own the canonical database")
        return ownership.fence_token


class _WorkspaceOwnership:
    def __init__(self, profile: WorkspaceCleoProfile) -> None:
        self._profile = profile
        self._stream: BufferedRandom | None = None
        self.fence_token = 0

    def acquire(self) -> None:
        stream = self._profile.lock_path.open("a+b")
        locked = False
        try:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            _lock_file(stream)
            locked = True
            self._stream = stream
            self.fence_token = _advance_fence(self._profile.fence_path)
        except Exception as error:
            if locked:
                _unlock_file(stream)
            stream.close()
            if isinstance(error, CleoOwnershipError):
                raise
            raise CleoOwnershipError(
                f"canonical CLEO database already has a writer: {self._profile.database_path}"
            ) from error

    def release(self) -> None:
        stream = self._stream
        if stream is None:
            return
        self._stream = None
        try:
            _unlock_file(stream)
        finally:
            stream.close()


def _decode_receipt(encoded: str) -> JsonValue:
    try:
        return normalize_json_value(json.loads(encoded))
    except (json.JSONDecodeError, TypeError) as error:
        raise CleoCoordinatorError(
            "canonical mutation receipt is not valid JSON"
        ) from error


def _advance_fence(path: Path) -> int:
    current = 0
    if path.exists():
        try:
            current = int(path.read_text(encoding="ascii").strip())
        except (OSError, ValueError) as error:
            raise CleoOwnershipError(f"invalid CLEO fence counter: {path}") from error
    token = current + 1
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="ascii", newline="\n") as stream:
            stream.write(f"{token}\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _flush_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)
    return token


def _discard_database(path: Path) -> None:
    path.unlink(missing_ok=True)
    for suffix in ("-journal", "-shm", "-wal"):
        path.with_name(f"{path.name}{suffix}").unlink(missing_ok=True)


def _flush_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


if os.name == "nt":
    import msvcrt

    def _lock_file(stream: BufferedRandom) -> None:
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock_file(stream: BufferedRandom) -> None:
        stream.seek(0)
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock_file(stream: BufferedRandom) -> None:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock_file(stream: BufferedRandom) -> None:
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


__all__ = [
    "CleoCoordinator",
    "CleoCoordinatorError",
    "CleoMutationExecutor",
    "CleoMutationResult",
    "CleoOwnershipError",
    "CleoPublication",
    "CleoPublisher",
]
