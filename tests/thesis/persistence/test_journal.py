from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from thesis.persistence import (
    ArtifactRef,
    CommandJournal,
    IdempotencyConflict,
    JournalIntegrityError,
    ToolCorrelationConflict,
    content_hash,
)

WORKFLOW_HASH = content_hash("workflow-v1")
MODEL_HASH = content_hash("model-v1")


def _accept(
    journal: CommandJournal,
    *,
    idempotency_id: str | None = "command-1",
    correlation_id: str = "tool-call-1",
    title: str = "Durable journals",
):
    artifact = ArtifactRef.from_bytes(
        "artifact://research/notes.txt",
        b"source notes",
        media_type="text/plain",
    )
    return journal.accept(
        "update_section",
        {"section": "introduction", "title": title},
        {"sections": [{"id": "introduction", "title": title}]},
        workflow_hash=WORKFLOW_HASH,
        model_hash=MODEL_HASH,
        artifacts=(artifact,),
        tool_correlation_ids=(correlation_id,),
        idempotency_id=idempotency_id,
    )


def test_accept_is_durable_and_duplicate_idempotency_is_a_noop(
    tmp_path: Path,
) -> None:
    path = tmp_path / "workspace" / "journal.sqlite3"
    journal = CommandJournal(path)

    first = _accept(journal)
    duplicate = _accept(CommandJournal(path))

    assert duplicate == first
    assert first.input_hash == content_hash(first.command_json)
    assert first.artifacts[0].immutable_id.startswith("sha256:")
    assert first.manifest()["toolCorrelationIds"] == ["tool-call-1"]
    assert len(journal.entries()) == 1
    assert [item.publication_id for item in journal.outbox()] == [first.publication_id]
    journal.verify_integrity()


def test_generated_idempotency_id_is_stable_for_identical_accepted_content(
    tmp_path: Path,
) -> None:
    journal = CommandJournal(tmp_path / "journal.sqlite3")

    first = _accept(journal, idempotency_id=None)
    second = _accept(journal, idempotency_id=None)

    assert first == second
    assert first.idempotency_id.startswith("cmd-")
    assert len(journal.entries()) == 1


def test_reused_idempotency_or_tool_correlation_ids_are_rejected(
    tmp_path: Path,
) -> None:
    journal = CommandJournal(tmp_path / "journal.sqlite3")
    _accept(journal)

    with pytest.raises(IdempotencyConflict, match="different content"):
        _accept(journal, title="Conflicting title")
    with pytest.raises(ToolCorrelationConflict, match="already accepted"):
        _accept(
            journal,
            idempotency_id="command-2",
            title="Another accepted command",
        )

    assert len(journal.entries()) == 1
    journal.verify_integrity()


def test_outbox_is_rebuilt_from_accepted_entries_and_immutable_receipts(
    tmp_path: Path,
) -> None:
    journal = CommandJournal(tmp_path / "journal.sqlite3")
    first = _accept(journal)
    second = _accept(
        journal,
        idempotency_id="command-2",
        correlation_id="tool-call-2",
        title="Publication outboxes",
    )
    claimed = journal.claim_next()
    assert claimed == first
    journal.record_published(
        first.publication_id,
        "a" * 40,
        git_head_before=None,
    )
    assert journal.claim_next() == second
    assert [item.status for item in journal.outbox()] == ["in_flight"]

    rebuilt = journal.rebuild_outbox()

    assert [(item.publication_id, item.status, item.attempts) for item in rebuilt] == [
        (second.publication_id, "pending", 0)
    ]
    assert journal.receipts()[0].publication_id == first.publication_id


def test_database_guards_and_hash_chain_detect_journal_mutation(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite3"
    journal = CommandJournal(path)
    _accept(journal)

    with (
        sqlite3.connect(path) as connection,
        pytest.raises(sqlite3.IntegrityError, match="immutable"),
    ):
        connection.execute(
            "UPDATE accepted_commands SET command_name = 'tampered' WHERE sequence = 1"
        )

    with sqlite3.connect(path) as connection:
        connection.execute("DROP TRIGGER accepted_commands_no_update")
        connection.execute(
            "UPDATE accepted_commands SET command_json = '{\"tampered\":true}' "
            "WHERE sequence = 1"
        )

    with pytest.raises(JournalIntegrityError, match="fingerprint is invalid"):
        journal.verify_integrity()


def test_artifact_reference_detects_changed_bytes(tmp_path: Path) -> None:
    artifact_path = tmp_path / "artifact.txt"
    artifact_path.write_bytes(b"immutable")
    reference = ArtifactRef.from_path(artifact_path, uri="artifact://result")

    assert reference.verifies(artifact_path)
    artifact_path.write_bytes(b"changed")
    assert not reference.verifies(artifact_path)
