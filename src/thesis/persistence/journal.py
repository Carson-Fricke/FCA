"""Append-only workspace command journal with a rebuildable publication outbox."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}")
_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_ZERO_HASH = "0" * 64
_SCHEMA_VERSION = 1


class JournalError(RuntimeError):
    """Base class for durable journal failures."""


class JournalIntegrityError(JournalError):
    """The durable journal does not match its append-only integrity chain."""


class IdempotencyConflict(JournalError):
    """An idempotency ID was reused for different accepted content."""


class ToolCorrelationConflict(JournalError):
    """A tool correlation ID was reused by another accepted command."""


def content_hash(value: bytes | str | object) -> str:
    """Return a stable SHA-256 hash for bytes, text, or canonical JSON data."""
    if isinstance(value, bytes):
        payload = value
    elif isinstance(value, str):
        payload = value.encode("utf-8")
    else:
        payload = _canonical_json(value).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True, order=True)
class ArtifactRef:
    """An immutable, content-addressed reference to a workspace artifact."""

    uri: str
    sha256: str
    size_bytes: int
    media_type: str = "application/octet-stream"

    def __post_init__(self) -> None:
        _validate_text(self.uri, "artifact URI", max_length=2048)
        _validate_digest(self.sha256, "artifact hash")
        if self.size_bytes < 0:
            raise ValueError("artifact size must not be negative")
        _validate_text(self.media_type, "artifact media type", max_length=255)

    @property
    def immutable_id(self) -> str:
        """Return the content identity independently of artifact location."""
        return f"sha256:{self.sha256}"

    @classmethod
    def from_bytes(
        cls,
        uri: str,
        content: bytes,
        *,
        media_type: str = "application/octet-stream",
    ) -> ArtifactRef:
        """Create a reference from artifact bytes without retaining the bytes."""
        return cls(
            uri=uri,
            sha256=content_hash(content),
            size_bytes=len(content),
            media_type=media_type,
        )

    @classmethod
    def from_path(
        cls,
        path: Path,
        *,
        uri: str | None = None,
        media_type: str = "application/octet-stream",
    ) -> ArtifactRef:
        """Hash an existing artifact and retain only its immutable reference."""
        content = path.read_bytes()
        return cls.from_bytes(
            path.as_posix() if uri is None else uri,
            content,
            media_type=media_type,
        )

    def verifies(self, path: Path) -> bool:
        """Return whether a local artifact still matches this reference."""
        if not path.is_file() or path.stat().st_size != self.size_bytes:
            return False
        return content_hash(path.read_bytes()) == self.sha256

    def as_dict(self) -> dict[str, object]:
        """Return the canonical publication representation."""
        return {
            "mediaType": self.media_type,
            "sha256": self.sha256,
            "sizeBytes": self.size_bytes,
            "uri": self.uri,
        }


@dataclass(frozen=True, slots=True)
class JournalEntry:
    """One immutable accepted command and its canonical resulting state."""

    sequence: int
    idempotency_id: str
    publication_id: str
    command_name: str
    command_json: str
    canonical_state_json: str
    workflow_hash: str
    model_hash: str
    input_hash: str
    artifacts: tuple[ArtifactRef, ...]
    tool_correlation_ids: tuple[str, ...]
    accepted_at: str
    fingerprint: str
    previous_hash: str
    journal_hash: str

    @property
    def command(self) -> Mapping[str, object]:
        """Return a detached copy of the accepted command payload."""
        return _decode_object(self.command_json, "command")

    @property
    def canonical_state(self) -> Mapping[str, object]:
        """Return a detached copy of the canonical accepted state."""
        return _decode_object(self.canonical_state_json, "canonical state")

    def manifest(self) -> dict[str, object]:
        """Build the canonical Git publication manifest for this entry."""
        return {
            "acceptedAt": self.accepted_at,
            "acceptedSequence": self.sequence,
            "artifacts": [artifact.as_dict() for artifact in self.artifacts],
            "canonicalState": dict(self.canonical_state),
            "command": {
                "name": self.command_name,
                "payload": dict(self.command),
            },
            "hashes": {
                "input": self.input_hash,
                "journal": self.journal_hash,
                "model": self.model_hash,
                "workflow": self.workflow_hash,
            },
            "idempotencyId": self.idempotency_id,
            "publicationId": self.publication_id,
            "schemaVersion": 1,
            "toolCorrelationIds": list(self.tool_correlation_ids),
        }

    def manifest_bytes(self) -> bytes:
        """Serialize the publication manifest deterministically."""
        return (_canonical_json(self.manifest()) + "\n").encode("utf-8")


@dataclass(frozen=True, slots=True)
class OutboxItem:
    """Current delivery state for a rebuildable publication request."""

    publication_id: str
    idempotency_id: str
    status: str
    attempts: int
    last_error: str | None
    updated_at: str


@dataclass(frozen=True, slots=True)
class PublicationReceipt:
    """Immutable link from an accepted publication to repository history."""

    publication_id: str
    commit_hash: str
    git_head_before: str | None
    published_at: str


class CommandJournal:
    """SQLite-backed accepted command journal and transactional publication outbox."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def accept(
        self,
        command_name: str,
        command: Mapping[str, object],
        canonical_state: Mapping[str, object],
        *,
        workflow_hash: str,
        model_hash: str,
        artifacts: Iterable[ArtifactRef] = (),
        tool_correlation_ids: Iterable[str] = (),
        idempotency_id: str | None = None,
    ) -> JournalEntry:
        """Durably accept a canonical mutation and enqueue one Git publication.

        Repeating identical content with the same explicit or generated ID returns
        the original entry. Reusing an ID for different content is rejected.
        """
        _validate_text(command_name, "command name", max_length=255)
        _validate_digest(workflow_hash, "workflow hash")
        _validate_digest(model_hash, "model hash")
        command_json = _canonical_json(dict(command))
        state_json = _canonical_json(dict(canonical_state))
        artifact_values = tuple(sorted(set(artifacts)))
        correlations = _normalize_correlations(tool_correlation_ids)
        input_hash = content_hash(command_json)
        material = _acceptance_material(
            command_name=command_name,
            command_json=command_json,
            canonical_state_json=state_json,
            workflow_hash=workflow_hash,
            model_hash=model_hash,
            input_hash=input_hash,
            artifacts=artifact_values,
            tool_correlation_ids=correlations,
        )
        fingerprint = content_hash(material)
        accepted_id = (
            f"cmd-{fingerprint[:48]}" if idempotency_id is None else idempotency_id
        )
        _validate_identifier(accepted_id, "idempotency ID")
        publication_id = f"pub-{content_hash(accepted_id)[:48]}"

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT * FROM accepted_commands WHERE idempotency_id = ?",
                    (accepted_id,),
                ).fetchone()
                if existing is not None:
                    if existing["fingerprint"] != fingerprint:
                        raise IdempotencyConflict(
                            f"idempotency ID {accepted_id!r} has different content"
                        )
                    self._ensure_outbox(connection, existing)
                    connection.commit()
                    return _entry_from_row(existing)

                self._check_correlations(connection, accepted_id, correlations)
                previous = connection.execute(
                    "SELECT sequence, journal_hash FROM accepted_commands "
                    "ORDER BY sequence DESC LIMIT 1"
                ).fetchone()
                sequence = 1 if previous is None else int(previous["sequence"]) + 1
                previous_hash = (
                    _ZERO_HASH if previous is None else str(previous["journal_hash"])
                )
                accepted_at = _timestamp()
                journal_hash = content_hash(
                    {
                        "acceptedAt": accepted_at,
                        "fingerprint": fingerprint,
                        "previousHash": previous_hash,
                        "sequence": sequence,
                    }
                )
                connection.execute(
                    """
                    INSERT INTO accepted_commands (
                        sequence, idempotency_id, publication_id, command_name,
                        command_json, canonical_state_json, workflow_hash,
                        model_hash, input_hash, artifacts_json,
                        tool_correlations_json, accepted_at, fingerprint,
                        previous_hash, journal_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        sequence,
                        accepted_id,
                        publication_id,
                        command_name,
                        command_json,
                        state_json,
                        workflow_hash,
                        model_hash,
                        input_hash,
                        _canonical_json(
                            [artifact.as_dict() for artifact in artifact_values]
                        ),
                        _canonical_json(list(correlations)),
                        accepted_at,
                        fingerprint,
                        previous_hash,
                        journal_hash,
                    ),
                )
                for correlation_id in correlations:
                    connection.execute(
                        "INSERT INTO tool_correlations "
                        "(correlation_id, idempotency_id) VALUES (?, ?)",
                        (correlation_id, accepted_id),
                    )
                connection.execute(
                    """
                    INSERT INTO publication_outbox (
                        publication_id, idempotency_id, status, attempts,
                        last_error, updated_at
                    ) VALUES (?, ?, 'pending', 0, NULL, ?)
                    """,
                    (publication_id, accepted_id, accepted_at),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        entry = self.get(accepted_id)
        if entry is None:
            raise JournalIntegrityError("accepted command disappeared after commit")
        return entry

    def get(self, idempotency_id: str) -> JournalEntry | None:
        """Return an accepted command by idempotency ID."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM accepted_commands WHERE idempotency_id = ?",
                (idempotency_id,),
            ).fetchone()
        return None if row is None else _entry_from_row(row)

    def get_publication(self, publication_id: str) -> JournalEntry | None:
        """Return an accepted command by publication ID."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM accepted_commands WHERE publication_id = ?",
                (publication_id,),
            ).fetchone()
        return None if row is None else _entry_from_row(row)

    def entries(self) -> tuple[JournalEntry, ...]:
        """Return all accepted entries in hash-chain order."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM accepted_commands ORDER BY sequence"
            ).fetchall()
        return tuple(_entry_from_row(row) for row in rows)

    def outbox(self) -> tuple[OutboxItem, ...]:
        """Return the current derived publication queue in acceptance order."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT o.* FROM publication_outbox AS o
                JOIN accepted_commands AS a USING (idempotency_id)
                ORDER BY a.sequence
                """
            ).fetchall()
        return tuple(_outbox_from_row(row) for row in rows)

    def receipts(self) -> tuple[PublicationReceipt, ...]:
        """Return immutable publication receipts ordered by accepted state."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT r.* FROM publication_receipts AS r
                JOIN accepted_commands AS a USING (publication_id)
                ORDER BY a.sequence
                """
            ).fetchall()
        return tuple(_receipt_from_row(row) for row in rows)

    def claim_next(self) -> JournalEntry | None:
        """Atomically fence one pending outbox row for publication."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT a.* FROM publication_outbox AS o
                    JOIN accepted_commands AS a USING (idempotency_id)
                    WHERE o.status = 'pending'
                    ORDER BY a.sequence
                    LIMIT 1
                    """
                ).fetchone()
                if row is None:
                    connection.commit()
                    return None
                changed = connection.execute(
                    """
                    UPDATE publication_outbox
                    SET status = 'in_flight', attempts = attempts + 1,
                        last_error = NULL, updated_at = ?
                    WHERE publication_id = ? AND status = 'pending'
                    """,
                    (_timestamp(), row["publication_id"]),
                ).rowcount
                if changed != 1:
                    raise JournalIntegrityError("outbox claim lost its write fence")
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return _entry_from_row(row)

    def release(self, publication_id: str, error: BaseException | str) -> None:
        """Return a failed in-flight publication to the pending queue."""
        message = str(error).strip() or type(error).__name__
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE publication_outbox
                SET status = 'pending', last_error = ?, updated_at = ?
                WHERE publication_id = ?
                """,
                (message[:2048], _timestamp(), publication_id),
            )

    def record_published(
        self,
        publication_id: str,
        commit_hash: str,
        *,
        git_head_before: str | None,
    ) -> PublicationReceipt:
        """Atomically persist an immutable Git receipt and retire its queue row."""
        _validate_identifier(publication_id, "publication ID")
        _validate_commit(commit_hash)
        if git_head_before is not None:
            _validate_commit(git_head_before)
        published_at = _timestamp()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                accepted = connection.execute(
                    "SELECT 1 FROM accepted_commands WHERE publication_id = ?",
                    (publication_id,),
                ).fetchone()
                if accepted is None:
                    raise JournalIntegrityError(
                        f"publication {publication_id!r} has no accepted command"
                    )
                existing = connection.execute(
                    "SELECT * FROM publication_receipts WHERE publication_id = ?",
                    (publication_id,),
                ).fetchone()
                if existing is not None:
                    receipt = _receipt_from_row(existing)
                    if (
                        receipt.commit_hash != commit_hash
                        or receipt.git_head_before != git_head_before
                    ):
                        raise JournalIntegrityError(
                            f"publication {publication_id!r} has a conflicting receipt"
                        )
                    connection.execute(
                        "DELETE FROM publication_outbox WHERE publication_id = ?",
                        (publication_id,),
                    )
                    connection.commit()
                    return receipt
                try:
                    connection.execute(
                        """
                        INSERT INTO publication_receipts (
                            publication_id, commit_hash, git_head_before, published_at
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (
                            publication_id,
                            commit_hash,
                            git_head_before,
                            published_at,
                        ),
                    )
                except sqlite3.IntegrityError as error:
                    raise JournalIntegrityError(
                        f"Git commit {commit_hash!r} is already assigned"
                    ) from error
                connection.execute(
                    "DELETE FROM publication_outbox WHERE publication_id = ?",
                    (publication_id,),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return PublicationReceipt(
            publication_id=publication_id,
            commit_hash=commit_hash,
            git_head_before=git_head_before,
            published_at=published_at,
        )

    def rebuild_outbox(self) -> tuple[OutboxItem, ...]:
        """Rebuild queue state solely from accepted entries and Git receipts."""
        now = _timestamp()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute("DELETE FROM publication_outbox")
                connection.execute(
                    """
                    INSERT INTO publication_outbox (
                        publication_id, idempotency_id, status, attempts,
                        last_error, updated_at
                    )
                    SELECT a.publication_id, a.idempotency_id, 'pending', 0, NULL, ?
                    FROM accepted_commands AS a
                    LEFT JOIN publication_receipts AS r USING (publication_id)
                    WHERE r.publication_id IS NULL
                    ORDER BY a.sequence
                    """,
                    (now,),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return self.outbox()

    def verify_integrity(self) -> None:
        """Validate SQLite, the append-only hash chain, and correlation indexes."""
        with self._connect() as connection:
            database_check = connection.execute("PRAGMA integrity_check").fetchone()
            if database_check is None or database_check[0] != "ok":
                raise JournalIntegrityError("SQLite integrity check failed")
            rows = connection.execute(
                "SELECT * FROM accepted_commands ORDER BY sequence"
            ).fetchall()
            correlations = connection.execute(
                "SELECT correlation_id, idempotency_id FROM tool_correlations"
            ).fetchall()
            receipt_rows = connection.execute(
                "SELECT * FROM publication_receipts"
            ).fetchall()

        previous_hash = _ZERO_HASH
        expected_correlations: set[tuple[str, str]] = set()
        for expected_sequence, row in enumerate(rows, start=1):
            entry = _entry_from_row(row)
            if entry.sequence != expected_sequence:
                raise JournalIntegrityError("journal sequence contains a gap")
            material = _acceptance_material(
                command_name=entry.command_name,
                command_json=entry.command_json,
                canonical_state_json=entry.canonical_state_json,
                workflow_hash=entry.workflow_hash,
                model_hash=entry.model_hash,
                input_hash=entry.input_hash,
                artifacts=entry.artifacts,
                tool_correlation_ids=entry.tool_correlation_ids,
            )
            if content_hash(material) != entry.fingerprint:
                raise JournalIntegrityError(
                    f"journal entry {entry.sequence} fingerprint is invalid"
                )
            if content_hash(entry.command_json) != entry.input_hash:
                raise JournalIntegrityError(
                    f"journal entry {entry.sequence} input hash is invalid"
                )
            if entry.previous_hash != previous_hash:
                raise JournalIntegrityError(
                    f"journal entry {entry.sequence} breaks the hash chain"
                )
            expected_hash = content_hash(
                {
                    "acceptedAt": entry.accepted_at,
                    "fingerprint": entry.fingerprint,
                    "previousHash": entry.previous_hash,
                    "sequence": entry.sequence,
                }
            )
            if entry.journal_hash != expected_hash:
                raise JournalIntegrityError(
                    f"journal entry {entry.sequence} hash is invalid"
                )
            previous_hash = entry.journal_hash
            expected_correlations.update(
                (correlation_id, entry.idempotency_id)
                for correlation_id in entry.tool_correlation_ids
            )

        actual_correlations = {
            (str(row["correlation_id"]), str(row["idempotency_id"]))
            for row in correlations
        }
        if actual_correlations != expected_correlations:
            raise JournalIntegrityError("tool correlation index disagrees with journal")
        for row in receipt_rows:
            receipt = _receipt_from_row(row)
            _validate_commit(receipt.commit_hash)
            if receipt.git_head_before is not None:
                _validate_commit(receipt.git_head_before)

    def _initialize(self) -> None:
        with self._connect() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version not in {0, _SCHEMA_VERSION}:
                raise JournalIntegrityError(
                    f"unsupported journal schema version {version}"
                )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS accepted_commands (
                    sequence INTEGER PRIMARY KEY,
                    idempotency_id TEXT NOT NULL UNIQUE,
                    publication_id TEXT NOT NULL UNIQUE,
                    command_name TEXT NOT NULL,
                    command_json TEXT NOT NULL,
                    canonical_state_json TEXT NOT NULL,
                    workflow_hash TEXT NOT NULL,
                    model_hash TEXT NOT NULL,
                    input_hash TEXT NOT NULL,
                    artifacts_json TEXT NOT NULL,
                    tool_correlations_json TEXT NOT NULL,
                    accepted_at TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    journal_hash TEXT NOT NULL UNIQUE
                );

                CREATE TABLE IF NOT EXISTS tool_correlations (
                    correlation_id TEXT PRIMARY KEY,
                    idempotency_id TEXT NOT NULL,
                    FOREIGN KEY (idempotency_id)
                        REFERENCES accepted_commands (idempotency_id)
                );

                CREATE TABLE IF NOT EXISTS publication_receipts (
                    publication_id TEXT PRIMARY KEY,
                    commit_hash TEXT NOT NULL UNIQUE,
                    git_head_before TEXT,
                    published_at TEXT NOT NULL,
                    FOREIGN KEY (publication_id)
                        REFERENCES accepted_commands (publication_id)
                );

                CREATE TABLE IF NOT EXISTS publication_outbox (
                    publication_id TEXT PRIMARY KEY,
                    idempotency_id TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL CHECK (status IN ('pending', 'in_flight')),
                    attempts INTEGER NOT NULL CHECK (attempts >= 0),
                    last_error TEXT,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (publication_id)
                        REFERENCES accepted_commands (publication_id),
                    FOREIGN KEY (idempotency_id)
                        REFERENCES accepted_commands (idempotency_id)
                );

                CREATE TRIGGER IF NOT EXISTS accepted_commands_no_update
                BEFORE UPDATE ON accepted_commands
                BEGIN
                    SELECT RAISE(ABORT, 'accepted commands are immutable');
                END;

                CREATE TRIGGER IF NOT EXISTS accepted_commands_no_delete
                BEFORE DELETE ON accepted_commands
                BEGIN
                    SELECT RAISE(ABORT, 'accepted commands are immutable');
                END;

                CREATE TRIGGER IF NOT EXISTS tool_correlations_no_update
                BEFORE UPDATE ON tool_correlations
                BEGIN
                    SELECT RAISE(ABORT, 'tool correlations are immutable');
                END;

                CREATE TRIGGER IF NOT EXISTS tool_correlations_no_delete
                BEFORE DELETE ON tool_correlations
                BEGIN
                    SELECT RAISE(ABORT, 'tool correlations are immutable');
                END;

                CREATE TRIGGER IF NOT EXISTS publication_receipts_no_update
                BEFORE UPDATE ON publication_receipts
                BEGIN
                    SELECT RAISE(ABORT, 'publication receipts are immutable');
                END;

                CREATE TRIGGER IF NOT EXISTS publication_receipts_no_delete
                BEFORE DELETE ON publication_receipts
                BEGIN
                    SELECT RAISE(ABORT, 'publication receipts are immutable');
                END;
                """
            )
            connection.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @staticmethod
    def _check_correlations(
        connection: sqlite3.Connection,
        idempotency_id: str,
        correlations: tuple[str, ...],
    ) -> None:
        for correlation_id in correlations:
            row = connection.execute(
                "SELECT idempotency_id FROM tool_correlations WHERE correlation_id = ?",
                (correlation_id,),
            ).fetchone()
            if row is not None and row["idempotency_id"] != idempotency_id:
                raise ToolCorrelationConflict(
                    f"tool correlation ID {correlation_id!r} is already accepted"
                )

    @staticmethod
    def _ensure_outbox(connection: sqlite3.Connection, accepted: sqlite3.Row) -> None:
        receipt = connection.execute(
            "SELECT 1 FROM publication_receipts WHERE publication_id = ?",
            (accepted["publication_id"],),
        ).fetchone()
        if receipt is None:
            connection.execute(
                """
                INSERT OR IGNORE INTO publication_outbox (
                    publication_id, idempotency_id, status, attempts,
                    last_error, updated_at
                ) VALUES (?, ?, 'pending', 0, NULL, ?)
                """,
                (
                    accepted["publication_id"],
                    accepted["idempotency_id"],
                    _timestamp(),
                ),
            )


def _acceptance_material(
    *,
    command_name: str,
    command_json: str,
    canonical_state_json: str,
    workflow_hash: str,
    model_hash: str,
    input_hash: str,
    artifacts: tuple[ArtifactRef, ...],
    tool_correlation_ids: tuple[str, ...],
) -> dict[str, object]:
    return {
        "artifacts": [artifact.as_dict() for artifact in artifacts],
        "canonicalState": json.loads(canonical_state_json),
        "command": {
            "name": command_name,
            "payload": json.loads(command_json),
        },
        "hashes": {
            "input": input_hash,
            "model": model_hash,
            "workflow": workflow_hash,
        },
        "toolCorrelationIds": list(tool_correlation_ids),
    }


def _entry_from_row(row: sqlite3.Row) -> JournalEntry:
    artifacts_value = json.loads(str(row["artifacts_json"]))
    correlations_value = json.loads(str(row["tool_correlations_json"]))
    if not isinstance(artifacts_value, list) or not isinstance(
        correlations_value, list
    ):
        raise JournalIntegrityError("journal collection fields must be JSON arrays")
    artifacts: list[ArtifactRef] = []
    for value in artifacts_value:
        if not isinstance(value, dict):
            raise JournalIntegrityError("artifact reference must be a JSON object")
        try:
            artifacts.append(
                ArtifactRef(
                    uri=str(value["uri"]),
                    sha256=str(value["sha256"]),
                    size_bytes=int(value["sizeBytes"]),
                    media_type=str(value["mediaType"]),
                )
            )
        except (KeyError, TypeError, ValueError) as error:
            raise JournalIntegrityError("artifact reference is invalid") from error
    if not all(isinstance(value, str) for value in correlations_value):
        raise JournalIntegrityError("tool correlation IDs must be strings")
    return JournalEntry(
        sequence=int(row["sequence"]),
        idempotency_id=str(row["idempotency_id"]),
        publication_id=str(row["publication_id"]),
        command_name=str(row["command_name"]),
        command_json=str(row["command_json"]),
        canonical_state_json=str(row["canonical_state_json"]),
        workflow_hash=str(row["workflow_hash"]),
        model_hash=str(row["model_hash"]),
        input_hash=str(row["input_hash"]),
        artifacts=tuple(artifacts),
        tool_correlation_ids=cast(tuple[str, ...], tuple(correlations_value)),
        accepted_at=str(row["accepted_at"]),
        fingerprint=str(row["fingerprint"]),
        previous_hash=str(row["previous_hash"]),
        journal_hash=str(row["journal_hash"]),
    )


def _outbox_from_row(row: sqlite3.Row) -> OutboxItem:
    return OutboxItem(
        publication_id=str(row["publication_id"]),
        idempotency_id=str(row["idempotency_id"]),
        status=str(row["status"]),
        attempts=int(row["attempts"]),
        last_error=None if row["last_error"] is None else str(row["last_error"]),
        updated_at=str(row["updated_at"]),
    )


def _receipt_from_row(row: sqlite3.Row) -> PublicationReceipt:
    return PublicationReceipt(
        publication_id=str(row["publication_id"]),
        commit_hash=str(row["commit_hash"]),
        git_head_before=(
            None if row["git_head_before"] is None else str(row["git_head_before"])
        ),
        published_at=str(row["published_at"]),
    )


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise ValueError("workspace values must be canonical JSON") from error


def _decode_object(value: str, name: str) -> Mapping[str, object]:
    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        raise JournalIntegrityError(f"{name} must be a JSON object")
    return cast(Mapping[str, object], decoded)


def _normalize_correlations(values: Iterable[str]) -> tuple[str, ...]:
    normalized = tuple(sorted(set(values)))
    for value in normalized:
        _validate_text(value, "tool correlation ID", max_length=255)
    return normalized


def _validate_digest(value: str, name: str) -> None:
    if _DIGEST_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 hash")


def _validate_commit(value: str) -> None:
    if _COMMIT_PATTERN.fullmatch(value) is None:
        raise ValueError("Git commit must be a lowercase SHA-1 or SHA-256 object ID")


def _validate_identifier(value: str, name: str) -> None:
    _validate_text(value, name, max_length=128)
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value) is None:
        raise ValueError(f"{name} contains unsupported characters")


def _validate_text(value: str, name: str, *, max_length: int) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must not be empty")
    if len(value) > max_length or any(ord(character) < 32 for character in value):
        raise ValueError(f"{name} contains unsupported characters")


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


__all__ = [
    "ArtifactRef",
    "CommandJournal",
    "IdempotencyConflict",
    "JournalEntry",
    "JournalError",
    "JournalIntegrityError",
    "OutboxItem",
    "PublicationReceipt",
    "ToolCorrelationConflict",
    "content_hash",
]
