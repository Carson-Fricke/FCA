"""Serialize and coalesce the turns of one conversation.

Each conversation has exactly one worker: a wake either starts a turn or advances
its input generation. A model-boundary snapshot acknowledges only the generation
it observed. Unconsumed wakes coalesce into one follow-up turn; input drained by
the active turn does not cause another turn. The running turn drains new inbound messages between model iterations
(``pull_new_input``), so the model sees the latest data without restarting and
without losing work.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from typing import Protocol, TypeVar

from thesis_runtime.contracts.activity import is_external_input as is_external_input
from thesis_runtime.conversations.service import ChatService
from thesis_runtime.subagents.store import SubAgentStore

__all__ = ["NodeTurnCoordinator", "TurnError", "TurnRunner"]

#: What one conversation's worker is doing right now.
TurnError = tuple[str, str, Exception]
_Snapshot = TypeVar("_Snapshot")


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
    generation: int = 0
    consumed: int = 0
    guard: threading.RLock = field(default_factory=threading.RLock)
    recent_ids: deque[str] = field(default_factory=lambda: deque(maxlen=256))
    consumed_position: int = 0
    durable_position: int = 0
    requested_position: int = 0
    requested_generation: int = 0
    retired: bool = False
    cursor_loaded: bool = False


class NodeTurnCoordinator:
    """One serialized turn worker per conversation, coalescing concurrent wakes.

    The worker key is the conversation's placement triple — workspace, chat,
    and anchor id — so the root conversation of each chat (whose anchor id is
    the empty string) and every nested conversation have independent workers.
    This coordinator requires one process owning a conversation; it is not a
    distributed lease. Durable positions fence delayed wakes beyond the bounded
    recent-id window. Persisted inputs require positive admitted positions.
    Idle states retire only with paired durable cursor callbacks. The reader
    returns zero for a new conversation; the ACK atomically persists max(old,
    position) before returning. Anchor ids use the empty string for root.
    """

    def __init__(
        self,
        *,
        runner: TurnRunner,
        chat: ChatService,
        store: SubAgentStore,
        spawn_thread: Callable[[Callable[[], None]], None] | None = None,
        on_error: Callable[[TurnError], None] | None = None,
        read_consumed_input: Callable[[str, str, str], int] | None = None,
        acknowledge_input: Callable[[str, str, str, int], None] | None = None,
    ) -> None:
        self._runner = runner
        self._chat = chat
        self._store = store
        self._spawn_thread = spawn_thread or _spawn_daemon_thread
        self._on_error = on_error
        if (read_consumed_input is None) != (acknowledge_input is None):
            raise ValueError("durable input cursor callbacks must be supplied together")
        self._read_consumed_input = read_consumed_input
        self._acknowledge_input = acknowledge_input
        self._states: dict[tuple[str, str, str], _WorkerState] = {}
        self._guard = threading.Lock()

    def _state(self, key: tuple[str, str, str]) -> _WorkerState:
        with self._guard:
            return self._states.setdefault(key, _WorkerState())

    def _load_cursor(self, key: tuple[str, str, str], state: _WorkerState) -> None:
        if not state.cursor_loaded:
            position = (
                self._read_consumed_input(*key) if self._read_consumed_input else 0
            )
            state.consumed_position = position
            state.durable_position = position
            state.cursor_loaded = True

    def _retire(self, key: tuple[str, str, str], state: _WorkerState) -> None:
        """Called under the per-conversation guard, never across persistence I/O."""
        if (
            self._read_consumed_input is not None
            and not state.busy
            and state.requested_position <= state.durable_position
        ):
            with self._guard:
                if self._states.get(key) is state:
                    state.retired = True
                    del self._states[key]

    def schedule(
        self,
        workspace_id: str,
        chat_id: str,
        sub_agent_id: str,
        *,
        input_id: str | None = None,
        input_position: int = 0,
    ) -> None:
        """Wake one conversation: start its worker, or mark it dirty when busy."""
        if input_id is not None and input_position <= 0:
            raise ValueError("persisted input requires a positive admitted position")
        key = (workspace_id, chat_id, sub_agent_id)
        while True:
            state = self._state(key)
            state.guard.acquire()
            if state.retired:
                state.guard.release()
                continue
            break
        try:
            self._load_cursor(key, state)
            if (input_id is not None and (input_id in state.recent_ids)) or (
                input_position > 0 and input_position <= state.consumed_position
            ):
                self._retire(key, state)
                return
            state.generation += 1
            if input_position > 0:
                state.requested_position = max(state.requested_position, input_position)
                state.requested_generation = state.generation
            if state.busy:
                return
            state.busy = True
            state.consumed = state.generation
        finally:
            state.guard.release()
        try:
            self._spawn_thread(lambda: self._work(workspace_id, chat_id, sub_agent_id))
        except Exception:
            with state.guard:
                state.busy = False
            raise

    def in_flight(self, workspace_id: str, chat_id: str, sub_agent_id: str) -> bool:
        """Return whether a turn is currently running for the conversation."""
        with self._guard:
            state = self._states.get((workspace_id, chat_id, sub_agent_id))
        if state is None:
            return False
        with state.guard:
            return state.busy

    def snapshot(
        self,
        workspace_id: str,
        chat_id: str,
        sub_agent_id: str,
        read: Callable[[], _Snapshot],
        *,
        observe: Callable[[_Snapshot], Collection[str]] | None = None,
    ) -> tuple[int, _Snapshot]:
        """Register snapshot input before a delayed wake can advance generation."""
        with self._guard:
            state = self._states.get((workspace_id, chat_id, sub_agent_id))
        if state is None:
            return 0, read()
        with state.guard:
            generation = state.generation
        # No persistence I/O holds the global map guard or an intake guard.
        # A concurrent wake gets a later generation and cannot be ACKed here.
        snapshot = read()
        with state.guard:
            if observe is not None:
                for input_id in observe(snapshot):
                    if input_id not in state.recent_ids:
                        state.recent_ids.append(input_id)
            return generation, snapshot

    def mark_consumed(
        self,
        workspace_id: str,
        chat_id: str,
        sub_agent_id: str,
        generation: int,
        observed_ids: Collection[str] = (),
        input_position: int = 0,
    ) -> None:
        """Acknowledge observed input without swallowing wakes after its snapshot."""
        state = self._state((workspace_id, chat_id, sub_agent_id))
        with state.guard:
            self._load_cursor((workspace_id, chat_id, sub_agent_id), state)
            state.consumed = max(state.consumed, min(generation, state.generation))
            state.consumed_position = max(state.consumed_position, input_position)
            if state.requested_position <= state.consumed_position:
                state.consumed = max(state.consumed, state.requested_generation)
            for input_id in observed_ids:
                if input_id not in state.recent_ids:
                    state.recent_ids.append(input_id)
            position = state.consumed_position
        if position > 0 and self._acknowledge_input is not None:
            self._acknowledge_input(workspace_id, chat_id, sub_agent_id, position)
            with state.guard:
                state.durable_position = max(state.durable_position, position)
                self._retire((workspace_id, chat_id, sub_agent_id), state)

    def _work(self, workspace_id: str, chat_id: str, sub_agent_id: str) -> None:
        key = (workspace_id, chat_id, sub_agent_id)
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
                    try:
                        self._on_error((workspace_id, sub_agent_id, error))
                    except Exception:
                        state = self._state(key)
                        with state.guard:
                            state.busy = False
                        raise
            state = self._state(key)
            record = (
                self._store.get_sub_agent(workspace_id, sub_agent_id)
                if sub_agent_id
                else None
            )
            if record is not None and record.status.value in {
                "completed",
                "failed",
                "cancelled",
            }:
                with state.guard:
                    # Terminal wakes are non-runnable, not model consumption.
                    # Drop only ephemeral worker state; durable inputs remain pending.
                    state.busy = False
                    with self._guard:
                        if self._states.get(key) is state:
                            state.retired = True
                            del self._states[key]
                return
            with state.guard:
                if state.generation <= state.consumed:
                    state.busy = False
                    self._retire(key, state)
                    return
                state.consumed = state.generation


def _spawn_daemon_thread(target: Callable[[], None]) -> None:
    threading.Thread(target=target, daemon=True).start()
