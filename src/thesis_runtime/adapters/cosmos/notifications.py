"""One subscription-driven change-feed reader per API timeline adapter."""

import logging
from collections.abc import Callable, Mapping
from threading import Event, Lock, Thread
from typing import Any, cast

from azure.core.paging import PageIterator
from azure.cosmos import ContainerProxy

from thesis_runtime.conversations.streaming import CommitNotifier


class CosmosTimelineNotifications(CommitNotifier):
    """Broadcast durable commit hints locally, including commits by other processes.

    This cursor is independent of runnable-work consumption. Only active chats
    are notified; no token content or executable work travels over this channel.
    """

    def __init__(self, container: ContainerProxy) -> None:
        super().__init__()
        self._container = container
        self._subscribers: dict[tuple[str, str], int] = {}
        self._guard = Lock()
        self._wake = Event()
        self._stop = Event()
        self._ready = Event()
        self._thread: Thread | None = None
        self._cursor: str | None = None
        self._generation = 0
        self._cursor_generation = 0

    def subscribe(
        self,
        workspace_id: str,
        chat_id: str,
        on_commit: Callable[[], None] | None = None,
    ) -> Callable[[], None]:
        release_local = super().subscribe(workspace_id, chat_id, on_commit)
        key = (workspace_id, chat_id)
        with self._guard:
            if not self._subscribers:
                self._generation += 1
                self._ready.clear()
            self._subscribers[key] = self._subscribers.get(key, 0) + 1
            if self._thread is None:
                self._thread = Thread(
                    target=self._run, name="timeline-notifications", daemon=True
                )
                self._thread.start()
        self._wake.set()
        # Capture the feed cursor before the subscriber's first durable read.
        # Recovery polling remains available if the feed is temporarily down.
        self._ready.wait(2)
        released = False

        def release() -> None:
            nonlocal released
            with self._guard:
                if released:
                    return
                released = True
                release_local()
                count = self._subscribers.get(key, 0)
                if count <= 1:
                    self._subscribers.pop(key, None)
                else:
                    self._subscribers[key] = count - 1

        return release

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _read(self) -> bool:
        options: dict[str, Any] = {"max_item_count": 256}
        with self._guard:
            generation = self._generation
            continuation = (
                self._cursor if self._cursor_generation == generation else None
            )
        if continuation is None:
            options["start_time"] = "Now"
        else:
            options["continuation"] = continuation
        headers: Mapping[str, str] = {}

        def capture(value: Mapping[str, str], body: object) -> None:
            nonlocal headers
            headers = value

        pages = cast(
            PageIterator,
            self._container.query_items_change_feed(
                **options, response_hook=capture
            ).by_page(),
        )
        documents = list(next(pages, ()))
        cursor = pages.continuation_token or headers.get("etag")
        if not isinstance(cursor, str) or not cursor:
            raise RuntimeError("timeline feed omitted its continuation token")
        with self._guard:
            # A resumed subscriber captures "Now" before its initial durable
            # read, rather than draining an arbitrarily large idle backlog.
            if generation != self._generation:
                return True
            self._cursor = cursor
            self._cursor_generation = generation
            self._ready.set()
            subscribed = set(self._subscribers)
        changed = {
            (row.get("workspaceId"), row.get("chatId"))
            for row in documents
            if row.get("kind") in {"message", "event", "execution", "output"}
            and row.get("seq")
        }
        for key in subscribed & changed:
            self.notify(*key)
        return bool(documents)

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._guard:
                active = bool(self._subscribers)
            if not active:
                self._wake.wait()
                self._wake.clear()
                continue
            try:
                more = self._read()
            except Exception:
                logging.getLogger(__name__).exception("timeline change feed failed")
                self._stop.wait(1)
            else:
                if not more:
                    self._stop.wait(0.15)
