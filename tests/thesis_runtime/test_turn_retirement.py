"""Idle worker retirement retains durable fences without process-local growth."""

import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from thesis_runtime.conversations import ChatService
from thesis_runtime.conversations.conversation import Conversations
from thesis_runtime.conversations.turns import NodeTurnCoordinator
from thesis_runtime.subagents.specs import SubAgentSpec


def test_idle_workers_retire_and_delayed_inputs_are_fenced_after_reload(
    tmp_path: Path,
) -> None:
    database = tmp_path / "cursors.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE cursors (workspace TEXT, chat TEXT, anchor TEXT, position INTEGER, PRIMARY KEY(workspace, chat, anchor))"
        )

    def read(workspace: str, chat: str, anchor: str) -> int:
        with sqlite3.connect(database) as connection:
            row = connection.execute(
                "SELECT position FROM cursors WHERE workspace=? AND chat=? AND anchor=?",
                (workspace, chat, anchor),
            ).fetchone()
            return 0 if row is None else row[0]

    def acknowledge(workspace: str, chat: str, anchor: str, position: int) -> None:
        with sqlite3.connect(database) as connection:
            connection.execute(
                "INSERT INTO cursors VALUES (?, ?, ?, ?) ON CONFLICT(workspace, chat, anchor) DO UPDATE SET position=max(position, excluded.position)",
                (workspace, chat, anchor, position),
            )

    service = Conversations(chat=ChatService(), spawn_thread=lambda target: None)
    runs: list[str] = []

    class Runner:
        def run(
            self,
            workspace_id: str,
            chat_id: str,
            sub_agent_id: str,
            *,
            pull_new_input: bool = False,
        ) -> None:
            runs.append(chat_id)
            generation, _ = coordinator.snapshot(
                workspace_id, chat_id, sub_agent_id, lambda: ()
            )
            coordinator.mark_consumed(
                workspace_id, chat_id, sub_agent_id, generation, input_position=1
            )

    coordinator = NodeTurnCoordinator(
        runner=Runner(),
        chat=service.chat,
        store=service.store,
        spawn_thread=lambda target: target(),
        read_consumed_input=read,
        acknowledge_input=acknowledge,
    )
    for index in range(32):
        coordinator.schedule(
            "workspace", f"chat-{index}", "", input_id="input", input_position=1
        )
        assert not coordinator._states
    assert len(runs) == 32
    for index in range(32):
        assert not coordinator.in_flight("workspace", f"unseen-{index}", "")
    assert not coordinator._states
    coordinator.schedule("workspace", "chat-0", "", input_id="input", input_position=1)
    assert len(runs) == 32 and not coordinator._states
    coordinator = NodeTurnCoordinator(
        runner=Runner(),
        chat=service.chat,
        store=service.store,
        spawn_thread=lambda target: target(),
        read_consumed_input=read,
        acknowledge_input=acknowledge,
    )
    coordinator.schedule("workspace", "chat-0", "", input_id="input", input_position=1)
    assert len(runs) == 32 and not coordinator._states


def test_ordered_burst_coalesces_without_pending_id_limit() -> None:
    service = Conversations(chat=ChatService(), spawn_thread=lambda target: None)
    workers: list[Callable[[], None]] = []
    cursor = 0

    def acknowledge(workspace: str, chat: str, anchor: str, position: int) -> None:
        nonlocal cursor
        cursor = max(cursor, position)

    class Runner:
        def run(
            self,
            workspace_id: str,
            chat_id: str,
            sub_agent_id: str,
            *,
            pull_new_input: bool = False,
        ) -> None:
            generation, _ = coordinator.snapshot(
                workspace_id, chat_id, sub_agent_id, lambda: ()
            )
            coordinator.mark_consumed(
                workspace_id, chat_id, sub_agent_id, generation, input_position=2000
            )

    coordinator = NodeTurnCoordinator(
        runner=Runner(),
        chat=service.chat,
        store=service.store,
        spawn_thread=workers.append,
        read_consumed_input=lambda *args: cursor,
        acknowledge_input=acknowledge,
    )
    for position in range(1, 2001):
        coordinator.schedule(
            "workspace",
            "chat",
            "",
            input_id=f"input-{position}",
            input_position=position,
        )
    assert len(workers) == 1
    workers[0]()
    assert cursor == 2000 and not coordinator._states


def test_ack_failure_keeps_state_and_does_not_claim_durable_retirement() -> None:
    service = Conversations(chat=ChatService(), spawn_thread=lambda target: None)
    workers: list[Callable[[], None]] = []

    class Runner:
        def run(
            self,
            workspace_id: str,
            chat_id: str,
            sub_agent_id: str,
            *,
            pull_new_input: bool = False,
        ) -> None:
            pass

    def acknowledge(*args: object) -> None:
        raise OSError("cursor unavailable")

    coordinator = NodeTurnCoordinator(
        runner=Runner(),
        chat=service.chat,
        store=service.store,
        spawn_thread=workers.append,
        read_consumed_input=lambda *args: 0,
        acknowledge_input=acknowledge,
    )
    coordinator.schedule("workspace", "chat", "", input_id="input", input_position=1)
    with pytest.raises(OSError, match="cursor unavailable"):
        coordinator.mark_consumed("workspace", "chat", "", 1, input_position=1)
    workers[0]()
    assert ("workspace", "chat", "") in coordinator._states
    assert coordinator._states[("workspace", "chat", "")].durable_position == 0


def test_terminal_child_does_not_spin_on_pending_wakes() -> None:
    service = Conversations(chat=ChatService(), spawn_thread=lambda target: None)
    node = service.lifecycle.spawn(
        "workspace", "chat", spec=SubAgentSpec(role="helper"), actor_id="operator"
    )
    service.lifecycle.cancel("workspace", node.sub_agent_id)
    workers: list[Callable[[], None]] = []
    runs: list[str] = []
    cursors: dict[tuple[str, str, str], int] = {}

    class Runner:
        def run(
            self,
            workspace_id: str,
            chat_id: str,
            sub_agent_id: str,
            *,
            pull_new_input: bool = False,
        ) -> None:
            runs.append(sub_agent_id)

    def acknowledge(workspace: str, chat: str, anchor: str, position: int) -> None:
        cursors[(workspace, chat, anchor)] = position

    coordinator = NodeTurnCoordinator(
        runner=Runner(),
        chat=service.chat,
        store=service.store,
        spawn_thread=workers.append,
        read_consumed_input=lambda *key: cursors.get(key, 0),
        acknowledge_input=acknowledge,
    )
    for position in range(1, 20):
        coordinator.schedule(
            "workspace",
            "chat",
            node.sub_agent_id,
            input_id=f"input-{position}",
            input_position=position,
        )
    workers[0]()
    assert runs == [node.sub_agent_id]
    assert not cursors
    assert not coordinator._states


def test_retirement_and_new_wake_do_not_use_a_stale_loaded_cursor() -> None:
    service = Conversations(chat=ChatService(), spawn_thread=lambda target: None)
    workers: list[Callable[[], None]] = []
    cursor = 0
    entered = threading.Event()
    release = threading.Event()
    reads = 0

    def read(*args: str) -> int:
        nonlocal reads
        reads += 1
        position = cursor
        if reads == 1:
            entered.set()
            assert release.wait(5)
        return position

    def acknowledge(workspace: str, chat: str, anchor: str, position: int) -> None:
        nonlocal cursor
        cursor = max(cursor, position)

    class Runner:
        def run(
            self,
            workspace_id: str,
            chat_id: str,
            sub_agent_id: str,
            *,
            pull_new_input: bool = False,
        ) -> None:
            coordinator.mark_consumed(
                workspace_id, chat_id, sub_agent_id, 1, input_position=1
            )

    coordinator = NodeTurnCoordinator(
        runner=Runner(),
        chat=service.chat,
        store=service.store,
        spawn_thread=workers.append,
        read_consumed_input=read,
        acknowledge_input=acknowledge,
    )
    wake = threading.Thread(
        target=lambda: coordinator.schedule(
            "workspace", "chat", "", input_id="input", input_position=1
        )
    )
    wake.start()
    assert entered.wait(5)
    # Observation does not acquire the cursor lock or allocate unrelated state.
    assert not coordinator.in_flight("workspace", "unseen", "")
    release.set()
    wake.join(5)
    assert not wake.is_alive()
    workers[0]()
    coordinator.schedule("workspace", "chat", "", input_id="input", input_position=1)
    assert reads == 2 and len(workers) == 1 and not coordinator._states
