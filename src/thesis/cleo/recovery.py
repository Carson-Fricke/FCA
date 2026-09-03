"""Durable mutation records and safe SQLite snapshots for workspace CLEO state."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import uuid
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

type JsonValue = (
    None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
)
type JsonObject = dict[str, JsonValue]

_JOURNAL_SCHEMA_VERSION = 1
_SQLITE_BUSY_TIMEOUT_MILLISECONDS = 5_000


class CleoRecoveryError(RuntimeError):
    """Base error for invalid recovery state."""


class JournalConflictError(CleoRecoveryError):
    """An idempotency key was reused for a different logical mutation."""


class SQLiteSnapshotError(CleoRecoveryError):
    """A canonical database could not be validated or safely snapshotted."""


@dataclass(frozen=True, slots=True)
class CleoMutation:
    """A durable, JSON-serializable proposal for one logical CLEO mutation."""

    idempotency_key: str
    operation: str
    arguments: JsonObject

    def __post_init__(self) -> None:
        key = self.idempotency_key.strip()
        operation = self.operation.strip()
        if not key:
            raise ValueError("idempotency_key must not be empty")
        if not operation:
            raise ValueError("operation must not be empty")
        arguments = normalize_json_object(self.arguments)
        object.__setattr__(self, "idempotency_key", key)
        object.__setattr__(self, "operation", operation)
        object.__setattr__(self, "arguments", arguments)

    @property
    def payload_hash(self) -> str:
        """Return the stable identity of the operation and its arguments."""
        payload: JsonObject = {
            "arguments": self.arguments,
            "operation": self.operation,
        }
        return hashlib.sha256(_canonical_json(payload)).hexdigest()


@dataclass(frozen=True, slots=True)
class JournalEntry:
    """An accepted mutation reconstructed from its durable journal record."""

    mutation: CleoMutation
    payload_hash: str
    accepted_path: Path
    accepted_at: str
    fence_token: int


@dataclass(frozen=True, slots=True)
class PublicationRecord:
    """The durable completion marker for a published database snapshot."""

    payload_hash: str
    database_sha256: str
    published_at: str
    receipt: JsonValue


@dataclass(frozen=True, slots=True)
class SQLiteValidation:
    """Successful SQLite integrity and referential-integrity validation."""

    path: Path
    integrity_messages: tuple[str, ...]
    foreign_key_violations: int


@dataclass(frozen=True, slots=True)
class SQLiteSnapshot:
    """A standalone, validated SQLite database ready for publication."""

    path: Path
    sha256: str
    size_bytes: int
    validation: SQLiteValidation


class MutationJournal:
    """Append-only accepted and published records for CLEO mutation recovery."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    @property
    def directory(self) -> Path:
        """Return the workspace journal directory used by this coordinator."""
        return self._directory

    def accept(self, mutation: CleoMutation, *, fence_token: int) -> JournalEntry:
        """Durably accept a proposal, or return its identical existing record."""
        if fence_token < 1:
            raise ValueError("fence_token must be positive")
        record_directory = self._record_directory(mutation.idempotency_key)
        accepted_path = record_directory / "accepted.json"
        if accepted_path.exists():
            entry = self._read_accepted(accepted_path)
            if (
                entry.mutation.idempotency_key != mutation.idempotency_key
                or entry.payload_hash != mutation.payload_hash
            ):
                raise JournalConflictError(
                    f"idempotency key {mutation.idempotency_key!r} already identifies "
                    "a different CLEO mutation"
                )
            return entry

        _ensure_plain_directory(self._directory)
        _ensure_plain_directory(record_directory)
        accepted_at = _utc_now()
        document: JsonObject = {
            "acceptedAt": accepted_at,
            "arguments": mutation.arguments,
            "fenceToken": fence_token,
            "idempotencyKey": mutation.idempotency_key,
            "operation": mutation.operation,
            "payloadHash": mutation.payload_hash,
            "schemaVersion": _JOURNAL_SCHEMA_VERSION,
        }
        try:
            _write_new_json(accepted_path, document)
        except FileExistsError:
            entry = self._read_accepted(accepted_path)
            if (
                entry.mutation.idempotency_key != mutation.idempotency_key
                or entry.payload_hash != mutation.payload_hash
            ):
                raise JournalConflictError(
                    f"idempotency key {mutation.idempotency_key!r} was accepted "
                    "concurrently with a different payload"
                ) from None
            return entry
        return JournalEntry(
            mutation=mutation,
            payload_hash=mutation.payload_hash,
            accepted_path=accepted_path,
            accepted_at=accepted_at,
            fence_token=fence_token,
        )

    def pending(self) -> tuple[JournalEntry, ...]:
        """Return accepted mutations that have no publication completion marker."""
        if not self._directory.exists():
            return ()
        if not self._directory.is_dir() or self._directory.is_symlink():
            raise CleoRecoveryError(
                f"CLEO journal is not a plain directory: {self._directory}"
            )
        entries: list[JournalEntry] = []
        for accepted_path in sorted(self._directory.glob("*/accepted.json")):
            entry = self._read_accepted(accepted_path)
            if self.publication(entry) is None:
                entries.append(entry)
        return tuple(
            sorted(
                entries,
                key=lambda entry: (
                    entry.accepted_at,
                    entry.mutation.idempotency_key,
                ),
            )
        )

    def publication(self, entry: JournalEntry) -> PublicationRecord | None:
        """Return and validate an entry's immutable publication marker."""
        path = entry.accepted_path.with_name("published.json")
        if not path.exists():
            return None
        document = _read_json_object(path)
        _expect_schema(document, path)
        idempotency_key = _expect_string(document, "idempotencyKey", path)
        payload_hash = _expect_digest(document, "payloadHash", path)
        database_sha256 = _expect_digest(document, "databaseSha256", path)
        published_at = _expect_string(document, "publishedAt", path)
        if idempotency_key != entry.mutation.idempotency_key:
            raise CleoRecoveryError(
                f"publication marker has the wrong idempotency key: {path}"
            )
        if payload_hash != entry.payload_hash:
            raise JournalConflictError(
                f"publication marker payload conflicts with {entry.mutation.idempotency_key!r}"
            )
        return PublicationRecord(
            payload_hash=payload_hash,
            database_sha256=database_sha256,
            published_at=published_at,
            receipt=document.get("receipt"),
        )

    def mark_published(
        self,
        entry: JournalEntry,
        *,
        database_sha256: str,
        receipt: JsonValue,
    ) -> PublicationRecord:
        """Append the completion marker after an idempotent publisher succeeds."""
        if len(database_sha256) != 64 or any(
            character not in "0123456789abcdef" for character in database_sha256
        ):
            raise ValueError("database_sha256 must be a SHA-256 hexadecimal digest")
        normalized_receipt = normalize_json_value(receipt)
        existing = self.publication(entry)
        if existing is not None:
            return existing
        published_at = _utc_now()
        document: JsonObject = {
            "databaseSha256": database_sha256,
            "idempotencyKey": entry.mutation.idempotency_key,
            "payloadHash": entry.payload_hash,
            "publishedAt": published_at,
            "receipt": normalized_receipt,
            "schemaVersion": _JOURNAL_SCHEMA_VERSION,
        }
        path = entry.accepted_path.with_name("published.json")
        try:
            _write_new_json(path, document)
        except FileExistsError:
            existing = self.publication(entry)
            if existing is None:
                raise CleoRecoveryError(
                    f"publication marker appeared but cannot be read: {path}"
                ) from None
            return existing
        return PublicationRecord(
            payload_hash=entry.payload_hash,
            database_sha256=database_sha256,
            published_at=published_at,
            receipt=normalized_receipt,
        )

    def _record_directory(self, idempotency_key: str) -> Path:
        digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
        return self._directory / digest

    def _read_accepted(self, path: Path) -> JournalEntry:
        document = _read_json_object(path)
        _expect_schema(document, path)
        arguments = document.get("arguments")
        if not isinstance(arguments, dict):
            raise CleoRecoveryError(f"journal arguments must be an object: {path}")
        mutation = CleoMutation(
            idempotency_key=_expect_string(document, "idempotencyKey", path),
            operation=_expect_string(document, "operation", path),
            arguments=arguments,
        )
        payload_hash = _expect_digest(document, "payloadHash", path)
        if payload_hash != mutation.payload_hash:
            raise CleoRecoveryError(
                f"journal payload hash does not match content: {path}"
            )
        fence_token = document.get("fenceToken")
        if not isinstance(fence_token, int) or isinstance(fence_token, bool):
            raise CleoRecoveryError(f"journal fence token must be an integer: {path}")
        return JournalEntry(
            mutation=mutation,
            payload_hash=payload_hash,
            accepted_path=path,
            accepted_at=_expect_string(document, "acceptedAt", path),
            fence_token=fence_token,
        )


def normalize_json_object(value: Mapping[str, object]) -> JsonObject:
    """Return a detached JSON object and reject non-portable values."""
    normalized = normalize_json_value(dict(value))
    if not isinstance(normalized, dict):
        raise TypeError("value must be a JSON object")
    return normalized


def normalize_json_value(value: object) -> JsonValue:
    """Return a detached JSON value and reject NaN, infinity, and custom objects."""
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        decoded: object = json.loads(encoded)
    except (TypeError, ValueError) as error:
        raise TypeError("value must contain only finite JSON values") from error
    if not _is_json_value(decoded):
        raise TypeError("value must contain only JSON values")
    return cast(JsonValue, decoded)


def validate_sqlite_database(database_path: Path) -> SQLiteValidation:
    """Run full SQLite and foreign-key integrity checks without creating a database."""
    if database_path.is_symlink():
        raise SQLiteSnapshotError(
            f"SQLite database must not be a symbolic link: {database_path}"
        )
    path = database_path.resolve()
    if not path.is_file():
        raise SQLiteSnapshotError(f"SQLite database is not a plain file: {path}")
    uri = f"{path.as_uri()}?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=5.0)) as connection:
            integrity_rows = connection.execute("PRAGMA integrity_check").fetchall()
            foreign_key_rows = connection.execute("PRAGMA foreign_key_check").fetchall()
    except sqlite3.Error as error:
        raise SQLiteSnapshotError(
            f"SQLite validation failed for {path}: {error}"
        ) from error
    messages = tuple(str(row[0]) for row in integrity_rows)
    if messages != ("ok",):
        detail = "; ".join(messages) if messages else "no integrity result"
        raise SQLiteSnapshotError(f"SQLite integrity check failed for {path}: {detail}")
    if foreign_key_rows:
        raise SQLiteSnapshotError(
            f"SQLite foreign-key check found {len(foreign_key_rows)} violation(s) in {path}"
        )
    return SQLiteValidation(
        path=path,
        integrity_messages=messages,
        foreign_key_violations=0,
    )


def create_sqlite_snapshot(source_path: Path, destination_path: Path) -> SQLiteSnapshot:
    """Checkpoint WAL, use SQLite backup, and validate a standalone snapshot."""
    if source_path.is_symlink():
        raise SQLiteSnapshotError(
            f"SQLite source must not be a symbolic link: {source_path}"
        )
    if destination_path.is_symlink():
        raise SQLiteSnapshotError(
            f"SQLite destination must not be a symbolic link: {destination_path}"
        )
    source = source_path.resolve()
    destination = destination_path.resolve()
    if source == destination:
        raise ValueError("source and destination SQLite paths must differ")
    if not source.is_file() or source.is_symlink():
        raise SQLiteSnapshotError(f"SQLite source is not a plain file: {source}")
    if destination.exists():
        raise FileExistsError(destination)
    _ensure_plain_directory(destination.parent)
    try:
        with closing(
            sqlite3.connect(
                source,
                isolation_level=None,
                timeout=5.0,
            )
        ) as source_connection:
            source_connection.execute(
                f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MILLISECONDS}"
            )
            checkpoint = source_connection.execute(
                "PRAGMA wal_checkpoint(TRUNCATE)"
            ).fetchone()
            if checkpoint is None or int(checkpoint[0]) != 0:
                raise SQLiteSnapshotError(
                    f"SQLite WAL checkpoint remained busy for {source}"
                )
            with closing(
                sqlite3.connect(
                    destination,
                    isolation_level=None,
                    timeout=5.0,
                )
            ) as destination_connection:
                source_connection.backup(destination_connection)
                mode_row = destination_connection.execute(
                    "PRAGMA journal_mode=DELETE"
                ).fetchone()
                if mode_row is None or str(mode_row[0]).lower() != "delete":
                    raise SQLiteSnapshotError(
                        f"snapshot did not close WAL journal mode for {destination}"
                    )
    except SQLiteSnapshotError:
        _remove_snapshot_files(destination)
        raise
    except sqlite3.Error as error:
        _remove_snapshot_files(destination)
        raise SQLiteSnapshotError(
            f"could not create SQLite snapshot {destination} from {source}: {error}"
        ) from error

    try:
        validation = validate_sqlite_database(destination)
        _flush_file(destination)
        digest = _sha256_file(destination)
        return SQLiteSnapshot(
            path=destination,
            sha256=digest,
            size_bytes=destination.stat().st_size,
            validation=validation,
        )
    except Exception:
        _remove_snapshot_files(destination)
        raise


def install_sqlite_snapshot(
    source_path: Path,
    canonical_path: Path,
    *,
    temporary_directory: Path,
) -> SQLiteSnapshot:
    """Atomically replace a canonical database with a clean validated snapshot."""
    if canonical_path.is_symlink():
        raise SQLiteSnapshotError(
            f"canonical SQLite path must not be a symbolic link: {canonical_path}"
        )
    _ensure_plain_directory(temporary_directory)
    temporary_path = temporary_directory / f"{uuid.uuid4().hex}.db"
    snapshot = create_sqlite_snapshot(source_path, temporary_path)
    canonical = canonical_path.resolve()
    _ensure_plain_directory(canonical.parent)
    try:
        os.replace(snapshot.path, canonical)
        _remove_sqlite_sidecars(canonical)
        _flush_file(canonical)
        _flush_directory(canonical.parent)
    except OSError as error:
        _remove_snapshot_files(temporary_path)
        raise SQLiteSnapshotError(
            f"could not install canonical SQLite snapshot at {canonical}: {error}"
        ) from error
    validation = validate_sqlite_database(canonical)
    return SQLiteSnapshot(
        path=canonical,
        sha256=snapshot.sha256,
        size_bytes=snapshot.size_bytes,
        validation=validation,
    )


def _canonical_json(value: JsonValue) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _is_json_value(value: object) -> bool:
    if value is None or isinstance(value, (bool, str, int)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_is_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(
            isinstance(key, str) and _is_json_value(item) for key, item in value.items()
        )
    return False


def _read_json_object(path: Path) -> JsonObject:
    try:
        with path.open("r", encoding="utf-8") as stream:
            value: object = json.load(stream)
    except (OSError, json.JSONDecodeError) as error:
        raise CleoRecoveryError(
            f"could not read CLEO journal record {path}: {error}"
        ) from error
    if not isinstance(value, dict) or not _is_json_value(value):
        raise CleoRecoveryError(f"CLEO journal record must be a JSON object: {path}")
    return cast(JsonObject, value)


def _write_new_json(path: Path, document: JsonObject) -> None:
    encoded = _canonical_json(document) + b"\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise
    _flush_directory(path.parent)


def _expect_schema(document: JsonObject, path: Path) -> None:
    if document.get("schemaVersion") != _JOURNAL_SCHEMA_VERSION:
        raise CleoRecoveryError(f"unsupported CLEO journal schema in {path}")


def _expect_string(document: JsonObject, key: str, path: Path) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value:
        raise CleoRecoveryError(f"journal field {key!r} must be a string: {path}")
    return value


def _expect_digest(document: JsonObject, key: str, path: Path) -> str:
    value = _expect_string(document, key, path)
    if len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise CleoRecoveryError(
            f"journal field {key!r} must be a SHA-256 digest: {path}"
        )
    return value


def _ensure_plain_directory(path: Path) -> None:
    if path.exists():
        if not path.is_dir() or path.is_symlink():
            raise CleoRecoveryError(f"path is not a plain directory: {path}")
        return
    path.mkdir(parents=True)
    _flush_directory(path)
    _flush_directory(path.parent)


def _remove_snapshot_files(path: Path) -> None:
    path.unlink(missing_ok=True)
    _remove_sqlite_sidecars(path)


def _remove_sqlite_sidecars(database_path: Path) -> None:
    for suffix in ("-journal", "-shm", "-wal"):
        database_path.with_name(f"{database_path.name}{suffix}").unlink(missing_ok=True)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _flush_file(path: Path) -> None:
    with path.open("r+b") as stream:
        os.fsync(stream.fileno())


def _flush_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


__all__ = [
    "CleoMutation",
    "CleoRecoveryError",
    "JournalConflictError",
    "JournalEntry",
    "JsonObject",
    "JsonValue",
    "MutationJournal",
    "PublicationRecord",
    "SQLiteSnapshot",
    "SQLiteSnapshotError",
    "SQLiteValidation",
    "create_sqlite_snapshot",
    "install_sqlite_snapshot",
    "normalize_json_object",
    "normalize_json_value",
    "validate_sqlite_database",
]
