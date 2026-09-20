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

from thesis.agent.runtime.control import ControlToolService
from thesis.agent.runtime.conversation import Conversations
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
from thesis.research.models import ResearchModelRef
from thesis.workflows.backtests import BacktestRunResult
from thesis.workflows.registry.hypothesis_development import (
    HypothesisDevelopmentRequest,
)
from thesis.workflows.registry.hypothesis_development.phases import (
    HypothesisDevelopmentPhase,
)
from thesis.workflows.research_models import ResearchModelRunResult

from ._support import IDEAS, WORKSPACE, _hypothesis_envelope, _Kit, _kit, _request


class _FakeStudyRunner:
    """A research-model port that returns one completed study run."""

    def run(self, request: object) -> ResearchModelRunResult:
        del request
        return ResearchModelRunResult(
            model_reference=ResearchModelRef(
                workspace_id=WORKSPACE,
                model_id="momentum-model",
                content_sha256="a" * 64,
            ),
            indicator_references=(),
            backtest=BacktestRunResult(
                job_id="study-1",
                state="completed",
                metrics={"sharpe": 1.0, "sample_valid": 1.0},
            ),
            workspace_artifact="workspace-model:momentum-model",
            runtime_configuration={},
            fitted_reference=None,
        )


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
    request: HypothesisDevelopmentRequest,
) -> ControlToolService:
    from thesis.agent.runtime.conversation import Conversations

    return ControlToolService(
        conversations=Conversations(
            chat=kit.chat,
            store=store,
            lifecycle=SubAgentLifecycle(kit.chat, store=store),
        ),
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
) -> Conversations:
    return Conversations(
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
    kit = _kit(
        tmp_path,
        payloads=[],
        results=[],
        research_runner=_FakeStudyRunner(),
    )
    request = _request(kit)
    store = PersistenceSubAgentStore(kit.chat.persistence)
    lifecycle = SubAgentLifecycle(kit.chat, store=store)
    control = _control(kit, store, request)

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
    assert kit.conversations.in_flight(WORKSPACE, request.chat_id, node_id) is True

    # The phase agent's own session turn calls the completion tool with the
    # hypothesis contract; the deterministic trials phase then runs the study
    # and hands its trials contract to the verdict agent.
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
                        "hypothesis": _hypothesis_envelope(),
                    }
                },
            )
        ),
        lifecycle=lifecycle,
    )
    result = runner.run_node_turn(WORKSPACE, request.chat_id, node_id)
    assert result.status == "completed"

    execution = kit.chat.get_execution(WORKSPACE, request.execution_id)
    assert execution.phase == HypothesisDevelopmentPhase.VERDICT.value
    seeds = _phase_seeds(kit.chat, request.chat_id, request.execution_id)
    assert len(seeds) == 2
    next_node = str(seeds[1].data["subAgentId"])
    assert next_node != node_id
    # The verdict agent's prompt carries the trials the deterministic phase ran.
    assert '"trials"' in seeds[1].body
    assert "runState" in seeds[1].body
    assert kit.conversations.in_flight(WORKSPACE, request.chat_id, next_node) is True

    # Creation is closed once it completed; the verdict agent is a
    # conversation, so it accepts the operator's input from birth.
    creation = store.get_sub_agent(WORKSPACE, node_id)
    verdict = store.get_sub_agent(WORKSPACE, next_node)
    assert creation is not None and creation.kind is SubAgentKind.CONVERSATION
    assert creation.accepts_input is False
    assert verdict is not None and verdict.kind is SubAgentKind.CONVERSATION
    assert verdict.accepts_input is True

    # The workflow node is a record, not a conversation: it is never scheduled.
    assert creation.parent_sub_agent_id is not None
    node_record = store.get_sub_agent(WORKSPACE, creation.parent_sub_agent_id)
    assert node_record is not None
    assert (
        kit.conversations.in_flight(
            WORKSPACE, request.chat_id, node_record.sub_agent_id
        )
        is False
    )
    assert len(kit.woke) == 2

    # The engine never called a model: the trials run was deterministic and the
    # only model turn was the creation agent's own.
    assert kit.provider.prompts == []


def test_rejected_contract_is_repaired_in_turn(tmp_path: Path) -> None:
    kit = _kit(
        tmp_path,
        payloads=[],
        results=[],
        research_runner=_FakeStudyRunner(),
    )
    request = _request(kit)
    store = PersistenceSubAgentStore(kit.chat.persistence)
    lifecycle = SubAgentLifecycle(kit.chat, store=store)
    control = _control(kit, store, request)
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
                        "hypothesis": _hypothesis_envelope(),
                    }
                },
            ),
        ),
        lifecycle=lifecycle,
    )
    runner.run_node_turn(WORKSPACE, request.chat_id, creation_node)

    failures = [
        message
        for message in _node_messages(kit.chat, request.chat_id, creation_node)
        if message.tool_result is not None and message.tool_result.get("ok") is False
    ]
    assert failures
    assert "hypothesis" in str(failures[0].tool_result["failure"])
    execution = kit.chat.get_execution(WORKSPACE, request.execution_id)
    assert execution.phase == HypothesisDevelopmentPhase.VERDICT.value


def test_phase_agent_fault_fails_the_execution(tmp_path: Path) -> None:
    kit = _kit(tmp_path, payloads=[], results=[])
    request = _request(kit)
    store = PersistenceSubAgentStore(kit.chat.persistence)
    lifecycle = SubAgentLifecycle(kit.chat, store=store)
    control = _control(kit, store, request)
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
    result = runner.run_node_turn(WORKSPACE, request.chat_id, node_id)

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
    kit = _kit(tmp_path, payloads=[], results=[])
    request = _request(kit)
    store = PersistenceSubAgentStore(kit.chat.persistence)
    lifecycle = SubAgentLifecycle(kit.chat, store=store)
    control = _control(kit, store, request)
    node_id = kit.workflow.prepare_phase(request)
    assert node_id is not None
    pending = store.get_sub_agent(WORKSPACE, node_id)
    assert pending is not None and pending.status is SubAgentStatus.PENDING

    # A turn that ends in text (no tool call) must still start the agent: the
    # runner starts the node it runs, so the pending spinner cannot survive it.
    runner = _runner(kit, store, control, _RecordingProvider(), lifecycle=lifecycle)
    result = runner.run_node_turn(WORKSPACE, request.chat_id, node_id)

    assert result.status == "completed"
    record = store.get_sub_agent(WORKSPACE, node_id)
    assert record is not None and record.status is SubAgentStatus.RUNNING
    # A conversational agent stays interruptible while it is running.
    assert record.accepts_input is True


def test_creation_transcript_replays_with_matching_tool_envelopes(
    tmp_path: Path,
) -> None:
    kit = _kit(tmp_path, payloads=[], results=[])
    request = _request(kit)
    store = PersistenceSubAgentStore(kit.chat.persistence)
    lifecycle = SubAgentLifecycle(kit.chat, store=store)
    control = _control(kit, store, request)

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
    ).run_node_turn(WORKSPACE, request.chat_id, creation_node)
    node = store.get_sub_agent(WORKSPACE, creation_node)
    assert node is not None and node.status is SubAgentStatus.NEEDS_INPUT

    # The answered turn replays the conversation with valid tool envelopes:
    # every replayed tool reply follows the assistant tool-call that requested it.
    recording = _RecordingProvider()
    _runner(kit, store, control, recording, lifecycle=lifecycle).run_node_turn(
        WORKSPACE, request.chat_id, creation_node
    )
    assert recording.tool_ids
    assert recording.bare_tool_messages == []


def test_completing_into_a_terminal_phase_finishes_the_execution(
    tmp_path: Path,
) -> None:
    """A phase completing into `failed` (or `completed`) must not try to spawn
    an agent for the terminal phase; the execution is finished instead."""
    kit = _kit(tmp_path, payloads=[], results=[])
    request = _request(kit)
    store = PersistenceSubAgentStore(kit.chat.persistence)
    lifecycle = SubAgentLifecycle(kit.chat, store=store)
    control = _control(kit, store, request)

    creation_node = kit.workflow.prepare_phase(request)
    assert creation_node is not None

    # The creation agent completes with a terminal target (`failed`).
    runner = _runner(
        kit,
        store,
        control,
        _QueuedCompletionProvider(
            (
                "request_phase_completion",
                {
                    "completion": {
                        "target": "failed",
                        "hypothesis": _hypothesis_envelope(),
                    }
                },
            )
        ),
        lifecycle=lifecycle,
    )
    result = runner.run_node_turn(WORKSPACE, request.chat_id, creation_node)

    # The tool call succeeded (no KeyError burning the iteration budget).
    assert result.status == "completed"

    # The execution is finished as failed, not left running at a terminal phase.
    execution = kit.chat.get_execution(WORKSPACE, request.execution_id)
    assert execution.state.value == "failed"
    assert execution.finished_at is not None

    # No agent was spawned for the terminal phase (only the creation node
    # and the workflow node exist).
    nodes = store.list_sub_agents(WORKSPACE)
    roles = {node.role for node in nodes}
    assert all("orchestrator" not in role or "creation" in role for role in roles)
