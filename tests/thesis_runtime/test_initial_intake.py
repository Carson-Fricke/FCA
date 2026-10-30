"""History windows never discard queued inputs before their initial ACK."""

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from thesis_runtime.adapters.filesystem.records import FileChatPersistence
from thesis_runtime.adapters.providers.invocations import (
    ModelCompletion,
    ProviderMessage,
    Role,
)
from thesis_runtime.adapters.providers.session_adapter import (
    NativeSessionAdapter,
    project_native_messages,
)
from thesis_runtime.contracts.datum import Datum
from thesis_runtime.contracts.messages import (
    MessageContent,
    SessionMessage,
    ToolCorrelation,
    ToolRequest,
    decode_transcript,
)
from thesis_runtime.contracts.records import ChatMessage, MessageRole, SubAgentKind
from thesis_runtime.conversations import ChatService
from thesis_runtime.conversations.conversation import Conversations
from thesis_runtime.conversations.delivery import Anchor
from thesis_runtime.conversations.incoming import write_incoming
from thesis_runtime.subagents.specs import InitialContext, SubAgentSpec


@pytest.mark.parametrize("interrupt_startup", [False, True])
def test_initial_context_is_durable_before_wake_and_not_repeated_on_revisit(
    tmp_path, interrupt_startup
):
    assignment = "Investigate the assigned construction."
    original = "  Original request with exact spacing.\nSecond line.  "
    wakes = []
    requests = []

    class Persistence(FileChatPersistence):
        fail = interrupt_startup

        def admit_message(self, message):
            if self.fail and message.body == assignment:
                self.fail = False
                raise OSError("assignment persistence interrupted")
            return super().admit_message(message)

    class Provider:
        streaming = True

        def stream(self, **kwargs):
            requests.append(kwargs["messages"])
            yield ModelCompletion(
                message=ProviderMessage("assistant", "Observed"),
                model="test",
                provider="test",
                role=kwargs["role"],
                prompt_tokens=0,
                completion_tokens=0,
            )

    persistence = Persistence(tmp_path)
    chat = ChatService(persistence=persistence)
    thread = chat.create_chat("workspace", title="Startup", owner_actor_id="operator")
    service = Conversations(
        chat=chat, provider=NativeSessionAdapter(Provider()), spawn_thread=wakes.append
    )
    context = InitialContext(original, "original-message", thread.chat_id)

    def spawn(prompt=assignment, key="assignment-one"):
        return service.spawn(
            Anchor("workspace", thread.chat_id),
            SubAgentSpec(role="helper"),
            prompt,
            SubAgentKind.CONVERSATION,
            sub_agent_id="startup-child",
            prompt_key=key,
            initial_context=(context,),
        )

    if interrupt_startup:
        with pytest.raises(OSError, match="assignment persistence"):
            spawn()
        assert not wakes
        rows = persistence.list(ChatMessage, "workspace")
        contexts = [row for row in rows if row.body == original]
        assert len(contexts) == 1 and contexts[0].data is not None
        assert contexts[0].data["runTurn"] is False
        chat = ChatService(persistence=FileChatPersistence(tmp_path))
        service = Conversations(
            chat=chat,
            provider=NativeSessionAdapter(Provider()),
            spawn_thread=wakes.append,
        )
    child = spawn()
    assert len(wakes) == 1
    transcript = child.transcript()
    assert [row.body for row in transcript] == [original, assignment]
    assert transcript[0].message_id != context.source_message_id
    assert [row.input_position for row in transcript] == [1, 2]
    wakes.pop(0)()
    assert [
        json.loads(message.content)["text"]
        for message in requests[0]
        if message.role == "user"
    ] == [original, assignment]
    spawn()
    assert not wakes
    spawn("Review the new evidence.", "assignment-two")
    assert len(wakes) == 1
    assert [row.body for row in child.transcript()].count(original) == 1


@pytest.mark.parametrize("delivery", [False, True])
def test_all_sixty_inputs_and_outputs_remain_in_context_after_restart(
    tmp_path: Path, delivery: bool
) -> None:
    chat = ChatService(persistence=FileChatPersistence(tmp_path))
    thread = chat.create_chat("workspace", title="Queued", owner_actor_id="operator")
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
            assert chat.read_consumed_input("workspace", thread.chat_id, "") == (
                0 if len(requests) == 1 else 60
            )
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
        read_consumed_input=chat.read_consumed_input,
        acknowledge_input=chat.acknowledge_input,
    )
    for index in range(60):
        if delivery:
            service.deliver(
                Anchor("workspace", thread.chat_id),
                Datum("evidence", text=f"queued-{index}"),
            )
        else:
            service.root("workspace", thread.chat_id).send(
                f"queued-{index}", "operator"
            )
    assert len(workers) == 1
    workers[0]()
    assert chat.read_consumed_input("workspace", thread.chat_id, "") == 60
    assert len(requests[0]) == 61
    for index in range(60):
        assert (
            sum(
                json.loads(message.content)["text"] == f"queued-{index}"
                for message in requests[0]
                if message.role == "user"
            )
            == 1
        )
    assert not service._workers._states
    chat = ChatService(persistence=FileChatPersistence(tmp_path))
    restarted = Conversations(
        chat=chat,
        provider=NativeSessionAdapter(Provider()),
        read_consumed_input=chat.read_consumed_input,
        acknowledge_input=chat.acknowledge_input,
    )
    restarted.root("workspace", thread.chat_id).run_turn()
    assert len(requests[1]) >= 62
    assert chat.read_consumed_input("workspace", thread.chat_id, "") == 60


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("rollback", [False, True])
def test_persisted_input_fifo_uses_positions_not_clock_or_reverse_ids(
    tmp_path: Path, nested: bool, rollback: bool
) -> None:
    clock = datetime(2026, 1, 1, tzinfo=UTC)
    ids = iter(("message-z", "message-a"))
    chat = ChatService(
        persistence=FileChatPersistence(tmp_path),
        clock=lambda: clock,
        id_factory=lambda prefix: next(ids) if prefix == "message" else "chat",
    )
    thread = chat.create_chat("workspace", title="FIFO", owner_actor_id="operator")
    service = Conversations(chat=chat, spawn_thread=lambda target: None)
    node = (
        service.lifecycle.spawn(
            "workspace",
            thread.chat_id,
            spec=SubAgentSpec(role="helper"),
            actor_id="operator",
        )
        if nested
        else None
    )
    anchor = Anchor(
        "workspace", thread.chat_id, None if node is None else node.sub_agent_id
    )
    data = {"subAgentId": anchor.sub_agent_id} if nested else None
    first = chat.send_message(
        "workspace", thread.chat_id, actor_id="operator", body="Accepted A", data=data
    )
    if rollback:
        clock -= timedelta(seconds=1)
    second = chat.send_message(
        "workspace", thread.chat_id, actor_id="operator", body="Accepted B", data=data
    )
    assert first.input_position == 1 and second.input_position == 2
    reloaded = ChatService(persistence=FileChatPersistence(tmp_path))
    assert [m.body for m in reloaded.list_messages("workspace", thread.chat_id)] == [
        "Accepted A",
        "Accepted B",
    ]
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

    runtime = Conversations(chat=reloaded, provider=NativeSessionAdapter(Provider()))
    runtime.run_node_turn("workspace", thread.chat_id, anchor.sub_agent_id or "")
    assert [json.loads(m.content)["text"] for m in requests[0] if m.role == "user"] == [
        "Accepted A",
        "Accepted B",
    ]
    assert first.created_at == clock + (
        timedelta(seconds=1) if rollback else timedelta()
    )


def test_input_reordering_leaves_midtool_and_final_assistant_slots_fixed(
    tmp_path: Path,
) -> None:
    clock = datetime(2026, 1, 1, tzinfo=UTC)
    ids = iter(("message-e", "message-d", "message-a", "message-g", "message-h"))
    chat = ChatService(
        persistence=FileChatPersistence(tmp_path),
        clock=lambda: clock,
        id_factory=lambda prefix: next(ids) if prefix == "message" else "chat",
    )
    thread = chat.create_chat(
        "workspace", title="Tool replay", owner_actor_id="operator"
    )
    anchor = Anchor("workspace", thread.chat_id)
    write_incoming(
        chat,
        anchor,
        SessionMessage("message-e", "incoming", MessageContent("Initial A", "user")),
        body="Initial A",
        role=MessageRole.USER,
    )
    clock += timedelta(seconds=1)

    def store_assistant(
        message_id: str, body: str, requests: tuple[ToolRequest, ...] = ()
    ) -> None:
        message = SessionMessage(
            message_id, "outgoing", MessageContent(body, "agent", requests=requests)
        )
        chat.create_message(
            "workspace",
            thread.chat_id,
            actor_id="agent",
            message_id=message_id,
            body=body,
            role=MessageRole.ASSISTANT,
            data={
                "sessionMessage": message.as_dict(),
                "toolCalls": [request.as_dict() for request in requests],
            },
        )

    store_assistant("message-d", "Checking", (ToolRequest("call", "check", "{}"),))
    clock += timedelta(seconds=1)
    write_incoming(
        chat,
        anchor,
        SessionMessage("message-a", "incoming", MessageContent("Steering B", "user")),
        body="Steering B",
        role=MessageRole.USER,
    )
    clock += timedelta(seconds=1)
    write_incoming(
        chat,
        anchor,
        SessionMessage(
            "message-g",
            "incoming",
            MessageContent(
                "Checked",
                "check",
                correlation=ToolCorrelation("message-d", "call", "check"),
                result={"ok": True},
            ),
        ),
        body="Checked",
        role=MessageRole.TOOL,
        tool_result={"ok": True, "callId": "call", "tool": "check"},
    )
    clock += timedelta(seconds=1)
    store_assistant("message-h", "Final")
    reloaded = ChatService(persistence=FileChatPersistence(tmp_path))
    service = Conversations(chat=reloaded)
    original = reloaded.list_messages("workspace", thread.chat_id)
    canonical = service.transcript(Anchor("workspace", thread.chat_id))
    assert [
        (i, m.message_id)
        for i, m in enumerate(canonical)
        if m.role is not MessageRole.USER
    ] == [
        (i, m.message_id)
        for i, m in enumerate(original)
        if m.role is not MessageRole.USER
    ]
    replay = project_native_messages(decode_transcript(canonical))
    call_index = next(i for i, message in enumerate(replay) if message.tool_calls)
    assert replay[call_index + 1].tool_call_id == "call"
    assert json.loads(replay[call_index + 2].content)["text"] == "Steering B"
    assert next(
        i
        for i, m in enumerate(replay)
        if m.role == "user" and json.loads(m.content)["text"] == "Steering B"
    ) < next(i for i, m in enumerate(replay) if m.content == "Final")
