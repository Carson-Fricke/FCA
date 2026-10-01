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

import pytest

from thesis.agent.runtime.control import ControlToolService
from thesis.agent.runtime.conversation import Conversations
from thesis.agent.runtime.lifecycle import SubAgentLifecycle
from thesis.agent.runtime.policy import AnsweringActor, OperatorActorKind
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
from thesis.workflows.core.workflow import PhaseCompletionRejected
from thesis.workflows.registry.hypothesis_development import (
    HypothesisDevelopmentRequest,
    rebuild_state,
)
from thesis.workflows.registry.hypothesis_development.phases import (
    HypothesisDevelopmentPhase,
)
from thesis.workflows.research_models import ResearchModelRunResult

from ._support import (
    IDEAS,
    WORKSPACE,
    _hypothesis_envelope,
    _Kit,
    _kit,
    _request,
)


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


def _message_data(message: ChatMessage) -> Mapping[str, object]:
    data = message.data
    assert data is not None
    return data


def _tool_result(message: ChatMessage) -> Mapping[str, object]:
    result = message.tool_result
    assert result is not None
    return result


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
    """The full agent-first loop over the new contracts.

    Launch → the creation agent proposes → `request_workflow_input` → the
    operator confirms → `request_phase_completion` (target
    falsification_trials) → the trials run deterministically (no trials node,
    workflow-scoped events) → the verdict agent is spawned with the trials in
    its seed → the verdict completes into the strategy chain.  The execution
    store is authoritative throughout: every transition is committed there
    with its completion contract.
    """
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
    assert [_message_data(seed)["subAgentId"] for seed in seeds] == [node_id]
    assert seeds[0].role is MessageRole.USER
    assert _message_data(seeds[0])["kind"] == "agent_prompt"
    assert IDEAS in seeds[0].body
    assert "request_phase_completion" in seeds[0].body
    assert "REQUIRES the operator's confirmation" in seeds[0].body
    assert "request_workflow_input" in seeds[0].body
    assert kit.conversations.in_flight(WORKSPACE, request.chat_id, node_id) is True

    # The creation agent's exact agent-facing policy: the drafting tool it is
    # configured with plus the structural phase control tools. Nothing else is
    # advertised, so a harness or backtest tool can never be called here.
    creation = store.get_sub_agent(WORKSPACE, node_id)
    assert creation is not None
    assert set(creation.allowed_tools) == {
        "validate_study",
        "request_workflow_input",
        "request_phase_completion",
    }

    # The creation agent proposes and asks the operator through its own
    # wrapped tool: the node enters needs_input and schedules nothing.
    runner = _runner(
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
    )
    result = runner.run_node_turn(WORKSPACE, request.chat_id, node_id)
    assert result.status == "completed"
    creation = store.get_sub_agent(WORKSPACE, node_id)
    assert creation is not None and creation.status is SubAgentStatus.NEEDS_INPUT

    # The operator confirms through the admission layer, then the agent's
    # resumed turn completes the phase.
    kit.conversations.answer(
        WORKSPACE,
        request.chat_id,
        node_id,
        body="Confirmed — run the falsification trials.",
        actor=AnsweringActor("owner-1", OperatorActorKind.HUMAN_OPERATOR),
    )
    resumed = store.get_sub_agent(WORKSPACE, node_id)
    assert resumed is not None and resumed.status is SubAgentStatus.RUNNING
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

    # The workflow owns the completion: the node closes exactly once, its
    # durable result is the completion contract, and the phase tool never
    # re-completes it outside the transition.
    creation_record = store.get_sub_agent(WORKSPACE, node_id)
    assert creation_record is not None
    assert creation_record.status is SubAgentStatus.COMPLETED
    assert isinstance(creation_record.result, Mapping)
    assert "hypothesis" in creation_record.result
    completed_events = [
        event
        for event in kit.workflow.observe(WORKSPACE, request.execution_id).events
        if event.type.value == "sub_agent.completed"
        and event.payload.get("phase") == "hypothesis_creation"
    ]
    assert len(completed_events) == 1

    execution = kit.chat.get_execution(WORKSPACE, request.execution_id)
    assert execution.phase == HypothesisDevelopmentPhase.VERDICT.value
    seeds = _phase_seeds(kit.chat, request.chat_id, request.execution_id)
    assert len(seeds) == 2
    next_node = str(_message_data(seeds[1])["subAgentId"])
    assert next_node != node_id
    # The verdict agent's prompt carries the trials the deterministic phase ran.
    assert '"trials"' in seeds[1].body
    assert "runState" in seeds[1].body
    assert kit.conversations.in_flight(WORKSPACE, request.chat_id, next_node) is True

    # The deterministic trials phase is not a sub-agent: no trials record
    # exists in the tree and no sub_agent.* event names the phase.
    records = store.list_sub_agents(WORKSPACE)
    assert all(record.phase != "falsification_trials" for record in records)
    assert all("falsification trials" not in record.role.lower() for record in records)
    events = kit.workflow.observe(WORKSPACE, request.execution_id).events
    assert not [
        event
        for event in events
        if event.type.value.startswith("sub_agent.")
        and event.payload.get("phase") == "falsification_trials"
    ]
    # The trials run is observable as workflow-scoped events only.
    progress = [event for event in events if event.type.value == "workflow.progress"]
    assert progress
    for event in progress:
        assert event.payload["phase"] == "falsification_trials"
        assert "subAgentId" not in event.payload
    phase_results = [
        event for event in events if event.type.value == "workflow.phase_result"
    ]
    assert len(phase_results) == 1
    artifact = phase_results[0].payload["artifact"]
    assert isinstance(artifact, Mapping)
    assert artifact["type"] == "falsification_report"
    assert artifact["totalCount"] == 1

    # The execution store is authoritative: both transitions committed with
    # their completion contracts, and the fold restores the full typed state.
    document = kit.execution_store.get(request.execution_id)
    assert [(item.from_phase, item.to_phase) for item in document.transitions] == [
        ("hypothesis_creation", "falsification_trials"),
        ("falsification_trials", "verdict"),
    ]
    rebuilt = rebuild_state(
        WORKSPACE,
        request.execution_id,
        document.transitions,
        initial_phase=HypothesisDevelopmentPhase.HYPOTHESIS_CREATION,
    )
    assert rebuilt.phase is HypothesisDevelopmentPhase.VERDICT
    assert rebuilt.draft is not None
    assert rebuilt.draft.claim == _hypothesis_envelope()["claim"]
    assert [trial.run_state for trial in rebuilt.trials] == ["completed"]

    # Creation is closed once it completed; the verdict agent is a
    # conversation, so it accepts the operator's input from birth.
    creation = store.get_sub_agent(WORKSPACE, node_id)
    verdict = store.get_sub_agent(WORKSPACE, next_node)
    assert creation is not None and creation.kind is SubAgentKind.CONVERSATION
    assert creation.accepts_input is False
    assert verdict is not None and verdict.kind is SubAgentKind.CONVERSATION
    assert verdict.accepts_input is True

    # The verdict agent must ask before completing: its seed carries the
    # completion protocol, and its first turn proposes the verdict and calls
    # request_workflow_input instead of completing.
    assert "request_workflow_input" in seeds[1].body
    assert "hypothesis_creation" in seeds[1].body
    runner = _runner(
        kit,
        store,
        control,
        _QueuedCompletionProvider(
            (
                "request_workflow_input",
                {
                    "message": "The falsifier survived; propose strategy design.",
                    "reason": "Operator confirmation is required.",
                },
            )
        ),
        lifecycle=lifecycle,
    )
    result = runner.run_node_turn(WORKSPACE, request.chat_id, next_node)
    assert result.status == "completed"
    verdict = store.get_sub_agent(WORKSPACE, next_node)
    assert verdict is not None and verdict.status is SubAgentStatus.NEEDS_INPUT

    # The operator confirms through the admission layer, then the agent's
    # resumed turn completes the phase; the workflow advances into the
    # strategy chain on top of the store fold.
    kit.conversations.answer(
        WORKSPACE,
        request.chat_id,
        next_node,
        body="Confirmed — proceed to strategy design.",
        actor=AnsweringActor("owner-1", OperatorActorKind.HUMAN_OPERATOR),
    )
    runner = _runner(
        kit,
        store,
        control,
        _QueuedCompletionProvider(
            (
                "request_phase_completion",
                {
                    "completion": {
                        "verdict": {
                            "outcome": "supported",
                            "reason": "the planned falsifier survived the trials",
                        },
                        "target": "strategy_design",
                    }
                },
            )
        ),
        lifecycle=lifecycle,
    )
    result = runner.run_node_turn(WORKSPACE, request.chat_id, next_node)
    assert result.status == "completed"

    execution = kit.chat.get_execution(WORKSPACE, request.execution_id)
    assert execution.phase == HypothesisDevelopmentPhase.STRATEGY_DESIGN.value
    document = kit.execution_store.get(request.execution_id)
    assert [item.to_phase for item in document.transitions] == [
        "falsification_trials",
        "verdict",
        "strategy_design",
    ]
    rebuilt = rebuild_state(
        WORKSPACE,
        request.execution_id,
        document.transitions,
        initial_phase=HypothesisDevelopmentPhase.HYPOTHESIS_CREATION,
    )
    assert rebuilt.verdict is not None
    assert rebuilt.verdict.outcome == "supported"

    # The engine never called a model: the trials run was deterministic and
    # the only model turns were the creation agent's own session turns.
    assert kit.provider.prompts == []


def test_loop_back_reseeds_the_revisited_phase(tmp_path: Path) -> None:
    """A verdict loop-back re-enters hypothesis creation with the same durable
    node.  Every entry must deliver its own phase seed instead of colliding
    with the previous entry's delivery key, so the revisited agent is woken
    with the new contract and the second verdict sees the second trials."""
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
    completion = _QueuedCompletionProvider(
        (
            "request_phase_completion",
            {
                "completion": {
                    "target": "falsification_trials",
                    "hypothesis": _hypothesis_envelope(),
                }
            },
        )
    )
    _runner(kit, store, control, completion, lifecycle=lifecycle).run_node_turn(
        WORKSPACE, request.chat_id, creation_node
    )
    seeds = _phase_seeds(kit.chat, request.chat_id, request.execution_id)
    assert len(seeds) == 2
    verdict_node = str(_message_data(seeds[1])["subAgentId"])

    # The operator loops back: the verdict completes into a revised creation.
    _runner(
        kit,
        store,
        control,
        _QueuedCompletionProvider(
            (
                "request_phase_completion",
                {
                    "completion": {
                        "target": "hypothesis_creation",
                        "verdict": {
                            "outcome": "refuted",
                            "reason": "the planned falsifier fired",
                        },
                    }
                },
            )
        ),
        lifecycle=lifecycle,
    ).run_node_turn(WORKSPACE, request.chat_id, verdict_node)
    execution = kit.chat.get_execution(WORKSPACE, request.execution_id)
    assert execution.phase == HypothesisDevelopmentPhase.HYPOTHESIS_CREATION.value

    # The creation node is reused, but the second entry carries its own seed:
    # a compact revision brief with the verdict and the recorded trial
    # findings. The original instructions and the previous proposal are
    # already in the conversation, so the brief stays well under half the
    # first seed instead of repeating the phase instructions.
    seeds = _phase_seeds(kit.chat, request.chat_id, request.execution_id)
    assert len(seeds) == 3
    revisited = seeds[2]
    assert _message_data(revisited)["subAgentId"] == creation_node
    assert (
        _message_data(revisited)["deliveryKey"]
        != _message_data(seeds[0])["deliveryKey"]
    )
    assert "Loop-back revision" in revisited.body
    assert "do NOT reply that the phase is complete" in revisited.body
    assert "the planned falsifier fired" in revisited.body
    assert "Falsification trials findings:" in revisited.body
    assert "sharpe" in revisited.body
    assert "request_workflow_input" in revisited.body
    assert len(revisited.body) * 2 <= len(seeds[0].body)
    assert (
        kit.conversations.in_flight(WORKSPACE, request.chat_id, creation_node) is True
    )

    # The second cycle runs: creation completes again, the trials rerun, and
    # the second verdict entry is seeded with the second trial contract.
    _runner(kit, store, control, completion, lifecycle=lifecycle).run_node_turn(
        WORKSPACE, request.chat_id, creation_node
    )
    seeds = _phase_seeds(kit.chat, request.chat_id, request.execution_id)
    assert len(seeds) == 4
    second_verdict = seeds[3]
    assert _message_data(second_verdict)["subAgentId"] == verdict_node
    assert (
        _message_data(second_verdict)["deliveryKey"]
        != _message_data(seeds[1])["deliveryKey"]
    )
    assert kit.conversations.in_flight(WORKSPACE, request.chat_id, verdict_node) is True


def test_strategy_design_is_a_conversation(tmp_path: Path) -> None:
    """The design phase explains the strategy in prose, asks the operator, and
    only completes after the operator's confirmation."""
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
    _runner(
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
    ).run_node_turn(WORKSPACE, request.chat_id, creation_node)

    # The verdict completes into strategy design.
    seeds = _phase_seeds(kit.chat, request.chat_id, request.execution_id)
    verdict_node = str(_message_data(seeds[1])["subAgentId"])
    _runner(
        kit,
        store,
        control,
        _QueuedCompletionProvider(
            (
                "request_phase_completion",
                {
                    "completion": {
                        "target": "strategy_design",
                        "verdict": {
                            "outcome": "supported",
                            "reason": "the planned falsifiers survived",
                        },
                    }
                },
            )
        ),
        lifecycle=lifecycle,
    ).run_node_turn(WORKSPACE, request.chat_id, verdict_node)
    execution = kit.chat.get_execution(WORKSPACE, request.execution_id)
    assert execution.phase == HypothesisDevelopmentPhase.STRATEGY_DESIGN.value

    # The design agent is conversational and its seed demands a prose proposal.
    seeds = _phase_seeds(kit.chat, request.chat_id, request.execution_id)
    design_node = str(_message_data(seeds[2])["subAgentId"])
    design = store.get_sub_agent(WORKSPACE, design_node)
    assert design is not None and design.accepts_input is True
    assert set(design.allowed_tools) == {
        "author_strategy",
        "request_workflow_input",
        "request_phase_completion",
    }
    assert "StrategyProgram" in seeds[2].body
    assert "export_strategy_program!" in seeds[2].body
    assert "author_strategy" in seeds[2].body
    assert "### Strategy" in seeds[2].body
    assert "Falsifier Re-checks" in seeds[2].body
    assert "request_workflow_input" in seeds[2].body
    assert "sharpe" in seeds[2].body

    # The design agent proposes and asks; nothing advances yet.
    runner = _runner(
        kit,
        store,
        control,
        _QueuedCompletionProvider(
            (
                "request_workflow_input",
                {
                    "message": "Confirm the strategy design.",
                    "reason": "Operator confirmation is required.",
                },
            )
        ),
        lifecycle=lifecycle,
    )
    result = runner.run_node_turn(WORKSPACE, request.chat_id, design_node)
    assert result.status == "completed"
    design = store.get_sub_agent(WORKSPACE, design_node)
    assert design is not None and design.status is SubAgentStatus.NEEDS_INPUT
    execution = kit.chat.get_execution(WORKSPACE, request.execution_id)
    assert execution.phase == HypothesisDevelopmentPhase.STRATEGY_DESIGN.value

    # The operator confirms; the resumed turn completes into backtesting.
    kit.conversations.answer(
        WORKSPACE,
        request.chat_id,
        design_node,
        body="Confirmed — backtest it.",
        actor=AnsweringActor("owner-1", OperatorActorKind.HUMAN_OPERATOR),
    )
    _runner(
        kit,
        store,
        control,
        _QueuedCompletionProvider(
            (
                "request_phase_completion",
                {
                    "completion": {
                        "target": "strategy_backtesting",
                        "strategy": {
                            "artifactPath": "strategies/momentum/1.0.0",
                            "bundleSha256": "a" * 64,
                            "strategyId": "momentum",
                            "version": "1.0.0",
                        },
                    }
                },
            )
        ),
        lifecycle=lifecycle,
    ).run_node_turn(WORKSPACE, request.chat_id, design_node)
    execution = kit.chat.get_execution(WORKSPACE, request.execution_id)
    assert execution.phase == HypothesisDevelopmentPhase.STRATEGY_BACKTESTING.value


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
    assert "hypothesis" in str(_tool_result(failures[0])["failure"])
    execution = kit.chat.get_execution(WORKSPACE, request.execution_id)
    assert execution.phase == HypothesisDevelopmentPhase.VERDICT.value


def test_deep_invalid_contract_is_rejected_and_repaired_in_turn(tmp_path: Path) -> None:
    """A contract that passes the shallow check but is deeply invalid is
    rejected before any transition commits: every problem is reported at once
    through the repair channel, the phase never moves on a rejected handoff,
    and the repaired contract still completes the workflow."""
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

    invalid = _hypothesis_envelope()
    invalid["claim"] = "   "
    invalid["model"] = {"modelId": 7}
    invalid["falsifiers"] = [
        {
            "falsifierId": "f1",
            "observable": "sharpe",
            "condition": "below",
            "threshold": 0.0,
            "test": "Run the momentum probe.",
        }
    ]

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
                        "hypothesis": invalid,
                    }
                },
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
    result = runner.run_node_turn(WORKSPACE, request.chat_id, creation_node)

    # The rejected call was an ordinary tool result, so the turn ran on and
    # the repaired contract completed the phase.
    assert result.status == "completed"
    failures = [
        message
        for message in _node_messages(kit.chat, request.chat_id, creation_node)
        if message.tool_result is not None and message.tool_result.get("ok") is False
    ]
    assert failures
    failure = str(_tool_result(failures[0])["failure"])
    # Every missing or wrongly-typed field is reported in one deterministic
    # rejection, so the repair turn is precise.
    assert "completion contract rejected with" in failure
    assert "hypothesis.claim: must be non-empty text" in failure
    assert "hypothesis.model.modelId: must be text, got int" in failure
    assert "hypothesis.model.version: required non-empty text is missing" in failure
    assert (
        "hypothesis.falsifiers[0].condition: Input should be 'gte', 'lte', 'gt' "
        "or 'lt'" in failure
    )
    # The handoff never half-committed: the second call was applied as the
    # creation phase's completion and the workflow reached the verdict phase.
    execution = kit.chat.get_execution(WORKSPACE, request.execution_id)
    assert execution.phase == HypothesisDevelopmentPhase.VERDICT.value


def test_output_mismatch_is_repairable_before_trials(tmp_path: Path) -> None:
    kit = _kit(tmp_path, payloads=[], results=[], research_runner=_FakeStudyRunner())
    request = _request(kit)
    node_id = kit.workflow.prepare_phase(request)
    assert node_id is not None
    hypothesis = _hypothesis_envelope()
    model = hypothesis["model"]
    assert isinstance(model, dict)
    model["outputs"] = ["residual_ar1"]

    with pytest.raises(PhaseCompletionRejected, match="newly authored model.outputs"):
        kit.workflow.apply_phase_completion(
            request,
            node_id,
            {"target": "falsification_trials", "hypothesis": hypothesis},
        )

    assert kit.execution_store.get(request.execution_id).transitions == ()
    assert (
        kit.chat.get_execution(WORKSPACE, request.execution_id).phase
        == "hypothesis_creation"
    )
    assert len(_phase_seeds(kit.chat, request.chat_id, request.execution_id)) == 1

    kit.workflow.apply_phase_completion(
        request,
        node_id,
        {"target": "falsification_trials", "hypothesis": _hypothesis_envelope()},
    )
    assert kit.chat.get_execution(WORKSPACE, request.execution_id).phase == "verdict"


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
    wakes_before_fault = len(kit.woke)
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
    assert isinstance(structured, Mapping)
    assert structured["status"] == "failed"
    assert structured["phase"]
    # The failure outcome wakes the launching conversation so its agent can
    # surface the failure; only the operator authorizes a new run.
    assert len(kit.woke) == wakes_before_fault + 1


def test_queued_turn_does_not_run_a_terminal_phase_agent(tmp_path: Path) -> None:
    kit = _kit(tmp_path, payloads=[], results=[])
    request = _request(kit)
    node_id = kit.workflow.prepare_phase(request)
    assert node_id is not None
    store = PersistenceSubAgentStore(kit.chat.persistence)
    lifecycle = SubAgentLifecycle(kit.chat, store=store)
    lifecycle.cancel(WORKSPACE, node_id)
    provider = _QueuedCompletionProvider(
        ("request_workflow_input", {"message": "Again?"})
    )
    runner = _runner(
        kit, store, _control(kit, store, request), provider, lifecycle=lifecycle
    )

    result = runner.run_node_turn(WORKSPACE, request.chat_id, node_id)

    assert provider.calls == 0
    assert result.turns == ()
    stored = lifecycle.get(WORKSPACE, node_id)
    assert stored is not None and stored.status is SubAgentStatus.CANCELLED


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
