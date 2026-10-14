"""In-process commit notification for the chat timeline stream.

Writers record a post-commit version per chat, and timeline readers snapshot
that version before reading durable items and wait for it to change. Async readers
subscribe a chat-scoped wake hint and clear it before their durable read. The counter
is an in-process wake hint, never a durable cursor; the database sequence
remains the only cursor. Ephemeral provider deltas and broker frame logs are
removed: every visible message, activity, and event is durable timeline data.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
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
        self._listeners: dict[tuple[str, str], set[Callable[[], None]]] = {}

    def notify(self, workspace_id: str, chat_id: str) -> None:
        """Record one committed change and wake every waiting reader."""
        key = (workspace_id, chat_id)
        with self._condition:
            self._versions[key] = self._versions.get(key, 0) + 1
            self._condition.notify_all()
            listeners = tuple(self._listeners.get(key, ()))
        for callback in listeners:
            try:
                callback()
            except Exception:
                logging.getLogger(__name__).exception("timeline subscriber failed")

    def subscribe(
        self,
        workspace_id: str,
        chat_id: str,
        on_commit: Callable[[], None] | None = None,
    ) -> Callable[[], None]:
        """Register a chat-scoped hint without prescribing an HTTP event loop."""
        key = (workspace_id, chat_id)
        if on_commit is not None:
            with self._condition:
                self._listeners.setdefault(key, set()).add(on_commit)

        def release() -> None:
            with self._condition:
                callbacks = self._listeners.get(key)
                if callbacks is not None and on_commit is not None:
                    callbacks.discard(on_commit)
                    if not callbacks:
                        self._listeners.pop(key, None)

        return release

    def close(self) -> None:
        """Local notifications own no background resources."""

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
            self._condition.wait_for(
                lambda: self._versions.get(key, 0) != version, timeout
            )
            return self._versions.get(key, 0)
