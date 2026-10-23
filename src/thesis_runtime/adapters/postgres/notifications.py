"""Commit-only LISTEN/NOTIFY hints; authoritative records remain in PostgreSQL."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from threading import Event, Lock, Thread

from thesis_runtime.conversations.streaming import CommitNotifier

from .store import PostgresStore


class PostgresNotifications(CommitNotifier):
    def __init__(self, store: PostgresStore) -> None:
        super().__init__()
        self.store = store
        self.work_changed = Event()
        self._stop = Event()
        self._guard = Lock()
        self._keys: dict[tuple[str, str], int] = {}
        self._thread = Thread(
            target=self._listen, name="postgres-notifications", daemon=True
        )
        self._thread.start()

    def subscribe(
        self,
        workspace_id: str,
        chat_id: str,
        on_commit: Callable[[], None] | None = None,
    ) -> Callable[[], None]:
        release = super().subscribe(workspace_id, chat_id, on_commit)
        key = workspace_id, chat_id
        with self._guard:
            self._keys[key] = self._keys.get(key, 0) + 1
        released = False

        def close() -> None:
            nonlocal released
            if not released:
                released = True
                release()
                with self._guard:
                    count = self._keys[key] - 1
                    if count:
                        self._keys[key] = count
                    else:
                        self._keys.pop(key)

        return close

    def _listen(self) -> None:
        while not self._stop.is_set():
            try:
                with self.store.pool.connection() as connection:
                    connection.execute("LISTEN thesis_commits")
                    # A reconnect may miss hints. Wake readers to reconcile the
                    # authoritative log and queued work, never replay effects.
                    self.work_changed.set()
                    with self._guard:
                        keys = tuple(self._keys)
                    for key in keys:
                        self.notify(*key)
                    while not self._stop.is_set():
                        for event in connection.notifies(timeout=0.5, stop_after=128):
                            data = json.loads(event.payload)
                            if data.get("work"):
                                self.work_changed.set()
                            if data.get("chat"):
                                self.notify(data["workspace"], data["chat"])
                    connection.execute("UNLISTEN thesis_commits")
            except Exception:
                if not self._stop.is_set():
                    logging.getLogger(__name__).exception(
                        "PostgreSQL notification connection failed"
                    )
                    self._stop.wait(1)

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)
