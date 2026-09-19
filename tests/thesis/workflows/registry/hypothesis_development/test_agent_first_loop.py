"""End-to-end agent-first loop: launch, seed, agent turn, transition, next spawn.

The phase agent owns the turn: the workflow prepares it (durable node, one seed
turn, a scheduled run of that agent), the agent's own session calls the
completion tool, and the engine validates the contract, applies the declared
transition, and prepares the next phase agent.  No engine turn and no engine
model call happen anywhere on this path.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

from thesis.agent.runtime.child_runner import NodeTurnRunner
from thesis.agent.runtime.control import ControlToolService
from thesis.agent.runtime.delivery import (
    DeliveryService,
    NodeInputService,
    TurnScheduler,
)
from thesis.agent.runtime.lifecycle import SubAgentLifecycle
from thesis.agent.runtime.registry import build_session_registry
from thesis.agent.runtime.session import CompletionProvider
from thesis.agent.runtime.store import PersistenceSubAgentStore
from thesis.chat.contracts import (
    ChatMessage,
    MessageRole,
    SubAgentKind,
    SubAgentStatus,
)
from thesis.chat.service import ChatService
from thesis.providers.invocations import ChatMessage as ProviderChatMessage
from thesis.providers.invocations import ModelCompletion, Role, ToolCall
from thesis.workflows.registry.hypothesis_development import (
    HypothesisDevelopmentRequest,
)
from thesis.workflows.registry.hypothesis_development.phases import (
    HypothesisDevelopmentPhase,
)

from ._support import IDEAS, WORKSPACE, _Kit, _kit, _request


class _QueuedCompletionProvider:
    """A session provider that calls queued tools with queued arguments.

    The last queued call repeats, so a repair loop converges on it.
    """

    def __init__(self, *calls: tuple[str, Mapping[str, object]]) -> None:
        self._calls = [(name, json.dumps(dict(arguments))) for name, arguments in calls]
        self.calls = 0

    def complete(
        self,
        *,
        messages: Sequence[ProviderChatMessage],
        role: Role,
        tools: Sequence[Mapping[str, object]] | None = None,
        response_format: Mapping[str, object] | None = None,
    ) -> ModelCompletion:
        del messages, tools, response_format
        index = min(self.calls, len(self._calls) - 1)
        self.calls += 1
        name, arguments = self._calls[index]
        call = ToolCall(id=f"call-{self.calls}", name=name, arguments=arguments)
        return ModelCompletion(
            content="",
            model="stub",
            provider="stub",
            role=role,
            prompt_tokens=0,
            completion_tokens=0,
            tool_calls=(call,),
        )


class _RecordingProvider:
    """A session provider that records the replayed messages and answers text."""

    def __init__(self) -> None:
        self.roles: list[str] = []
        self.tool_ids: list[str | None] = []
        self.announced: set[str] = set()
        self.bare_tool_messages: list[str | None] = []

    def complete(
        self,
        *,
        messages: Sequence[ProviderChatMessage],
        role: Role,
        tools: Sequence[Mapping[str, object]] | None = None,
        response_format: Mapping[str, object] | None = None,
    ) -> ModelCompletion:
        del tools, response_format
        self.roles = [message.role for message in messages]
        for message in messages:
            if message.role == "assistant":
                self.announced.update(call.id for call in message.tool_calls)
            if message.role == "tool":
                self.tool_ids.append(message.tool_call_id)
                if message.tool_call_id not in self.announced:
                    self.bare_tool_messages.append(message.tool_call_id)
        return ModelCompletion(
            content="No further action.",
            model="stub",
            provider="stub",
            role=role,
            prompt_tokens=0,
            completion_tokens=0,
        )


class _FaultingProvider:
    """A session provider whose model call fails."""

    def complete(
        self,
        *,
        messages: Sequence[ProviderChatMessage],
        role: Role,
        tools: Sequence[Mapping[str, object]] | None = None,
        response_format: Mapping[str, object] | None = None,
    ) -> ModelCompletion:
        del messages, role, tools, response_format
        raise RuntimeError("provider down")


def _phase_seeds(
    chat: ChatService, chat_id: str, execution_id: str
) -> list[ChatMessage]:
    return [
        message
        for message in chat.list_messages(WORKSPACE, chat_id)
        if message.execution_id == execution_id
        and message.data is not None
        and message.data.get("kind") in {"phase_seed", "agent_prompt"}
    ]


def _node_messages(chat: ChatService, chat_id: str, node_id: str) -> list[ChatMessage]:
    return [
        message
        for message in chat.list_messages(WORKSPACE, chat_id)
        if message.data is not None and message.data.get("subAgentId") == node_id
    ]


def _control(
    kit: _Kit,
    store: PersistenceSubAgentStore,
    schedule: TurnScheduler,
    request: HypothesisDevelopmentRequest,
) -> ControlToolService:
    return ControlToolService(
        lifecycle=SubAgentLifecycle(kit.chat, store=store),
        store=store,
        inputs=NodeInputService(chat=kit.chat, store=store),
        delivery=DeliveryService(kit.chat, schedule=schedule),
        phase_completions=lambda workspace_id, record, completion: (
            kit.workflow.apply_phase_completion(
                request, record.sub_agent_id, completion
            )
        ),
    )


def _runner(
    kit: _Kit,
    store: PersistenceSubAgentStore,
    control: ControlToolService,
    provider: CompletionProvider,
    *,
    lifecycle: SubAgentLifecycle,
    on_fault=None,
) -> NodeTurnRunner:
    return NodeTurnRunner(
        chat=kit.chat,
        store=store,
        provider=provider,
        registry_factory=lambda context: build_session_registry(
            context=context, control=control
        ),
        lifecycle=lifecycle,
        on_fault=on_fault,
    )


def test_agent_turn_completes_the_phase_and_spawns_the_next(tmp_path: Path) -> None:
    scheduled: list[tuple[str, str, str]] = []

    def schedule(workspace_id: str, chat_id: str, sub_agent_id: str) -> None:
        scheduled.append((workspace_id, chat_id, sub_agent_id))

    kit = _kit(tmp_path, payloads=[], results=[], turn_scheduler=schedule)
    request = _request(kit)
    store = PersistenceSubAgentStore(kit.chat.persistence)
    lifecycle = SubAgentLifecycle(kit.chat, store=store)
    control = _control(kit, store, schedule, request)

    # Launch prepares the first phase agent: one prompt turn, and a scheduled
    # turn on that agent — never on the engine.  The workflow starts in
    # hypothesis creation, and the operator's words are in its prompt.
    node_id = kit.workflow.prepare_phase(request)
    assert node_id is not None
    seeds = _phase_seeds(kit.chat, request.chat_id, request.execution_id)
    assert [seed.data["subAgentId"] for seed in seeds] == [node_id]
    # Spawning and prompting are one act: the prompt is delivered at the new
    # agent's own anchor as a user turn, and that delivery scheduled its turn.
    assert seeds[0].role is MessageRole.USER
    assert seeds[0].data["kind"] == "agent_prompt"
    assert IDEAS in seeds[0].body
    assert "request_phase_completion" in seeds[0].body
    # Creation's prompt requires the operator's confirmation before completing.
    assert "REQUIRES the operator's confirmation" in seeds[0].body
    assert scheduled == [(WORKSPACE, request.chat_id, node_id)]

    # The phase agent's own session turn calls the completion tool.
    runner = _runner(
        kit,
        store,
        control,
        _QueuedCompletionProvider(
            (
                "request_phase_completion",
                {
                    "completion": {
                        "target": "falsification_trials",
                        "hypothesis": {"claim": "momentum persists"},
                    }
                },
            )
        ),
        lifecycle=lifecycle,
    )
    result = runner.run(WORKSPACE, request.chat_id, node_id)
    assert result.status == "completed"

    # The accepted contract applied the transition and prepared the next agent.
    execution = kit.chat.get_execution(WORKSPACE, request.execution_id)
    assert execution.phase == HypothesisDevelopmentPhase.FALSIFICATION_TRIALS.value
    seeds = _phase_seeds(kit.chat, request.chat_id, request.execution_id)
    assert len(seeds) == 2
    next_node = str(seeds[1].data["subAgentId"])
    assert next_node != node_id
    assert (WORKSPACE, request.chat_id, next_node) in scheduled

    # Creation is a conversation (interruptible while it is working); trials is
    # one-shot.  A completed node no longer accepts input.
    creation = store.get_sub_agent(WORKSPACE, node_id)
    trials = store.get_sub_agent(WORKSPACE, next_node)
    assert creation is not None and creation.kind is SubAgentKind.CONVERSATION
    assert trials is not None and trials.kind is SubAgentKind.ONE_SHOT
    assert creation.accepts_input is False

    # The workflow node is a record, not a conversation: it is never scheduled.
    assert creation.parent_sub_agent_id is not None
    assert [entry[2] for entry in scheduled] == [node_id, next_node]

    # The engine never called a model: the only model turn was the agent's own.
    assert kit.provider.prompts == []


def test_rejected_contract_is_repaired_in_turn(tmp_path: Path) -> None:
    scheduled: list[tuple[str, str, str]] = []

    def schedule(workspace_id: str, chat_id: str, sub_agent_id: str) -> None:
        scheduled.append((workspace_id, chat_id, sub_agent_id))

    kit = _kit(tmp_path, payloads=[], results=[], turn_scheduler=schedule)
    request = _request(kit)
    store = PersistenceSubAgentStore(kit.chat.persistence)
    lifecycle = SubAgentLifecycle(kit.chat, store=store)
    control = _control(kit, store, schedule, request)
    creation_node = kit.workflow.prepare_phase(request)
    assert creation_node is not None

    # The creation phase's contract requires a hypothesis object; the first
    # contract is missing it and the second repairs it.
    runner = _runner(
        kit,
        store,
        control,
        _QueuedCompletionProvider(
            (
                "request_phase_completion",
                {"completion": {"target": "falsification_trials"}},
            ),
            (
                "request_phase_completion",
                {
                    "completion": {
                        "target": "falsification_trials",
                        "hypothesis": {"claim": "momentum persists"},
                    }
                },
            ),
        ),
        lifecycle=lifecycle,
    )
    runner.run(WORKSPACE, request.chat_id, creation_node)

    failures = [
        message
        for message in _node_messages(kit.chat, request.chat_id, creation_node)
        if message.tool_result is not None and message.tool_result.get("ok") is False
    ]
    assert failures
    assert "hypothesis" in str(failures[0].tool_result["failure"])
    execution = kit.chat.get_execution(WORKSPACE, request.execution_id)
    assert execution.phase == HypothesisDevelopmentPhase.FALSIFICATION_TRIALS.value


def test_phase_agent_fault_fails_the_execution(tmp_path: Path) -> None:
    scheduled: list[tuple[str, str, str]] = []

    def schedule(workspace_id: str, chat_id: str, sub_agent_id: str) -> None:
        scheduled.append((workspace_id, chat_id, sub_agent_id))

    kit = _kit(tmp_path, payloads=[], results=[], turn_scheduler=schedule)
    request = _request(kit)
    store = PersistenceSubAgentStore(kit.chat.persistence)
    lifecycle = SubAgentLifecycle(kit.chat, store=store)
    control = _control(kit, store, schedule, request)
    node_id = kit.workflow.prepare_phase(request)
    assert node_id is not None

    runner = _runner(
        kit,
        store,
        control,
        _FaultingProvider(),
        lifecycle=lifecycle,
        on_fault=lambda record, error: kit.workflow.fail_execution(
            request, reason=str(error)
        ),
    )
    result = runner.run(WORKSPACE, request.chat_id, node_id)

    assert result.status == "failed"
    node = store.get_sub_agent(WORKSPACE, node_id)
    assert node is not None and node.status is SubAgentStatus.FAILED
    execution = kit.chat.get_execution(WORKSPACE, request.execution_id)
    assert execution.state.value == "failed"
    failures = [
        message
        for message in kit.chat.list_messages(WORKSPACE, request.chat_id)
        if message.data is not None
        and message.data.get("datumType") == "workflow_outcome"
        and message.data.get("ok") is False
    ]
    assert failures
    tool_result = failures[0].tool_result
    assert tool_result is not None
    structured = tool_result["structured"]
    assert structured["status"] == "failed"
    assert structured["phase"]


def test_a_text_only_agent_turn_starts_and_keeps_the_node_running(
    tmp_path: Path,
) -> None:
    scheduled: list[tuple[str, str, str]] = []

    def schedule(workspace_id: str, chat_id: str, sub_agent_id: str) -> None:
        scheduled.append((workspace_id, chat_id, sub_agent_id))

    kit = _kit(tmp_path, payloads=[], results=[], turn_scheduler=schedule)
    request = _request(kit)
    store = PersistenceSubAgentStore(kit.chat.persistence)
    lifecycle = SubAgentLifecycle(kit.chat, store=store)
    control = _control(kit, store, schedule, request)
    node_id = kit.workflow.prepare_phase(request)
    assert node_id is not None
    pending = store.get_sub_agent(WORKSPACE, node_id)
    assert pending is not None and pending.status is SubAgentStatus.PENDING

    # A turn that ends in text (no tool call) must still start the agent: the
    # runner starts the node it runs, so the pending spinner cannot survive it.
    runner = _runner(kit, store, control, _RecordingProvider(), lifecycle=lifecycle)
    result = runner.run(WORKSPACE, request.chat_id, node_id)

    assert result.status == "completed"
    record = store.get_sub_agent(WORKSPACE, node_id)
    assert record is not None and record.status is SubAgentStatus.RUNNING
    # A conversational agent stays interruptible while it is running.
    assert record.accepts_input is True


def test_creation_transcript_replays_with_matching_tool_envelopes(
    tmp_path: Path,
) -> None:
    scheduled: list[tuple[str, str, str]] = []

    def schedule(workspace_id: str, chat_id: str, sub_agent_id: str) -> None:
        scheduled.append((workspace_id, chat_id, sub_agent_id))

    kit = _kit(tmp_path, payloads=[], results=[], turn_scheduler=schedule)
    request = _request(kit)
    store = PersistenceSubAgentStore(kit.chat.persistence)
    lifecycle = SubAgentLifecycle(kit.chat, store=store)
    control = _control(kit, store, schedule, request)

    creation_node = kit.workflow.prepare_phase(request)
    assert creation_node is not None

    # The creation agent asks through the wrapped tool and waits for the answer.
    _runner(
        kit,
        store,
        control,
        _QueuedCompletionProvider(
            (
                "request_workflow_input",
                {
                    "message": "Please confirm the proposal.",
                    "reason": "Operator confirmation is required.",
                },
            )
        ),
        lifecycle=lifecycle,
    ).run(WORKSPACE, request.chat_id, creation_node)
    node = store.get_sub_agent(WORKSPACE, creation_node)
    assert node is not None and node.status is SubAgentStatus.NEEDS_INPUT

    # The answered turn replays the conversation with valid tool envelopes:
    # every replayed tool reply follows the assistant tool-call that requested it.
    recording = _RecordingProvider()
    _runner(kit, store, control, recording, lifecycle=lifecycle).run(
        WORKSPACE, request.chat_id, creation_node
    )
    assert recording.tool_ids
    assert recording.bare_tool_messages == []
