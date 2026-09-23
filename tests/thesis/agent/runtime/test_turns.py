"""The per-node turn coordinator and the mid-turn input drain."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from thesis.agent.runtime.conversation import Conversations
from thesis.agent.runtime.lifecycle import SubAgentLifecycle
from thesis.agent.runtime.session import (
    RegisteredTool,
    SessionToolRegistry,
    ToolExecution,
)
from thesis.agent.runtime.specs import SubAgentSpec
from thesis.agent.runtime.store import PersistenceSubAgentStore
from thesis.agent.runtime.turns import NodeTurnCoordinator, TurnRunner
from thesis.chat.contracts import (
    MessageRole,
    SubAgentKind,
    TranscriptItemKind,
)
from thesis.chat.service import ChatService, InMemoryChatPersistence
from thesis.providers.invocations import ChatMessage as ProviderChatMessage
from thesis.providers.invocations import ModelCompletion, Role, ToolCall

_WORKSPACE = "ws-turns"
_ACTOR = "operator-1"


class _RecordingRunner:
    """A runner double that can trigger wakes while its turn runs."""

    def __init__(self) -> None:
        self.runs: list[tuple[str, str, str, bool]] = []
        self.during: list[Callable[[], None]] = []

    def run(
        self,
        workspace_id: str,
        chat_id: str,
        sub_agent_id: str,
        *,
        pull_new_input: bool = False,
    ) -> object:
        self.runs.append((workspace_id, chat_id, sub_agent_id, pull_new_input))
        actions, self.during = self.during, []
        for action in actions:
            action()
        return object()


def _coordinator(
    runner: TurnRunner, spawned: list[Callable[[], None]]
) -> NodeTurnCoordinator:
    chat = ChatService(persistence=InMemoryChatPersistence())
    store = PersistenceSubAgentStore(chat.persistence)
    return NodeTurnCoordinator(
        runner=runner,
        chat=chat,
        store=store,
        spawn_thread=lambda target: spawned.append(target),
    )


def test_coordinator_coalesces_wakes_that_arrive_during_a_turn() -> None:
    runner = _RecordingRunner()
    spawned: list[Callable[[], None]] = []
    coordinator = _coordinator(runner, spawned)

    coordinator.schedule(_WORKSPACE, "chat-1", "node-1")
    assert len(spawned) == 1
    # Two wakes arrive while the first turn is running: they must not start a
    # second concurrent worker, only mark the node dirty.
    runner.during.extend(
        [
            lambda: coordinator.schedule(_WORKSPACE, "chat-1", "node-1"),
            lambda: coordinator.schedule(_WORKSPACE, "chat-1", "node-1"),
        ]
    )

    spawned[0]()

    assert len(spawned) == 1
    assert [(run[2], run[3]) for run in runner.runs] == [
        ("node-1", True),
        ("node-1", True),
    ]
    assert coordinator.in_flight(_WORKSPACE, "chat-1", "node-1") is False


class _DrainProbeProvider:
    """A provider whose first turn triggers a wake, second verifies the drain."""

    def __init__(self, chat: ChatService, chat_id: str, node_id: str) -> None:
        self._chat = chat
        self._chat_id = chat_id
        self._node_id = node_id
        self.calls = 0
        self.saw_drained = False

    def complete(
        self,
        *,
        messages: Sequence[ProviderChatMessage],
        role: Role,
        tools: Sequence[Mapping[str, object]] | None = None,
        response_format: Mapping[str, object] | None = None,
    ) -> ModelCompletion:
        del tools, response_format
        self.calls += 1
        if self.calls == 1:
            self._chat.create_message(
                _WORKSPACE,
                self._chat_id,
                actor_id=_ACTOR,
                body="an update arrived mid-turn",
                role=MessageRole.USER,
                kind=TranscriptItemKind.SUB_AGENT,
                data={"subAgentId": self._node_id},
            )
            call = ToolCall(id="call-1", name="probe", arguments="{}")
            return ModelCompletion(
                content="",
                model="stub",
                provider="stub",
                role=role,
                prompt_tokens=0,
                completion_tokens=0,
                tool_calls=(call,),
            )
        self.saw_drained = any(
            message.role == "user" and "mid-turn" in message.content
            for message in messages
        )
        return ModelCompletion(
            content="done",
            model="stub",
            provider="stub",
            role=role,
            prompt_tokens=0,
            completion_tokens=0,
        )


def test_a_mid_turn_wake_is_drained_into_the_running_session() -> None:
    chat = ChatService(persistence=InMemoryChatPersistence())
    chat_id = chat.create_chat(
        _WORKSPACE, title="drain chat", owner_actor_id=_ACTOR
    ).chat_id
    store = PersistenceSubAgentStore(chat.persistence)
    lifecycle = SubAgentLifecycle(chat, store=store)
    child = lifecycle.spawn(
        _WORKSPACE,
        chat_id,
        spec=SubAgentSpec(role="worker", kind=SubAgentKind.ONE_SHOT),
        actor_id=_ACTOR,
    )
    chat.create_message(
        _WORKSPACE,
        chat_id,
        actor_id=_ACTOR,
        body="do the work",
        role=MessageRole.USER,
        kind=TranscriptItemKind.SUB_AGENT,
        data={"subAgentId": child.sub_agent_id},
    )
    provider = _DrainProbeProvider(chat, chat_id, child.sub_agent_id)
    registry = SessionToolRegistry(
        [
            RegisteredTool(
                spec={
                    "type": "function",
                    "function": {"name": "probe", "parameters": {"type": "object"}},
                },
                handler=lambda arguments: ToolExecution(
                    ok=True, structured={"ran": True}, text="ok"
                ),
            )
        ]
    )
    service = Conversations(
        chat=chat,
        store=store,
        provider=provider,
        registry_factory=lambda context: registry,
        lifecycle=lifecycle,
    )

    result = service.run_node_turn(_WORKSPACE, chat_id, child.sub_agent_id)

    assert result.status == "completed"
    assert provider.calls == 2
    assert provider.saw_drained is True
