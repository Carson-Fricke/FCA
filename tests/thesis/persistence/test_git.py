from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from thesis.persistence import (
    CommandJournal,
    GitCoordinator,
    GitFenceError,
    PublicationReconciler,
    PublicationService,
    ReconciliationError,
    content_hash,
)

WORKFLOW_HASH = content_hash("workflow-v1")
MODEL_HASH = content_hash("model-v1")


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ("git", *arguments),
        cwd=repository,
        capture_output=True,
        check=True,
        encoding="utf-8",
        stdin=subprocess.DEVNULL,
        timeout=10,
    )
    return result.stdout.strip()


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "--quiet")
    _git(repository, "config", "user.name", "Thesis Test")
    _git(repository, "config", "user.email", "thesis@example.invalid")
    (repository / "README.md").write_text("isolated repository\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "--quiet", "-m", "initial")
    return repository


def _accept(journal: CommandJournal, identifier: str = "command-1"):
    return journal.accept(
        "update_section",
        {"section": "methods", "revision": identifier},
        {"sections": {"methods": {"revision": identifier}}},
        workflow_hash=WORKFLOW_HASH,
        model_hash=MODEL_HASH,
        tool_correlation_ids=(f"tool-{identifier}",),
        idempotency_id=identifier,
    )


def test_publication_commits_only_manifest_and_records_all_hashes(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    unrelated = repository / "unrelated.txt"
    unrelated.write_text("must stay staged\n", encoding="utf-8")
    _git(repository, "add", "unrelated.txt")
    journal = CommandJournal(tmp_path / "journal.sqlite3")
    entry = _accept(journal)

    receipt = PublicationService(journal, GitCoordinator(repository)).publish_next()

    assert receipt is not None
    assert receipt.commit_hash == _git(repository, "rev-parse", "HEAD")
    assert _git(repository, "diff", "--cached", "--name-only") == "unrelated.txt"
    changed = _git(
        repository,
        "diff-tree",
        "--no-commit-id",
        "--name-only",
        "-r",
        receipt.commit_hash,
    )
    assert changed == ".thesis/publication.json"
    manifest = json.loads((repository / ".thesis" / "publication.json").read_text())
    assert manifest["hashes"] == {
        "input": entry.input_hash,
        "journal": entry.journal_hash,
        "model": MODEL_HASH,
        "workflow": WORKFLOW_HASH,
    }
    assert manifest["toolCorrelationIds"] == ["tool-command-1"]
    message = _git(repository, "show", "-s", "--format=%B", receipt.commit_hash)
    assert f"Thesis-Publication-Id: {entry.publication_id}" in message
    assert journal.outbox() == ()
    journal.verify_integrity()


def test_interrupted_after_git_commit_is_recovered_without_duplicate_commit(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    path = tmp_path / "journal.sqlite3"
    journal = CommandJournal(path)
    entry = _accept(journal)
    assert journal.claim_next() == entry
    git = GitCoordinator(repository)
    git_receipt = git.publish(entry, expected_head=git.head())
    committed_head = git_receipt.commit_hash

    restarted_journal = CommandJournal(path)
    report = PublicationReconciler(restarted_journal, git).reconcile()
    duplicate_report = PublicationReconciler(restarted_journal, git).reconcile()

    assert [item.commit_hash for item in report.recovered_publications] == [
        committed_head
    ]
    assert report.queued_publication_ids == ()
    assert duplicate_report.recovered_publications == ()
    assert PublicationService(restarted_journal, git).publish_next() is None
    assert _git(repository, "rev-list", "--count", "HEAD") == "2"


def test_direct_retry_recovers_git_parent_as_original_fence(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    journal = CommandJournal(tmp_path / "journal.sqlite3")
    entry = _accept(journal)
    parent = _git(repository, "rev-parse", "HEAD")
    assert journal.claim_next() == entry
    git = GitCoordinator(repository)
    git.publish(entry, expected_head=parent)
    journal.release(entry.publication_id, "simulated receipt failure")

    receipt = PublicationService(journal, git).publish_next()

    assert receipt is not None
    assert receipt.git_head_before == parent
    assert _git(repository, "rev-list", "--count", "HEAD") == "2"


def test_interrupted_before_git_commit_rebuilds_and_drains_queue(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    journal = CommandJournal(tmp_path / "journal.sqlite3")
    entry = _accept(journal)
    assert journal.claim_next() == entry
    assert journal.outbox()[0].status == "in_flight"

    receipts = PublicationService(journal, GitCoordinator(repository)).drain()

    assert len(receipts) == 1
    assert receipts[0].publication_id == entry.publication_id
    assert journal.outbox() == ()
    assert _git(repository, "rev-list", "--count", "HEAD") == "2"


def test_expected_head_fences_concurrent_repository_change(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    journal = CommandJournal(tmp_path / "journal.sqlite3")
    entry = _accept(journal)
    git = GitCoordinator(repository)
    stale_head = git.head()
    (repository / "concurrent.txt").write_text("change\n", encoding="utf-8")
    _git(repository, "add", "concurrent.txt")
    _git(repository, "commit", "--quiet", "-m", "concurrent change")

    with pytest.raises(GitFenceError, match="Git HEAD moved"):
        git.publish(entry, expected_head=stale_head)

    assert not (repository / ".thesis" / "publication.json").exists()


def test_existing_unlocked_publication_lock_file_is_reusable(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    journal = CommandJournal(tmp_path / "journal.sqlite3")
    entry = _accept(journal)
    lock = repository / ".git" / "thesis-publication.lock"
    lock.write_bytes(b"\0")

    receipt = GitCoordinator(repository).publish(
        entry,
        expected_head=_git(repository, "rev-parse", "HEAD"),
    )

    assert receipt.commit_hash == _git(repository, "rev-parse", "HEAD")


def test_reconciliation_rejects_receipt_commit_removed_from_head(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    journal = CommandJournal(tmp_path / "journal.sqlite3")
    receipt = PublicationService(
        journal,
        GitCoordinator(repository),
    )
    _accept(journal)
    published = receipt.publish_next()
    assert published is not None
    parent = _git(repository, "rev-parse", f"{published.commit_hash}^")
    _git(repository, "update-ref", "HEAD", parent, published.commit_hash)

    with pytest.raises(ReconciliationError, match="reachable publication"):
        PublicationReconciler(journal, GitCoordinator(repository)).reconcile()


def test_reconciliation_rejects_changed_manifest_at_git_head(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    journal = CommandJournal(tmp_path / "journal.sqlite3")
    _accept(journal)
    service = PublicationService(journal, GitCoordinator(repository))
    assert service.publish_next() is not None
    manifest = repository / ".thesis" / "publication.json"
    manifest.write_text('{"replaced":true}\n', encoding="utf-8")
    _git(repository, "add", ".thesis/publication.json")
    _git(repository, "commit", "--quiet", "-m", "replace thesis manifest")

    with pytest.raises(ReconciliationError, match="Git HEAD does not expose"):
        PublicationReconciler(journal, GitCoordinator(repository)).reconcile()
