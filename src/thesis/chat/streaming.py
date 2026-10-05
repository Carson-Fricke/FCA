"""In-process commit notification for the chat timeline stream.

Writers record a post-commit version per chat, and timeline readers snapshot
that version before reading durable items and wait for it to change. The counter
is an in-process wake hint, never a durable cursor; the database sequence
remains the only cursor. Ephemeral provider deltas and broker frame logs are
removed: every visible message, activity, and event is durable timeline data.
"""

from __future__ import annotations

from threading import Condition

__all__ = ["CommitNotifier"]


class CommitNotifier:
    """Thread-safe per-chat commit counter that wakes timeline readers.

    Writers call :meth:`notify` after a durable commit. A reader snapshots the
    version before reading durable items and then waits for a version change,
    so a commit between the read and the wait cannot be lost. Multiple commits
    may coalesce into one wake; a timeout bounds a lost or missed wake.
    """

    def __init__(self) -> None:
        self._condition = Condition()
        self._versions: dict[tuple[str, str], int] = {}

    def notify(self, workspace_id: str, chat_id: str) -> None:
        """Record one committed change and wake every waiting reader."""
        key = (workspace_id, chat_id)
        with self._condition:
            self._versions[key] = self._versions.get(key, 0) + 1
            self._condition.notify_all()

    def version(self, workspace_id: str, chat_id: str) -> int:
        """Return the chat's current commit version, or ``0``."""
        with self._condition:
            return self._versions.get((workspace_id, chat_id), 0)

    def wait(
        self, workspace_id: str, chat_id: str, version: int, timeout: float
    ) -> int:
        """Block until the chat's version differs from ``version`` or ``timeout``."""
        key = (workspace_id, chat_id)
        with self._condition:
            if self._versions.get(key, 0) != version:
                return self._versions.get(key, 0)
            self._condition.wait(timeout)
            return self._versions.get(key, 0)
