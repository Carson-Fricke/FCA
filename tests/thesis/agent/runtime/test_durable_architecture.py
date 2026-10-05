"""Composed conversations use durable policy, authority and model-read epochs."""

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from thesis.agent.messages import decode_transcript
from thesis.agent.runtime.child import ChildConversation
from thesis.agent.runtime.context import ToolContext
from thesis.agent.runtime.control import ControlToolService, StaleCompletionError
from thesis.agent.runtime.control_tools import control_registered_tools
from thesis.agent.runtime.conversation import Conversations
from thesis.agent.runtime.delivery import Anchor
from thesis.agent.runtime.lifecycle import SubAgentLifecycleError
from thesis.agent.runtime.session import SessionResult, SessionToolRegistry
from thesis.agent.runtime.specs import SubAgentSpec
from thesis.agent.runtime.turns import NodeTurnCoordinator
from thesis.chat import (
    AgentInvocation,
    ChatService,
    CompletionMode,
    FileChatPersistence,
    SubAgentKind,
    SubAgentStatus,
    WorkflowExecution,
    replace_record,
)
from thesis.chat.persistence import record_from_payload
from thesis.providers.invocations import ChatMessage, ModelCompletion, Role, ToolCall
from thesis.providers.session_adapter import project_native_messages


def test_composed_roots_helpers_phase_reload_and_repair(tmp_path: Path) -> None:
    chat = ChatService(persistence=FileChatPersistence(tmp_path))
    first = chat.create_chat("workspace", title="First", owner_actor_id="operator")
    second = chat.create_chat("workspace", title="Second", owner_actor_id="operator")
    requests: list[tuple[ChatMessage, ...]] = []
    contexts: list[ToolContext] = []
    workers: list[Callable[[], None]] = []
    phase_calls = 0

    def advance_epoch() -> None:
        execution = chat.get_execution("workspace", "execution")
        chat.persistence.save(
            replace_record(execution, intent_epoch=execution.intent_epoch + 1)
        )

    class Provider:
        def complete(
            self,
            *,
            messages: Sequence[ChatMessage],
            role: Role = "primary",
            tools: Sequence[Mapping[str, object]] | None = None,
            response_format: Mapping[str, object] | None = None,
        ) -> ModelCompletion:
            nonlocal phase_calls
            requests.append(tuple(messages))
            if "'analysis'" in messages[0].content:
                phase_calls += 1
                if phase_calls == 1:
                    advance_epoch()
                    phase.send("Re-derive using revised intent", "operator")
                return ModelCompletion(
                    "",
                    "model",
                    "provider",
                    role,
                    0,
                    0,
                    tool_calls=(
                        ToolCall(
                            f"phase-{phase_calls}",
                            "request_phase_completion",
                            '{"completion":{"summary":"accepted"}}',
                        ),
                    ),
                )
            return ModelCompletion("Helper evidence", "model", "provider", role, 0, 0)

    def registry(context: ToolContext) -> SessionToolRegistry:
        contexts.append(context)
        return SessionToolRegistry(control_registered_tools(control, context))

    service = Conversations(
        chat=chat,
        provider=Provider(),
        registry_factory=registry,
        spawn_thread=workers.append,
    )
    validated: list[int] = []

    def validate(
        workspace: str, record: object, completion: Mapping[str, object]
    ) -> str | None:
        validated.append(int(str(completion["intentEpoch"])))
        return None

    def apply(
        workspace: str, record: object, completion: Mapping[str, object]
    ) -> Mapping[str, object]:
        assert isinstance(record, type(phase.record))
        service.lifecycle.complete(workspace, "phase", result=completion)
        return completion

    control = ControlToolService(
        conversations=service, contract_validator=validate, phase_completions=apply
    )
    root = service.root("workspace", first.chat_id)
    helper = service.spawn(
        Anchor("workspace", first.chat_id),
        SubAgentSpec(role="helper", kind=SubAgentKind.ONE_SHOT),
        "Investigate",
        SubAgentKind.ONE_SHOT,
    )
    assert helper.record is not None
    assert helper.record.completion_mode is CompletionMode.COMPLETE_ON_TEXT
    helper_result = helper.run_turn()
    assert isinstance(helper_result, SessionResult)
    assert helper_result.status == "completed"
    assert helper_result.turns[-1].message.direction == "outgoing"
    assert helper_result.turns[-1].message.content.text == "Helper evidence"
    assert contexts[-1].sub_agent_id == helper.conversation_id
    assert requests[-1][1:] == project_native_messages(
        decode_transcript(helper.transcript()[:-1])
    )
    # A separate runtime resolves the exact policy and return anchor from disk.
    reloaded = Conversations(
        chat=ChatService(persistence=FileChatPersistence(tmp_path)),
        provider=Provider(),
        spawn_thread=workers.append,
    )
    restored = reloaded.get("workspace", helper.conversation_id)
    assert isinstance(restored, ChildConversation)
    restored.run_turn()
    assert (
        restored.record is not None
        and restored.record.status is SubAgentStatus.COMPLETED
    )
    assert restored.returns_to == Anchor("workspace", first.chat_id)
    restored.deliver_completed_result()
    assert (
        len(
            [
                m
                for m in root.transcript()
                if (m.data or {}).get("kind") == "child_result"
            ]
        )
        == 1
    )
    assert service.children("workspace", second.chat_id) == ()
    root_result = root.run_turn()
    assert root_result is not None
    assert contexts[-1].sub_agent_id is None
    assert requests[-1][1:] == project_native_messages(
        decode_transcript(root.transcript()[:-1])
    )
    assert all("Investigate" not in message.content for message in requests[-1])

    chat.persistence.save(
        WorkflowExecution(
            workspace_id="workspace",
            created_at=datetime.now(UTC),
            created_by="operator",
            execution_id="execution",
            chat_id=first.chat_id,
            trigger_message_id="trigger",
            workflow_id="research",
            workflow_version="1.0.0",
            requested_by="operator",
        )
    )
    node = service.lifecycle.spawn(
        "workspace",
        first.chat_id,
        spec=SubAgentSpec(role="workflow-node"),
        actor_id="operator",
        workflow_execution_id="execution",
    )
    phase = service.spawn(
        Anchor("workspace", first.chat_id, node.sub_agent_id),
        SubAgentSpec(role="analysis", phase="analysis"),
        "Analyze",
        SubAgentKind.CONVERSATION,
        sub_agent_id="phase",
        phase_executor=True,
        completion_mode=CompletionMode.EXPLICIT_COMPLETION,
        attribution={
            "workflow_execution_id": "execution",
            "phase_attempt_id": "attempt",
        },
    )
    phase_result = phase.run_turn()
    assert isinstance(phase_result, SessionResult)
    assert phase_result.status == "completed"
    assert phase_result.turns, phase_result.content
    for turn in phase_result.turns:
        assert turn.message.direction in ("incoming", "outgoing")
        assert not hasattr(turn.message, "role")
    phase_messages = decode_transcript(phase.transcript())
    assert requests[-1][1:] == project_native_messages(
        phase_messages[
            : next(
                i
                for i, message in enumerate(phase_messages)
                if any(request.id == "phase-2" for request in message.content.requests)
            )
        ]
    )
    for message in phase_messages:
        assert message.direction in ("incoming", "outgoing")
        assert not hasattr(message, "role")
        if message.content.source == "tool":
            assert message.direction == "incoming"
            assert message.content.result is not None
            assert message.content.correlation is not None
    assert phase_calls == 2 and validated == [1]
    assert phase.record is not None and phase.record.status is SubAgentStatus.COMPLETED
    phase_contexts = [c for c in contexts if c.sub_agent_id == "phase"]
    assert phase_contexts and phase_contexts[0].intent_epoch == 0
    assert phase_contexts[-1].intent_epoch == 1
    assert {c.intent_epoch for c in phase_contexts} == {0, 1}
    assert all(
        c.phase_executor and c.phase_attempt_id == "attempt" for c in phase_contexts
    )
    invocations = [
        i
        for i in chat.persistence.list(AgentInvocation, "workspace")
        if i.conversation_id == "phase"
    ]
    assert [i.intent_epoch for i in invocations if i.tool_name] == [0, 1]

    helper_context = ToolContext(
        "workspace",
        first.chat_id,
        "helper",
        "operator",
        sub_agent_id="helper",
        workflow_execution_id="execution",
        phase_attempt_id="attempt",
    )
    names = {tool.name for tool in control_registered_tools(control, helper_context)}
    assert "request_sub_agent_completion" in names
    assert "request_phase_completion" not in names

    cancelled = service.spawn(
        Anchor("workspace", first.chat_id),
        SubAgentSpec(role="cancel"),
        "Stop",
        SubAgentKind.CONVERSATION,
    )
    cancelled.cancel()
    with pytest.raises(SubAgentLifecycleError, match="cancelled"):
        cancelled.complete({"summary": "not success"})
    with pytest.raises(RuntimeError, match="only a completed"):
        cancelled.deliver_completed_result()


def test_phase_epoch_changes_during_validation_reject_before_apply() -> None:
    chat = ChatService()
    thread = chat.create_chat("workspace", title="Epoch", owner_actor_id="operator")
    chat.persistence.save(
        WorkflowExecution(
            workspace_id="workspace",
            created_at=datetime.now(UTC),
            created_by="operator",
            execution_id="execution",
            chat_id=thread.chat_id,
            trigger_message_id="trigger",
            workflow_id="research",
            workflow_version="1.0.0",
            requested_by="operator",
        )
    )
    service = Conversations(chat=chat, spawn_thread=lambda target: None)
    node = service.lifecycle.spawn(
        "workspace",
        thread.chat_id,
        spec=SubAgentSpec(role="workflow-node"),
        actor_id="operator",
        workflow_execution_id="execution",
    )
    phase = service.spawn(
        Anchor("workspace", thread.chat_id, node.sub_agent_id),
        SubAgentSpec(role="phase", phase="analysis"),
        "Analyze",
        SubAgentKind.CONVERSATION,
        phase_executor=True,
        attribution={
            "workflow_execution_id": "execution",
            "phase_attempt_id": "attempt",
        },
    )
    epoch = 0
    applied: list[object] = []

    def validate(
        workspace: str, record: object, completion: Mapping[str, object]
    ) -> str | None:
        nonlocal epoch
        epoch += 1
        return None

    control = ControlToolService(
        conversations=service,
        execution_epochs=lambda workspace, execution: epoch,
        contract_validator=validate,
        phase_completions=lambda *args: applied.append(args) or {},
    )
    with pytest.raises(StaleCompletionError):
        control.complete_phase(
            "workspace", phase.conversation_id, completion={"intentEpoch": 0}
        )
    assert applied == []


def test_fast_spawn_persists_policy_before_synchronous_worker() -> None:
    chat = ChatService()
    thread = chat.create_chat("workspace", title="Fast", owner_actor_id="operator")

    class Provider:
        def complete(
            self,
            *,
            messages: Sequence[ChatMessage],
            role: Role = "primary",
            tools: Sequence[Mapping[str, object]] | None = None,
            response_format: Mapping[str, object] | None = None,
        ) -> ModelCompletion:
            return ModelCompletion("Evidence", "model", "provider", role, 0, 0)

    service = Conversations(
        chat=chat, provider=Provider(), spawn_thread=lambda target: target()
    )
    child = service.spawn(
        Anchor("workspace", thread.chat_id),
        SubAgentSpec(role="fast", kind=SubAgentKind.ONE_SHOT),
        "Investigate",
        SubAgentKind.ONE_SHOT,
    )
    assert child.record is not None and child.record.status is SubAgentStatus.COMPLETED
    assert child.record.completion_mode is CompletionMode.COMPLETE_ON_TEXT
    assert (
        len(
            [
                m
                for m in service.root("workspace", thread.chat_id).transcript()
                if (m.data or {}).get("kind") == "child_result"
            ]
        )
        == 1
    )


def test_legacy_completion_policy_decodes_without_runtime_fallback() -> None:
    from thesis.chat import SubAgentRecord

    legacy = {
        "workspace_id": "workspace",
        "created_at": datetime.now(UTC).isoformat(),
        "created_by": "operator",
        "sub_agent_id": "child",
        "chat_id": "chat",
        "role": "helper",
        "kind": "one_shot",
        "parent_sub_agent_id": "parent",
    }
    decoded = record_from_payload(SubAgentRecord, legacy)
    assert isinstance(decoded, SubAgentRecord)
    assert decoded.completion_mode is CompletionMode.COMPLETE_ON_TEXT
    assert decoded.returns_to_sub_agent_id == "parent"
    assert not decoded.phase_executor
    phase = record_from_payload(
        SubAgentRecord,
        {
            **legacy,
            "phase": "analysis",
            "phase_attempt_id": "attempt",
            "workflow_execution_id": "execution",
        },
    )
    assert isinstance(phase, SubAgentRecord)
    assert phase.completion_mode is CompletionMode.EXPLICIT_COMPLETION
    assert phase.phase_executor


def test_ordered_intake_fences_delayed_wakes_beyond_recent_window() -> None:
    chat = ChatService()
    service = Conversations(chat=chat, spawn_thread=lambda target: None)

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

    workers: list[Callable[[], None]] = []
    coordinator = NodeTurnCoordinator(
        runner=Runner(), chat=chat, store=service.store, spawn_thread=workers.append
    )
    for position in range(1, 301):
        coordinator.schedule(
            "workspace",
            "chat",
            "",
            input_id=f"input-{position}",
            input_position=position,
        )
    coordinator.mark_consumed(
        "workspace",
        "chat",
        "",
        300,
        [f"input-{i}" for i in range(1, 301)],
        input_position=300,
    )
    workers[0]()
    coordinator.schedule("workspace", "chat", "", input_id="input-1", input_position=1)
    assert len(workers) == 1
    state = coordinator._states[("workspace", "chat", "")]
    assert len(state.recent_ids) == 256 and not state.pending
    coordinator.schedule(
        "workspace", "other-chat", "", input_id="fresh", input_position=1
    )
    assert len(workers) == 2
