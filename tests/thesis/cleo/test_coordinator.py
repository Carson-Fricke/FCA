from __future__ import annotations

import asyncio
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from thesis.cleo import (
    CleoCoordinator,
    CleoMutation,
    CleoMutationExecutor,
    CleoOwnershipError,
    CleoProfileError,
    CleoPublication,
    CleoPublisher,
    JournalConflictError,
    SQLiteSnapshotError,
    WorkspaceCleoProfile,
    create_sqlite_snapshot,
    validate_sqlite_database,
)
from thesis.cleo.recovery import JsonValue


class SQLiteInitializer:
    def initialize(self, database_path: Path, *, selected_plan: str) -> None:
        with closing(sqlite3.connect(database_path)) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                "CREATE TABLE tasks (position INTEGER PRIMARY KEY, title TEXT NOT NULL)"
            )
            connection.execute(
                "INSERT INTO tasks (position, title) VALUES (0, ?)",
                (f"seed:{selected_plan}",),
            )
            connection.commit()


class RecordingExecutor(CleoMutationExecutor):
    def __init__(self) -> None:
        self.calls: list[str] = []

    def apply(
        self,
        database_path: Path,
        mutation: CleoMutation,
    ) -> JsonValue:
        self.calls.append(mutation.idempotency_key)
        position = mutation.arguments["position"]
        title = mutation.arguments["title"]
        assert isinstance(position, int)
        assert isinstance(title, str)
        with closing(sqlite3.connect(database_path)) as connection:
            connection.execute(
                "INSERT INTO tasks (position, title) VALUES (?, ?)",
                (position, title),
            )
            connection.commit()
        return {"position": position, "title": title}


class RecordingPublisher(CleoPublisher):
    def __init__(self) -> None:
        self.calls: list[CleoPublication] = []
        self.side_effects: dict[str, JsonValue] = {}
        self.fail_after_first_side_effect = False

    def publish(self, publication: CleoPublication) -> JsonValue:
        self.calls.append(publication)
        existing = self.side_effects.get(publication.idempotency_key)
        if existing is not None:
            return existing
        assert publication.database_path.name == "cleo.db"
        validate_sqlite_database(publication.database_path)
        receipt: JsonValue = {
            "commit": f"commit-{len(self.side_effects) + 1}",
            "fence": publication.fence_token,
        }
        self.side_effects[publication.idempotency_key] = receipt
        if self.fail_after_first_side_effect:
            self.fail_after_first_side_effect = False
            raise RuntimeError("process stopped after publication")
        return receipt


class FailAfterStagingCommit(CleoMutationExecutor):
    def apply(
        self,
        database_path: Path,
        mutation: CleoMutation,
    ) -> JsonValue:
        with closing(sqlite3.connect(database_path)) as connection:
            connection.execute(
                "INSERT INTO tasks (position, title) VALUES (?, ?)",
                (mutation.arguments["position"], mutation.arguments["title"]),
            )
            connection.commit()
        raise RuntimeError("staging process stopped")


def test_profile_initializes_fresh_project_and_deny_by_default_policy(
    tmp_path: Path,
) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first = WorkspaceCleoProfile(first_root)
    second = WorkspaceCleoProfile(second_root)

    initialized = first.initialize(
        selected_plan="research-v1",
        initializer=SQLiteInitializer(),
    )
    retried = first.initialize(
        selected_plan="research-v1",
        initializer=SQLiteInitializer(),
    )
    second.initialize(
        selected_plan="implementation-v2",
        initializer=SQLiteInitializer(),
    )

    assert initialized.created is True
    assert retried.created is False
    assert first.selected_plan() == "research-v1"
    assert second.selected_plan() == "implementation-v2"
    assert initialized.database.path == first.database_path
    assert first.publication_paths == (first.database_path,)
    assert first.ignore_policy_path.read_text(encoding="ascii") == (
        "# The canonical database is the only publishable CLEO state.\n"
        "*\n"
        "!.gitignore\n"
        "!cleo.db\n"
    )
    assert first.journal_directory.is_relative_to(first_root / ".thesis")
    assert first.backup_directory.is_relative_to(first.cleo_directory)
    assert first.runtime_directory.is_relative_to(first.cleo_directory)
    assert first.database_path.read_bytes() != second.database_path.read_bytes()

    with pytest.raises(CleoProfileError, match="already uses plan"):
        first.initialize(
            selected_plan="different-plan",
            initializer=SQLiteInitializer(),
        )


def test_safe_snapshot_includes_wal_and_rejects_corruption(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    snapshot_path = tmp_path / "snapshots" / "snapshot.db"
    snapshot_path.parent.mkdir()
    writer = sqlite3.connect(source)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE values_table (value TEXT NOT NULL)")
        writer.execute("INSERT INTO values_table VALUES ('in-wal')")
        writer.commit()
        snapshot = create_sqlite_snapshot(source, snapshot_path)
    finally:
        writer.close()

    assert snapshot.sha256
    assert snapshot.validation.integrity_messages == ("ok",)
    assert not snapshot_path.with_name("snapshot.db-wal").exists()
    assert not snapshot_path.with_name("snapshot.db-shm").exists()
    with closing(
        sqlite3.connect(f"{snapshot_path.as_uri()}?mode=ro", uri=True)
    ) as connection:
        assert connection.execute("SELECT value FROM values_table").fetchone() == (
            "in-wal",
        )

    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"not sqlite")
    with pytest.raises(SQLiteSnapshotError, match="validation failed"):
        validate_sqlite_database(corrupt)


def test_concurrent_proposals_are_serialized_and_duplicates_apply_once(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        profile = _initialized_profile(tmp_path)
        executor = RecordingExecutor()
        publisher = RecordingPublisher()
        mutations = [
            CleoMutation(
                f"proposal-{position}",
                "add-task",
                {"position": position, "title": f"task-{position}"},
            )
            for position in range(1, 5)
        ]
        duplicate = CleoMutation(
            "proposal-2",
            "add-task",
            {"title": "task-2", "position": 2},
        )

        async with CleoCoordinator(
            profile=profile,
            executor=executor,
            publisher=publisher,
        ) as coordinator:
            results = await asyncio.gather(
                *(coordinator.submit(mutation) for mutation in [*mutations, duplicate])
            )

        assert executor.calls == [f"proposal-{position}" for position in range(1, 5)]
        assert len(publisher.calls) == 4
        assert [result.applied for result in results] == [True, True, True, True, False]
        assert all(result.published for result in results)
        assert len({call.fence_token for call in publisher.calls}) == 1
        assert not profile.database_path.with_name("cleo.db-wal").exists()
        assert not profile.database_path.with_name("cleo.db-shm").exists()
        assert not profile.database_path.with_name("cleo.db-journal").exists()
        with closing(sqlite3.connect(profile.database_path)) as connection:
            rows = connection.execute(
                "SELECT position, title FROM tasks ORDER BY position"
            ).fetchall()
        assert rows == [
            (0, "seed:research-v1"),
            (1, "task-1"),
            (2, "task-2"),
            (3, "task-3"),
            (4, "task-4"),
        ]

    asyncio.run(scenario())


def test_conflicting_idempotency_key_is_rejected_without_second_mutation(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        profile = _initialized_profile(tmp_path)
        executor = RecordingExecutor()
        publisher = RecordingPublisher()
        async with CleoCoordinator(
            profile=profile,
            executor=executor,
            publisher=publisher,
        ) as coordinator:
            await coordinator.submit(
                CleoMutation(
                    "same-key",
                    "add-task",
                    {"position": 1, "title": "original"},
                )
            )
            with pytest.raises(JournalConflictError, match="different CLEO mutation"):
                await coordinator.submit(
                    CleoMutation(
                        "same-key",
                        "add-task",
                        {"position": 2, "title": "conflict"},
                    )
                )

        assert executor.calls == ["same-key"]
        assert len(publisher.calls) == 1

    asyncio.run(scenario())


def test_startup_recovers_publication_without_replaying_logical_mutation(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        profile = _initialized_profile(tmp_path)
        executor = RecordingExecutor()
        publisher = RecordingPublisher()
        publisher.fail_after_first_side_effect = True
        mutation = CleoMutation(
            "crash-key",
            "add-task",
            {"position": 1, "title": "survives"},
        )

        coordinator = CleoCoordinator(
            profile=profile,
            executor=executor,
            publisher=publisher,
        )
        await coordinator.start()
        with pytest.raises(RuntimeError, match="stopped after publication"):
            await coordinator.submit(mutation)
        await coordinator.close()

        recovered = CleoCoordinator(
            profile=profile,
            executor=executor,
            publisher=publisher,
        )
        recovery_results = await recovered.start()
        retry_result = await recovered.submit(mutation)
        await recovered.close()

        assert executor.calls == ["crash-key"]
        assert len(publisher.calls) == 2
        assert len(publisher.side_effects) == 1
        assert len(recovery_results) == 1
        assert recovery_results[0].applied is False
        assert recovery_results[0].published is True
        assert retry_result.applied is False
        assert retry_result.publication_receipt == {"commit": "commit-1", "fence": 1}
        with closing(sqlite3.connect(profile.database_path)) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM tasks WHERE title = 'survives'"
            ).fetchone() == (1,)

    asyncio.run(scenario())


def test_failed_staging_mutation_never_partially_updates_canonical_database(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        profile = _initialized_profile(tmp_path)
        mutation = CleoMutation(
            "staging-crash",
            "add-task",
            {"position": 1, "title": "atomic"},
        )
        failed = CleoCoordinator(
            profile=profile,
            executor=FailAfterStagingCommit(),
            publisher=RecordingPublisher(),
        )
        await failed.start()
        with pytest.raises(RuntimeError, match="staging process stopped"):
            await failed.submit(mutation)
        await failed.close()

        with closing(sqlite3.connect(profile.database_path)) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM tasks WHERE title = 'atomic'"
            ).fetchone() == (0,)
        assert not tuple(profile.snapshot_directory.glob("mutation-*.db"))

        executor = RecordingExecutor()
        recovered = CleoCoordinator(
            profile=profile,
            executor=executor,
            publisher=RecordingPublisher(),
        )
        results = await recovered.start()
        await recovered.close()
        assert executor.calls == ["staging-crash"]
        assert len(results) == 1
        assert results[0].applied is True
        with closing(sqlite3.connect(profile.database_path)) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM tasks WHERE title = 'atomic'"
            ).fetchone() == (1,)

    asyncio.run(scenario())


def test_only_one_coordinator_can_own_canonical_database(tmp_path: Path) -> None:
    async def scenario() -> None:
        profile = _initialized_profile(tmp_path)
        first = CleoCoordinator(
            profile=profile,
            executor=RecordingExecutor(),
            publisher=RecordingPublisher(),
        )
        second = CleoCoordinator(
            profile=profile,
            executor=RecordingExecutor(),
            publisher=RecordingPublisher(),
        )
        await first.start()
        assert first.fence_token == 1
        with pytest.raises(CleoOwnershipError, match="already has a writer"):
            await second.start()
        await first.close()

        await second.start()
        assert second.fence_token == 2
        await second.close()

    asyncio.run(scenario())


def test_snapshot_does_not_copy_unrelated_runtime_or_backup_files(
    tmp_path: Path,
) -> None:
    profile = _initialized_profile(tmp_path)
    (profile.runtime_directory / "worker.pid").write_text("123", encoding="ascii")
    (profile.backup_directory / "old.db").write_bytes(b"backup")
    profile.database_path.with_name("cleo.db-wal.fake").write_bytes(b"runtime")
    publication = tmp_path / "publication"
    publication.mkdir()

    for path in profile.publication_paths:
        shutil.copy2(path, publication / path.name)

    assert sorted(path.name for path in publication.iterdir()) == ["cleo.db"]


def _initialized_profile(tmp_path: Path) -> WorkspaceCleoProfile:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    profile = WorkspaceCleoProfile(workspace)
    profile.initialize(
        selected_plan="research-v1",
        initializer=SQLiteInitializer(),
    )
    return profile
