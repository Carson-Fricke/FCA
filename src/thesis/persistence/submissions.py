"""Immutable submission references; this module neither dispatches nor polls.

The caller must retain the exact submitted inputs before crossing an external
mutation boundary. A lost acknowledgement leaves an inspectable job identity,
never authority to replay it. Store conflicts are attribution errors, not
harness admission decisions.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol


class SubmissionStore(Protocol):
    """Create-only persistence shared by HTTP, conversation, and workflow callers."""

    def retain(
        self,
        workspace_id: str,
        job_id: str,
        operation: str,
        inputs: Mapping[str, object],
    ) -> None: ...

    def get(self, workspace_id: str, job_id: str) -> dict[str, object] | None: ...

    def find(self, job_id: str) -> dict[str, object] | None:
        """Resolve a globally unique retained ID for authenticated completion intake."""
        ...

    def list(self, workspace_id: str) -> tuple[dict[str, object], ...]: ...


def submission_document(
    workspace_id: str, job_id: str, operation: str, inputs: Mapping[str, object]
) -> dict[str, object]:
    """Detach caller-owned inputs and give their canonical JSON a content identity."""
    if not all(value.strip() for value in (workspace_id, job_id, operation)):
        raise ValueError("submission scope, job identity, and operation are required")
    payload = json.dumps(inputs, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return {
        "workspace_id": workspace_id,
        "job_id": job_id,
        "operation": operation,
        "inputs": json.loads(payload),
        "input_sha256": hashlib.sha256(payload.encode()).hexdigest(),
        "retained_at": datetime.now(UTC).isoformat(),
    }


def require_same_submission(
    previous: Mapping[str, object], current: Mapping[str, object]
) -> None:
    """Never replace the attribution of an already-retained execution identity."""
    for key in ("workspace_id", "job_id", "operation", "inputs", "input_sha256"):
        if previous.get(key) != current.get(key):
            raise ValueError(f"submission identity conflict: {current['job_id']}")


class SqliteSubmissionStore:
    """Local durable journal with committed, create-only job references."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS submissions "
                "(job_id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, payload TEXT NOT NULL)"
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def retain(
        self,
        workspace_id: str,
        job_id: str,
        operation: str,
        inputs: Mapping[str, object],
    ) -> None:
        document = submission_document(workspace_id, job_id, operation, inputs)
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO submissions VALUES (?, ?, ?)",
                (job_id, workspace_id, json.dumps(document, allow_nan=False)),
            )
            row = connection.execute(
                "SELECT payload FROM submissions WHERE job_id = ?", (job_id,)
            ).fetchone()
            require_same_submission(json.loads(row[0]), document)

    def get(self, workspace_id: str, job_id: str) -> dict[str, object] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM submissions WHERE job_id = ? AND workspace_id = ?",
                (job_id, workspace_id),
            ).fetchone()
        return None if row is None else json.loads(row[0])

    def list(self, workspace_id: str) -> tuple[dict[str, object], ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload FROM submissions WHERE workspace_id = ? ORDER BY rowid DESC",
                (workspace_id,),
            ).fetchall()
        return tuple(json.loads(row[0]) for row in rows)

    def find(self, job_id: str) -> dict[str, object] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload FROM submissions WHERE job_id = ?", (job_id,)
            ).fetchone()
        return None if row is None else json.loads(row[0])


def retain_submission(
    store: SubmissionStore | None,
    workspace_id: str,
    job_id: str,
    operation: str,
    inputs: Mapping[str, object],
) -> None:
    """Fail closed when composition omitted durable submission persistence."""
    if store is None:
        raise RuntimeError("durable submission persistence is not configured")
    store.retain(workspace_id, job_id, operation, inputs)
