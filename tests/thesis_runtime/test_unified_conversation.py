"""Root and phase conversations share execution, persistence, and intake."""

import json
from collections.abc import Callable, Collection, Mapping, Sequence
from datetime import UTC, datetime

import pytest

from thesis_runtime.adapters.providers.invocations import (
    ModelCompletion,
    ProviderMessage,
    Role,
    ToolCall,
)
from thesis_runtime.adapters.providers.session_adapter import (
    NativeSessionAdapter,
    project_native_messages,
)
from thesis_runtime.contracts.context import ToolContext
from thesis_runtime.contracts.datum import Datum
from thesis_runtime.contracts.messages import decode_transcript, from_record
from thesis_runtime.contracts.records import ChatMessage as DurableChatMessage
from thesis_runtime.contracts.records import MessageRole, WorkflowExecution
from thesis_runtime.conversations import ChatService
from thesis_runtime.conversations.conversation import Conversations
from thesis_runtime.conversations.delivery import Anchor
from thesis_runtime.conversations.turns import NodeTurnCoordinator
from thesis_runtime.subagents.specs import SubAgentSpec
from thesis_runtime.subagents.store import PersistenceSubAgentStore
from thesis_runtime.tools.registry import (
    RegisteredTool,
    SessionToolRegistry,
    ToolExecution,
)


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("late", [False, True])
@pytest.mark.parametrize("delayed_wake", [False, True])
def test_consumed_input_has_no_duplicate_turn_but_late_input_does(
    nested: bool,
    late: bool,
    delayed_wake: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat = ChatService()
    thread = chat.create_chat("workspace", title="Shared", owner_actor_id="operator")
    workers: list[Callable[[], None]] = []
    contexts: list[ToolContext] = []
    requests: list[tuple[ProviderMessage, ...]] = []
    dispatched: list[str] = []
    wake_pending: DurableChatMessage | None = None

    def intake() -> None:
        nonlocal wake_pending
        message = chat.send_message(
            "workspace",
            thread.chat_id,
            actor_id="operator",
            body="Fresh steering",
            data={"subAgentId": anchor.sub_agent_id} if nested else None,
        )
        assert message.role is MessageRole.USER
        if delayed_wake:
            wake_pending = message
        else:
            service.schedule_input(message)

    class Provider:
        def stream(self, *, message_id=None, **kwargs):
            from tests.thesis_runtime.provider_fakes import completion_frames

            yield from completion_frames(self.complete(**kwargs), message_id)

        streaming = True

        def complete(
            self,
            *,
            messages: Sequence[ProviderMessage],
            role: Role = "primary",
            tools: Sequence[Mapping[str, object]] | None = None,
            response_format: Mapping[str, object] | None = None,
        ) -> ModelCompletion:
            requests.append(tuple(messages))
            index = len(requests)
            if index == 1:
                return ModelCompletion(
                    message=ProviderMessage(
                        "assistant",
                        "Checking",
                        tool_calls=(ToolCall("call", "check", "{}"),),
                    ),
                    model="model",
                    provider="provider",
                    role=role,
                    prompt_tokens=0,
                    completion_tokens=0,
                )
            if index == 2:
                if wake_pending:
                    service.schedule_input(wake_pending)
            assert index <= (3 if late else 2), "duplicate follow-up turn"
            return ModelCompletion(
                message=ProviderMessage("assistant", "Finished"),
                model="model",
                provider="provider",
                role=role,
                prompt_tokens=0,
                completion_tokens=0,
            )

    def handler(arguments: Mapping[str, object], context: ToolContext) -> ToolExecution:
        envelope = service.transcript(anchor)[-1]
        assert envelope.role is MessageRole.ASSISTANT
        assert from_record(envelope).content.requests
        dispatched.append("check")
        if not late:
            intake()
        return ToolExecution(ok=True, text="Checked")

    def registry(context: ToolContext) -> SessionToolRegistry:
        contexts.append(context)
        return SessionToolRegistry(
            [
                RegisteredTool(
                    {"name": "check"},
                    handler,
                ),
            ]
        )

    service = Conversations(
        chat=chat,
        provider=NativeSessionAdapter(Provider()),
        registry_factory=registry,
        spawn_thread=workers.append,
        root_allowed_tools=("check",),
    )
    node = (
        service.lifecycle.spawn(
            "workspace",
            thread.chat_id,
            spec=SubAgentSpec(role="research", allowed_tools=("check",)),
            actor_id="operator",
        )
        if nested
        else None
    )
    anchor = Anchor(
        "workspace", thread.chat_id, None if node is None else node.sub_agent_id
    )
    original_snapshot = service._workers.snapshot
    late_injected = False

    def snapshot(
        workspace_id: str,
        chat_id: str,
        sub_agent_id: str,
        read: Callable[[], tuple[DurableChatMessage, ...]],
        *,
        observe: Callable[[tuple[DurableChatMessage, ...]], Collection[str]]
        | None = None,
    ) -> tuple[int, tuple[DurableChatMessage, ...]]:
        nonlocal late_injected
        result = original_snapshot(
            workspace_id, chat_id, sub_agent_id, read, observe=observe
        )
        if late and len(requests) == 2 and not late_injected:
            # Arrive after the final request's post-provider drain snapshot.
            late_injected = True
            intake()
            if wake_pending is not None:
                service.schedule_input(wake_pending)
        return result

    monkeypatch.setattr(service._workers, "snapshot", snapshot)
    chat.send_message(
        "workspace",
        thread.chat_id,
        actor_id="operator",
        body="Initial",
        data={"subAgentId": anchor.sub_agent_id} if nested else None,
    )
    service.wake(anchor)
    assert len(workers) == 1
    workers[0]()
    if delayed_wake and not late:
        # A transport may publish its wake only after the worker has exited.
        assert wake_pending is not None
        service.schedule_input(wake_pending)
    assert len(workers) == 1
    assert len(requests) == (3 if late else 2)
    assert late_injected is late
    assert dispatched == ["check"]
    assert (
        sum(
            json.loads(message.content)["text"] == "Fresh steering"
            for message in requests[-1]
            if message.role == "user"
        )
        == 1
    )
    assert not service.in_flight("workspace", thread.chat_id, anchor.sub_agent_id or "")
    assert contexts and all(c.sub_agent_id == anchor.sub_agent_id for c in contexts)
    if not nested:
        assert all(c.phase_attempt_id is None for c in contexts)
        assert all(c.allowed_tools == ("check",) for c in contexts)
    assert chat.read_consumed_input(
        "workspace", thread.chat_id, anchor.schedule_id
    ) == max(message.input_position for message in service.transcript(anchor))


def test_child_send_uses_authorized_anchored_answer_path() -> None:
    chat = ChatService()
    thread = chat.create_chat("workspace", title="Input", owner_actor_id="operator")
    workers: list[Callable[[], None]] = []
    requests: list[tuple[ProviderMessage, ...]] = []

    class Provider:
        def stream(self, *, message_id=None, **kwargs):
            from tests.thesis_runtime.provider_fakes import completion_frames

            yield from completion_frames(self.complete(**kwargs), message_id)

        streaming = True

        def complete(
            self,
            *,
            messages: Sequence[ProviderMessage],
            role: Role = "primary",
            tools: Sequence[Mapping[str, object]] | None = None,
            response_format: Mapping[str, object] | None = None,
        ) -> ModelCompletion:
            requests.append(tuple(messages))
            return ModelCompletion(
                message=ProviderMessage("assistant", "Finished"),
                model="model",
                provider="provider",
                role=role,
                prompt_tokens=0,
                completion_tokens=0,
            )

    service = Conversations(
        chat=chat,
        provider=NativeSessionAdapter(Provider()),
        spawn_thread=workers.append,
    )
    node = service.lifecycle.spawn(
        "workspace",
        thread.chat_id,
        spec=SubAgentSpec(role="research", allowed_tools=("check",)),
        actor_id="operator",
    )
    service.lifecycle.start("workspace", node.sub_agent_id)
    service.lifecycle.request_input("workspace", node.sub_agent_id)
    child = service.get("workspace", node.sub_agent_id)
    assert child is not None
    message = child.send("Answer", "operator")
    assert message.data is not None
    assert message.data["subAgentId"] == node.sub_agent_id
    assert message.data["kind"] == "operator_input"
    current = service.lifecycle.get("workspace", node.sub_agent_id)
    assert current is not None and current.status.value == "running"
    assert message not in service.root("workspace", thread.chat_id).transcript()
    assert len(workers) == 1
    workers[0]()
    assert len(requests) == 1
    assert json.loads(requests[0][-1].content)["text"] == "Answer"
    assert (
        chat.read_consumed_input("workspace", thread.chat_id, node.sub_agent_id)
        == message.input_position
    )
    assert len(workers) == 1


def test_acknowledging_snapshot_does_not_consume_a_newer_wake() -> None:
    chat = ChatService()
    workers: list[Callable[[], None]] = []
    runs: list[int] = []

    class Runner:
        def run(
            self,
            workspace_id: str,
            chat_id: str,
            sub_agent_id: str,
            *,
            pull_new_input: bool = False,
        ) -> None:
            runs.append(len(runs))
            if len(runs) == 1:
                generation, _ = coordinator.snapshot(
                    "workspace", "chat", "", lambda: ()
                )
                coordinator.schedule("workspace", "chat", "")
                coordinator.mark_consumed("workspace", "chat", "", generation)

    coordinator = NodeTurnCoordinator(
        runner=Runner(),
        chat=chat,
        store=PersistenceSubAgentStore(chat.persistence),
        spawn_thread=workers.append,
    )
    coordinator.schedule("workspace", "chat", "")
    workers[0]()
    assert runs == [0, 1]
    assert not coordinator.in_flight("workspace", "chat", "")


def test_cancelled_child_keeps_actual_result_and_durable_final_state() -> None:
    chat = ChatService()
    thread = chat.create_chat("workspace", title="Cancel", owner_actor_id="operator")
    requests: list[Sequence[ProviderMessage]] = []

    class Provider:
        def stream(self, *, message_id=None, **kwargs):
            from tests.thesis_runtime.provider_fakes import completion_frames

            yield from completion_frames(self.complete(**kwargs), message_id)

        streaming = True

        def complete(
            self,
            *,
            messages: Sequence[ProviderMessage],
            role: Role = "primary",
            tools: Sequence[Mapping[str, object]] | None = None,
            response_format: Mapping[str, object] | None = None,
        ) -> ModelCompletion:
            requests.append(tuple(messages))
            assert len(requests) == 1
            return ModelCompletion(
                message=ProviderMessage(
                    "assistant",
                    "Checking",
                    tool_calls=(
                        ToolCall("first", "check", "{}"),
                        ToolCall("second", "check", "{}"),
                    ),
                ),
                model="model",
                provider="provider",
                role=role,
                prompt_tokens=0,
                completion_tokens=0,
            )

    def handler(arguments: Mapping[str, object], context: ToolContext) -> ToolExecution:
        service.lifecycle.cancel("workspace", node.sub_agent_id)
        return ToolExecution(ok=True, text="Actual result")

    service = Conversations(
        chat=chat,
        provider=NativeSessionAdapter(Provider()),
        registry_factory=lambda context: SessionToolRegistry(
            [
                RegisteredTool({"name": "check"}, handler),
            ]
        ),
    )
    node = service.lifecycle.spawn(
        "workspace",
        thread.chat_id,
        actor_id="operator",
        spec=SubAgentSpec(role="research", allowed_tools=("check",)),
    )
    anchor = Anchor("workspace", thread.chat_id, node.sub_agent_id)
    chat.send_message(
        "workspace",
        thread.chat_id,
        actor_id="operator",
        body="Check",
        data={"subAgentId": node.sub_agent_id},
    )
    result = service.run_node_turn("workspace", thread.chat_id, node.sub_agent_id)
    assert result.status == "cancelled"
    current = service.lifecycle.get("workspace", node.sub_agent_id)
    assert current is not None and current.status.value == "cancelled"
    tool_messages = [
        message
        for message in service.transcript(anchor)
        if from_record(message).content.correlation is not None
    ]
    assert len(tool_messages) == 2
    assert (
        tool_messages[0].tool_result is not None and tool_messages[0].tool_result["ok"]
    )
    assert tool_messages[1].tool_result is not None
    failure = tool_messages[1].tool_result["failure"]
    assert isinstance(failure, Mapping) and failure["kind"] == "cancelled"


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("newer", [False, True])
def test_snapshot_registers_input_before_wake_and_ack(
    nested: bool,
    newer: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat = ChatService()
    thread = chat.create_chat(
        "workspace", title="Snapshot race", owner_actor_id="operator"
    )
    workers: list[Callable[[], None]] = []
    requests: list[tuple[ProviderMessage, ...]] = []

    class Provider:
        def stream(self, *, message_id=None, **kwargs):
            from tests.thesis_runtime.provider_fakes import completion_frames

            yield from completion_frames(self.complete(**kwargs), message_id)

        streaming = True

        def complete(
            self,
            *,
            messages: Sequence[ProviderMessage],
            role: Role = "primary",
            tools: Sequence[Mapping[str, object]] | None = None,
            response_format: Mapping[str, object] | None = None,
        ) -> ModelCompletion:
            requests.append(tuple(messages))
            return ModelCompletion(
                message=ProviderMessage(
                    "assistant",
                    "Checking" if len(requests) == 1 else "Finished",
                    tool_calls=(ToolCall("call", "check", "{}"),)
                    if len(requests) == 1
                    else (),
                ),
                model="model",
                provider="provider",
                role=role,
                prompt_tokens=0,
                completion_tokens=0,
            )

    def input_message(body: str) -> DurableChatMessage:
        return chat.send_message(
            "workspace",
            thread.chat_id,
            actor_id="operator",
            body=body,
            data={"subAgentId": anchor.sub_agent_id} if nested else None,
        )

    def handler(arguments: Mapping[str, object], context: ToolContext) -> ToolExecution:
        input_message("Snapshot input")
        return ToolExecution(ok=True, text="Checked")

    service = Conversations(
        chat=chat,
        provider=NativeSessionAdapter(Provider()),
        spawn_thread=workers.append,
        registry_factory=lambda context: SessionToolRegistry(
            [
                RegisteredTool({"name": "check"}, handler),
            ]
        ),
    )
    node = (
        service.lifecycle.spawn(
            "workspace",
            thread.chat_id,
            actor_id="operator",
            spec=SubAgentSpec(role="research", allowed_tools=("check",)),
        )
        if nested
        else None
    )
    anchor = Anchor(
        "workspace", thread.chat_id, None if node is None else node.sub_agent_id
    )
    input_message("Initial")
    original = service._workers.snapshot
    injected = False
    newer_injected = False

    def snapshot(
        workspace_id: str,
        chat_id: str,
        sub_agent_id: str,
        read: Callable[[], tuple[DurableChatMessage, ...]],
        *,
        observe: Callable[[tuple[DurableChatMessage, ...]], Collection[str]]
        | None = None,
    ) -> tuple[int, tuple[DurableChatMessage, ...]]:
        nonlocal injected, newer_injected
        result = original(workspace_id, chat_id, sub_agent_id, read, observe=observe)
        observed = next(
            (message for message in result[1] if message.body == "Snapshot input"),
            None,
        )
        if observed is not None and not injected:
            injected = True
            # Snapshot already includes the first input, but its wake arrives
            # before ACK, without creating a duplicate turn.
            service.schedule_input(observed)
        if newer and injected and len(requests) == 2 and not newer_injected:
            # The newer arrival must miss the final post-provider drain too.
            newer_injected = True
            service.schedule_input(input_message("After snapshot"))
        return result

    monkeypatch.setattr(service._workers, "snapshot", snapshot)
    service.wake(anchor)
    workers[0]()
    assert injected
    assert newer_injected is newer
    assert len(workers) == 1
    assert len(requests) == (3 if newer else 2)
    assert (
        sum(
            json.loads(message.content)["text"] == "Snapshot input"
            for message in requests[1]
            if message.role == "user"
        )
        == 1
    )
    assert all(
        json.loads(message.content)["text"] != "After snapshot"
        for message in requests[1]
        if message.role == "user"
    )
    if newer:
        assert (
            sum(
                json.loads(message.content)["text"] == "After snapshot"
                for message in requests[2]
                if message.role == "user"
            )
            == 1
        )
    assert chat.read_consumed_input(
        "workspace", thread.chat_id, anchor.schedule_id
    ) == max(message.input_position for message in service.transcript(anchor))
    assert not service.in_flight("workspace", thread.chat_id, anchor.schedule_id)


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("external_delivery", [False, True])
def test_runtime_tool_is_neutral_input_and_root_publishes_every_row_once(
    nested: bool,
    external_delivery: bool,
) -> None:
    chat = ChatService()
    thread = chat.create_chat(
        "workspace", title="Internal activity", owner_actor_id="operator"
    )
    workers: list[Callable[[], None]] = []
    requests: list[tuple[ProviderMessage, ...]] = []
    contexts: list[ToolContext] = []

    class Provider:
        def stream(self, *, message_id=None, **kwargs):
            from tests.thesis_runtime.provider_fakes import completion_frames

            yield from completion_frames(self.complete(**kwargs), message_id)

        streaming = True

        def complete(
            self,
            *,
            messages: Sequence[ProviderMessage],
            role: Role = "primary",
            tools: Sequence[Mapping[str, object]] | None = None,
            response_format: Mapping[str, object] | None = None,
        ) -> ModelCompletion:
            requests.append(tuple(messages))
            assert len(requests) <= 2, "internal activity caused a follow-up"
            return ModelCompletion(
                message=ProviderMessage(
                    "assistant",
                    "Checking" if len(requests) == 1 else "Finished",
                    tool_calls=(ToolCall("outer", "check", "{}"),)
                    if len(requests) == 1
                    else (),
                ),
                model="model",
                provider="provider",
                role=role,
                prompt_tokens=0,
                completion_tokens=0,
            )

    def registry(context: ToolContext) -> SessionToolRegistry:
        contexts.append(context)

        def internal() -> ToolExecution:
            pending = service.transcript(anchor)[-1]
            assert (
                pending.data is not None and pending.data["toolActivity"] == "started"
            )
            return ToolExecution(ok=True, text="Internal evidence")

        def handler(
            arguments: Mapping[str, object], context: ToolContext
        ) -> ToolExecution:
            service.execute_runtime_tool(
                context, "validate_study", {"source": "study"}, internal
            )
            # Canonical runtime evidence is consumed without a separate wake.
            if external_delivery:
                service.deliver(anchor, Datum("external", text="External evidence"))
            return ToolExecution(ok=True, text="Outer result")

        return SessionToolRegistry(
            [
                RegisteredTool({"name": "check"}, handler),
            ]
        )

    service = Conversations(
        chat=chat,
        provider=NativeSessionAdapter(Provider()),
        registry_factory=registry,
        spawn_thread=workers.append,
    )
    chat.persistence.save(
        WorkflowExecution(
            workspace_id="workspace",
            execution_id="execution",
            chat_id=thread.chat_id,
            trigger_message_id="trigger",
            workflow_id="research",
            workflow_version="1.0.0",
            requested_by="operator",
            created_by="operator",
            created_at=datetime.now(UTC),
            conversation_id=thread.chat_id,
            root_conversation_id=thread.chat_id,
            parent_conversation_id=None,
        )
    )
    node = (
        service.lifecycle.spawn(
            "workspace",
            thread.chat_id,
            actor_id="operator",
            spec=SubAgentSpec(
                role="research", phase="analysis", allowed_tools=("check",)
            ),
            workflow_execution_id="execution",
        )
        if nested
        else None
    )
    anchor = Anchor(
        "workspace", thread.chat_id, None if node is None else node.sub_agent_id
    )
    chat.send_message(
        "workspace",
        thread.chat_id,
        actor_id="operator",
        body="Check",
        data={"subAgentId": anchor.sub_agent_id} if nested else None,
    )
    service.wake(anchor)
    workers[0]()
    assert len(workers) == 1 and len(requests) == 2
    assert [message.role for message in requests[1]] == [
        "system",
        "user",
        "assistant",
        "tool",
        "user",
    ] + (["user"] if external_delivery else [])
    assert requests[1][3].tool_call_id == "outer"
    runtime = json.loads(requests[1][4].content)
    assert runtime["text"] == "Internal evidence"
    assert runtime["source"] == "runtime"
    assert runtime["correlation"] is None
    assert runtime["result"]["ok"] is True
    if external_delivery:
        assert "External evidence" in requests[1][-1].content
    durable = service.transcript(anchor)
    started = [
        message
        for message in durable
        if (message.data or {}).get("toolActivity") == "started"
    ]
    assert len(started) == 1
    pending = started[0]
    assert pending.data is not None
    owning_contexts = [c for c in contexts if c.sub_agent_id == anchor.sub_agent_id]
    assert owning_contexts
    assert all(pending.created_by == c.actor_id for c in owning_contexts)
    assert all(pending.execution_id == c.workflow_execution_id for c in owning_contexts)
    assert all(pending.data["ownerActorId"] == c.actor_id for c in owning_contexts)
    if nested:
        assert pending.data["phase"] == "analysis"
    finished = []
    for message in durable:
        if not isinstance((message.data or {}).get("sessionMessage"), Mapping):
            continue
        result = from_record(message).content.result
        if isinstance(result, Mapping) and str(result.get("toolCallId", "")).startswith(
            "runtime-"
        ):
            finished.append(message)
    assert len(finished) == 1
    assert from_record(finished[0]).content.correlation is None
    replay = project_native_messages(decode_transcript(durable))
    assert not any("Running validate_study" in message.content for message in replay)
    assert sum("Internal evidence" in message.content for message in replay) == 1
    assert not service.in_flight("workspace", thread.chat_id, anchor.schedule_id)
