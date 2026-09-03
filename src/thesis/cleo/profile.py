"""Per-workspace CLEO layout and canonical database policy."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import uuid
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from thesis.cleo.recovery import (
    SQLiteSnapshot,
    SQLiteSnapshotError,
    install_sqlite_snapshot,
    validate_sqlite_database,
)

_PROFILE_TABLE = "_thesis_cleo_profile"
_PROFILE_SCHEMA_VERSION = 1
_GITIGNORE = """# The canonical database is the only publishable CLEO state.\n*
!.gitignore
!cleo.db
"""


class CleoProfileError(RuntimeError):
    """The workspace CLEO profile is missing, unsafe, or inconsistent."""


@runtime_checkable
class CleoProjectInitializer(Protocol):
    """Adapter that creates a fresh CLEO database for a selected plan."""

    def initialize(self, database_path: Path, *, selected_plan: str) -> None:
        """Create a new CLEO project at ``database_path`` without copying another."""


@dataclass(frozen=True, slots=True)
class InitializedCleoProject:
    """The validated canonical database and its selected workspace plan."""

    selected_plan: str
    database: SQLiteSnapshot
    created: bool


@dataclass(frozen=True, slots=True)
class WorkspaceCleoProfile:
    """Paths and publication policy for one isolated workspace CLEO project."""

    workspace_root: Path

    def __post_init__(self) -> None:
        root = self.workspace_root.resolve()
        if not root.is_dir():
            raise CleoProfileError(f"workspace root is not a directory: {root}")
        object.__setattr__(self, "workspace_root", root)

    @property
    def cleo_directory(self) -> Path:
        return self.workspace_root / ".cleo"

    @property
    def database_path(self) -> Path:
        """Return the sole canonical workspace CLEO database path."""
        return self.cleo_directory / "cleo.db"

    @property
    def ignore_policy_path(self) -> Path:
        return self.cleo_directory / ".gitignore"

    @property
    def runtime_directory(self) -> Path:
        return self.cleo_directory / "runtime"

    @property
    def snapshot_directory(self) -> Path:
        return self.runtime_directory / "snapshots"

    @property
    def backup_directory(self) -> Path:
        return self.cleo_directory / "backups"

    @property
    def lock_path(self) -> Path:
        return self.runtime_directory / "coordinator.lock"

    @property
    def fence_path(self) -> Path:
        return self.runtime_directory / "coordinator.fence"

    @property
    def journal_directory(self) -> Path:
        """Return the portable workspace journal, outside CLEO runtime state."""
        return self.workspace_root / ".thesis" / "journal" / "cleo"

    @property
    def publication_paths(self) -> tuple[Path, ...]:
        """Return CLEO-owned files allowed in a workspace publication."""
        return (self.database_path,)

    def prepare(self) -> None:
        """Install the deny-by-default CLEO ignore policy and runtime layout."""
        _ensure_workspace_directory(self.cleo_directory, root=self.workspace_root)
        _ensure_workspace_directory(self.runtime_directory, root=self.workspace_root)
        _ensure_workspace_directory(self.snapshot_directory, root=self.workspace_root)
        _ensure_workspace_directory(self.backup_directory, root=self.workspace_root)
        _ensure_workspace_directory(self.journal_directory, root=self.workspace_root)
        if self.ignore_policy_path.exists():
            if (
                self.ignore_policy_path.is_symlink()
                or not self.ignore_policy_path.is_file()
            ):
                raise CleoProfileError(
                    f"CLEO ignore policy is not a plain file: {self.ignore_policy_path}"
                )
            existing = self.ignore_policy_path.read_text(encoding="ascii")
            if existing != _GITIGNORE:
                raise CleoProfileError(
                    f"workspace has a conflicting CLEO ignore policy: {self.ignore_policy_path}"
                )
            return
        _write_new_file(self.ignore_policy_path, _GITIGNORE.encode("ascii"))

    def initialize(
        self,
        *,
        selected_plan: str,
        initializer: CleoProjectInitializer,
    ) -> InitializedCleoProject:
        """Create an independent project, safely retrying an identical initialization."""
        plan = selected_plan.strip()
        if not plan:
            raise ValueError("selected_plan must not be empty")
        self.prepare()
        if self.database_path.exists():
            existing_plan = self.selected_plan()
            if existing_plan != plan:
                raise CleoProfileError(
                    f"workspace CLEO project already uses plan {existing_plan!r}, not {plan!r}"
                )
            validation = validate_sqlite_database(self.database_path)
            return InitializedCleoProject(
                selected_plan=plan,
                database=SQLiteSnapshot(
                    path=self.database_path,
                    sha256=_database_sha256(self.database_path),
                    size_bytes=self.database_path.stat().st_size,
                    validation=validation,
                ),
                created=False,
            )

        staging_path = self.snapshot_directory / f"initialize-{uuid.uuid4().hex}.db"
        try:
            initializer.initialize(staging_path, selected_plan=plan)
            if not staging_path.is_file() or staging_path.is_symlink():
                raise CleoProfileError(
                    "CLEO initializer did not create a plain SQLite database"
                )
            _record_profile(staging_path, selected_plan=plan)
            snapshot = install_sqlite_snapshot(
                staging_path,
                self.database_path,
                temporary_directory=self.snapshot_directory,
            )
        except SQLiteSnapshotError as error:
            raise CleoProfileError(
                f"CLEO project initialization failed: {error}"
            ) from error
        finally:
            _remove_database_files(staging_path)
        return InitializedCleoProject(
            selected_plan=plan,
            database=snapshot,
            created=True,
        )

    def selected_plan(self) -> str:
        """Read the selected plan from the canonical project's profile metadata."""
        if not self.database_path.is_file() or self.database_path.is_symlink():
            raise CleoProfileError(
                f"canonical CLEO database is missing or unsafe: {self.database_path}"
            )
        try:
            with closing(
                sqlite3.connect(
                    f"{self.database_path.resolve().as_uri()}?mode=ro",
                    uri=True,
                )
            ) as connection:
                row = connection.execute(
                    f"SELECT selected_plan, schema_version FROM {_PROFILE_TABLE} WHERE id = 1"
                ).fetchone()
        except sqlite3.Error as error:
            raise CleoProfileError(
                "canonical database has no valid Thesis profile"
            ) from error
        if row is None or row[1] != _PROFILE_SCHEMA_VERSION:
            raise CleoProfileError(
                "canonical database has unsupported Thesis profile metadata"
            )
        return str(row[0])

    def assert_ready(self) -> None:
        """Validate the layout, profile metadata, and canonical SQLite database."""
        self.prepare()
        self.selected_plan()
        validate_sqlite_database(self.database_path)


def _record_profile(database_path: Path, *, selected_plan: str) -> None:
    try:
        with closing(sqlite3.connect(database_path)) as connection:
            connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {_PROFILE_TABLE} (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    schema_version INTEGER NOT NULL,
                    selected_plan TEXT NOT NULL
                )
                """
            )
            existing = connection.execute(
                f"SELECT schema_version, selected_plan FROM {_PROFILE_TABLE} WHERE id = 1"
            ).fetchone()
            if existing is None:
                connection.execute(
                    f"INSERT INTO {_PROFILE_TABLE} "
                    "(id, schema_version, selected_plan) VALUES (1, ?, ?)",
                    (_PROFILE_SCHEMA_VERSION, selected_plan),
                )
            elif existing != (_PROFILE_SCHEMA_VERSION, selected_plan):
                raise CleoProfileError(
                    "initializer created conflicting Thesis profile metadata"
                )
            connection.commit()
    except sqlite3.Error as error:
        raise CleoProfileError(
            "initializer did not create a valid SQLite database"
        ) from error


def _ensure_workspace_directory(path: Path, *, root: Path) -> None:
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise CleoProfileError(f"workspace path escapes its root: {path}")
    relative = path.relative_to(root)
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise CleoProfileError(f"workspace path contains a symlink: {current}")
    if path.exists():
        if path.is_symlink() or not path.is_dir():
            raise CleoProfileError(f"workspace path is not a plain directory: {path}")
        return
    try:
        path.mkdir(parents=True)
    except OSError as error:
        raise CleoProfileError(
            f"could not create workspace directory {path}: {error}"
        ) from error


def _write_new_file(path: Path, content: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _database_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _remove_database_files(path: Path) -> None:
    if path.exists():
        path.chmod(path.stat().st_mode | stat.S_IWRITE)
        path.unlink()
    for suffix in ("-journal", "-shm", "-wal"):
        path.with_name(f"{path.name}{suffix}").unlink(missing_ok=True)


__all__ = [
    "CleoProfileError",
    "CleoProjectInitializer",
    "InitializedCleoProject",
    "WorkspaceCleoProfile",
]
