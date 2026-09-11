"""In-process streaming log for chat replies.

Assistant replies stream to the UI as ephemeral SSE frames: ``provider.delta``
frames carry incremental model text, and a final ``chat.message`` frame carries
the persisted durable message.  The log is bounded, rebuildable runtime state
(never canonical): chat history is served by the chat service, and the broker
only tails live frames for a connected chat.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from threading import Lock

PROVIDER_DELTA = "provider.delta"
CHAT_MESSAGE = "chat.message"


@dataclass(frozen=True, slots=True)
class ChatStreamFrame:
    """One framed chat-stream event with its per-chat replay cursor."""

    cursor: int
    event: str
    data: Mapping[str, object]

    def as_dict(self) -> dict[str, object]:
        return {"cursor": self.cursor, "event": self.event, "data": dict(self.data)}


class InMemoryChatStreamBroker:
    """Bounded, thread-safe per-chat frame log for live streaming."""

    def __init__(self, *, max_frames_per_chat: int = 2_000) -> None:
        self._frames: dict[tuple[str, str], deque[ChatStreamFrame]] = {}
        self._cursors: dict[tuple[str, str], int] = {}
        self._lock = Lock()
        self._max_frames = max_frames_per_chat

    def publish(
        self,
        workspace_id: str,
        chat_id: str,
        event: str,
        data: Mapping[str, object],
    ) -> ChatStreamFrame:
        """Append one frame and return it with its assigned cursor."""
        key = (workspace_id, chat_id)
        with self._lock:
            cursor = self._cursors.get(key, 0) + 1
            self._cursors[key] = cursor
            frame = ChatStreamFrame(cursor=cursor, event=event, data=dict(data))
            self._frames.setdefault(key, deque(maxlen=self._max_frames)).append(frame)
            return frame

    def read(
        self, workspace_id: str, chat_id: str, *, after_cursor: int = 0
    ) -> tuple[ChatStreamFrame, ...]:
        """Return frames published after ``after_cursor`` in cursor order."""
        key = (workspace_id, chat_id)
        with self._lock:
            return tuple(
                frame
                for frame in self._frames.get(key, ())
                if frame.cursor > after_cursor
            )

    def latest_cursor(self, workspace_id: str, chat_id: str) -> int:
        """Return the newest published cursor for the chat, or 0."""
        with self._lock:
            return self._cursors.get((workspace_id, chat_id), 0)


__all__ = [
    "CHAT_MESSAGE",
    "PROVIDER_DELTA",
    "ChatStreamFrame",
    "InMemoryChatStreamBroker",
]
