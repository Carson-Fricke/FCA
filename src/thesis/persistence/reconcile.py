"""Reconciliation of accepted workspace state, Git history, and queue state."""

from __future__ import annotations

from dataclasses import dataclass

from thesis.persistence.git import GitCoordinator, GitReceipt
from thesis.persistence.journal import CommandJournal, PublicationReceipt


class ReconciliationError(RuntimeError):
    """Accepted state cannot be reconciled safely with repository history."""


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    """Observed repairs after reconciling durable and rebuildable state."""

    recovered_publications: tuple[PublicationReceipt, ...]
    queued_publication_ids: tuple[str, ...]
    git_head: str | None


class PublicationReconciler:
    """Repair interrupted publication and derive the pending queue from facts."""

    def __init__(self, journal: CommandJournal, git: GitCoordinator) -> None:
        self._journal = journal
        self._git = git

    def reconcile(self) -> ReconciliationReport:
        """Match accepted entries to exact Git commits, then rebuild the outbox."""
        self._journal.verify_integrity()
        recorded = {
            receipt.publication_id: receipt for receipt in self._journal.receipts()
        }
        published_ids = set(recorded)
        recovered: list[PublicationReceipt] = []
        entries = self._journal.entries()
        for entry in entries:
            receipt = recorded.get(entry.publication_id)
            if receipt is not None:
                if not self._git.commit_matches(
                    receipt.commit_hash, entry
                ) or not self._git.is_ancestor(receipt.commit_hash):
                    raise ReconciliationError(
                        f"Git commit {receipt.commit_hash!r} no longer matches "
                        f"reachable publication {entry.publication_id!r}"
                    )
                continue
            commit_hash = self._git.find_publication(entry)
            if commit_hash is not None:
                published_ids.add(entry.publication_id)
                recovered.append(
                    self._journal.record_published(
                        entry.publication_id,
                        commit_hash,
                        git_head_before=self._git.parent(commit_hash),
                    )
                )

        git_head = self._git.head()
        latest_published = next(
            (
                entry
                for entry in reversed(entries)
                if entry.publication_id in published_ids
            ),
            None,
        )
        if latest_published is not None and (
            git_head is None
            or not self._git.manifest_matches(git_head, latest_published)
        ):
            raise ReconciliationError(
                "Git HEAD does not expose the latest published accepted state"
            )
        queue = self._journal.rebuild_outbox()
        return ReconciliationReport(
            recovered_publications=tuple(recovered),
            queued_publication_ids=tuple(item.publication_id for item in queue),
            git_head=git_head,
        )


class PublicationService:
    """Deliver journal outbox entries through the fenced Git coordinator."""

    def __init__(self, journal: CommandJournal, git: GitCoordinator) -> None:
        self._journal = journal
        self._git = git
        self._reconciler = PublicationReconciler(journal, git)

    def publish_next(self) -> PublicationReceipt | None:
        """Publish one pending accepted state with retry-safe crash semantics."""
        entry = self._journal.claim_next()
        if entry is None:
            return None
        head_before = self._git.head()
        try:
            git_receipt = self._git.publish(entry, expected_head=head_before)
            return self._record(entry.publication_id, git_receipt)
        except BaseException as error:
            self._journal.release(entry.publication_id, error)
            raise

    def drain(self, *, limit: int | None = None) -> tuple[PublicationReceipt, ...]:
        """Reconcile first, then publish queued entries in acceptance order."""
        if limit is not None and limit < 0:
            raise ValueError("publication limit must not be negative")
        self._reconciler.reconcile()
        receipts: list[PublicationReceipt] = []
        while limit is None or len(receipts) < limit:
            receipt = self.publish_next()
            if receipt is None:
                break
            receipts.append(receipt)
        return tuple(receipts)

    def _record(
        self, publication_id: str, git_receipt: GitReceipt
    ) -> PublicationReceipt:
        return self._journal.record_published(
            publication_id,
            git_receipt.commit_hash,
            git_head_before=git_receipt.head_before,
        )


__all__ = [
    "PublicationReconciler",
    "PublicationService",
    "ReconciliationError",
    "ReconciliationReport",
]
