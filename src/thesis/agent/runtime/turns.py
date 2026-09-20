"""Serialize and coalesce the turns of one conversation.

Each conversation has exactly one worker: a wake either starts a turn or marks
the conversation dirty.  The worker loops, so every wake produces at least one
turn, and any number of wakes that arrive while a turn runs coalesce into a
single follow-up turn which replays the transcript and therefore sees them
all.  The running turn drains new inbound messages between model iterations
(``pull_new_input``), so the model sees the latest data without restarting and
without losing work.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from thesis.agent.runtime.store import SubAgentStore
from thesis.chat.service import ChatService

__all__ = ["NodeTurnCoordinator", "TurnError", "TurnRunner"]

#: What one conversation's worker is doing right now.
TurnError = tuple[str, str, Exception]


class TurnRunner(Protocol):
    """The runner seam the worker calls: one turn for one conversation anchor."""

    def run(
        self,
        workspace_id: str,
        chat_id: str,
        sub_agent_id: str,
        *,
        pull_new_input: bool = False,
    ) -> object: ...


@dataclass
class _WorkerState:
    busy: bool = False
    pending: bool = False


class NodeTurnCoordinator:
    """One serialized turn worker per conversation, coalescing concurrent wakes.

    The worker key is the conversation's placement triple — workspace, chat,
    and anchor id — so the root conversation of each chat (whose anchor id is
    the empty string) and every nested conversation have independent workers.
    """

    def __init__(
        self,
        *,
        runner: TurnRunner,
        chat: ChatService,
        store: SubAgentStore,
        spawn_thread: Callable[[Callable[[], None]], None] | None = None,
        on_error: Callable[[TurnError], None] | None = None,
    ) -> None:
        self._runner = runner
        self._chat = chat
        self._store = store
        self._spawn_thread = spawn_thread or _spawn_daemon_thread
        self._on_error = on_error
        self._states: dict[tuple[str, str, str], _WorkerState] = {}
        self._guard = threading.Lock()

    def schedule(self, workspace_id: str, chat_id: str, sub_agent_id: str) -> None:
        """Wake one conversation: start its worker, or mark it dirty when busy."""
        key = (workspace_id, chat_id, sub_agent_id)
        with self._guard:
            state = self._states.setdefault(key, _WorkerState())
            state.pending = True
            if state.busy:
                return
            state.busy = True
            state.pending = False
        self._spawn_thread(lambda: self._work(workspace_id, chat_id, sub_agent_id))

    def in_flight(self, workspace_id: str, chat_id: str, sub_agent_id: str) -> bool:
        """Return whether a turn is currently running for the conversation."""
        with self._guard:
            state = self._states.get((workspace_id, chat_id, sub_agent_id))
            return bool(state and state.busy)

    def _work(self, workspace_id: str, chat_id: str, sub_agent_id: str) -> None:
        key = (workspace_id, chat_id, sub_agent_id)
        try:
            while True:
                try:
                    self._runner.run(
                        workspace_id,
                        chat_id,
                        sub_agent_id,
                        pull_new_input=True,
                    )
                except Exception as error:  # noqa: BLE001 - one node fault is isolated
                    if self._on_error is not None:
                        self._on_error((workspace_id, sub_agent_id, error))
                with self._guard:
                    state = self._states.get(key)
                    if state is None or not state.pending:
                        self._states.pop(key, None)
                        return
                    state.pending = False
        finally:
            with self._guard:
                state = self._states.get(key)
                if state is not None and not state.pending:
                    self._states.pop(key, None)


def _spawn_daemon_thread(target: Callable[[], None]) -> None:
    threading.Thread(target=target, daemon=True).start()
