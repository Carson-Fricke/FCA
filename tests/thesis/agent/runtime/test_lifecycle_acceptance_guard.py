"""Workflow lifecycle decisions share the acceptance execution guard."""

import threading
from datetime import UTC, datetime

from thesis.agent.runtime.lifecycle import SubAgentLifecycle, SubAgentLifecycleError
from thesis.agent.runtime.specs import SubAgentSpec
from thesis.agent.runtime.store import PersistenceSubAgentStore
from thesis.chat import (
    ChatService,
    ChatThread,
    SubAgentStatus,
    WorkflowExecution,
    WorkflowExecutionState,
)


def test_cancellation_waits_for_acceptance_and_rereads_completed_state() -> None:
    chat = ChatService()
    chat.persistence.save(
        ChatThread(
            workspace_id="workspace",
            created_at=datetime.now(UTC),
            created_by="operator",
            chat_id="chat",
            title="Acceptance",
            owner_actor_id="operator",
        )
    )
    store = PersistenceSubAgentStore(chat.persistence)
    lifecycle = SubAgentLifecycle(chat, store=store)
    node = lifecycle.spawn(
        "workspace",
        "chat",
        spec=SubAgentSpec(role="phase"),
        actor_id="operator",
        workflow_execution_id="execution",
    )
    started = threading.Event()
    finished = threading.Event()
    errors: list[Exception] = []
    cancelled: list[tuple[str, ...]] = []
    chat.persistence.save(
        WorkflowExecution(
            workspace_id="workspace",
            created_at=datetime.now(UTC),
            created_by="operator",
            execution_id="execution",
            chat_id="chat",
            trigger_message_id="trigger",
            workflow_id="research",
            workflow_version="1.0.0",
            requested_by="operator",
            state=WorkflowExecutionState.RUNNING,
            phase="analysis",
        )
    )

    def cancel() -> None:
        started.set()
        try:
            cancelled.append(lifecycle.cancel_subtree("workspace", node.sub_agent_id))
        except SubAgentLifecycleError as error:
            errors.append(error)
        finally:
            finished.set()

    def commit() -> None:
        thread = threading.Thread(target=cancel)
        threads.append(thread)
        thread.start()
        assert started.wait(5)
        assert not finished.wait(0.05)
        # The workflow must close its executor inside this same acceptance scope.
        lifecycle.complete(
            "workspace", node.sub_agent_id, result={"summary": "accepted"}
        )

    threads: list[threading.Thread] = []
    chat.accept_transition(
        "workspace",
        "execution",
        expected_intent_epoch=0,
        expected_phase="analysis",
        expected_node_id=None,
        expected_attempt_id=None,
        commit=commit,
    )
    thread = threads[0]
    thread.join(5)
    assert not thread.is_alive() and finished.is_set()
    assert not errors and cancelled == [()]
    current = lifecycle.get("workspace", node.sub_agent_id)
    assert current is not None and current.status is SubAgentStatus.COMPLETED
    assert lifecycle.cancel("workspace", node.sub_agent_id) == current
    assert lifecycle.cancel_subtree("workspace", node.sub_agent_id) == ()


def test_cancellation_first_prevents_later_completion() -> None:
    chat = ChatService()
    lifecycle = SubAgentLifecycle(
        chat, store=PersistenceSubAgentStore(chat.persistence)
    )
    node = lifecycle.spawn(
        "workspace",
        "chat",
        spec=SubAgentSpec(role="phase"),
        actor_id="operator",
        workflow_execution_id="execution",
    )
    lifecycle.cancel_subtree("workspace", node.sub_agent_id)
    try:
        lifecycle.complete("workspace", node.sub_agent_id)
    except SubAgentLifecycleError:
        pass
    else:
        raise AssertionError("cancelled executor completed")
    current = lifecycle.get("workspace", node.sub_agent_id)
    assert current is not None and current.status is SubAgentStatus.CANCELLED
