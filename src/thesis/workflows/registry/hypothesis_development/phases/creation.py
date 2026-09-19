"""Hypothesis-creation phase executor."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from thesis.agent.router import InvocationResult
from thesis.chat.contracts import (
    MessageRole,
    RecordNotFound,
    TranscriptItemKind,
)
from thesis.execution.events import ExecutionEventType
from thesis.execution.sub_agents import (
    STATUS_COMPLETED,
)
from thesis.experiments.ledger import AttemptKind
from thesis.workflows.core.workflow import ActiveSubAgent

from ..artifacts import build_hypothesis
from ..composers.creation import JsonCreationTurnComposer
from ..contracts.creation import HypothesisDevelopmentRequest
from ..contracts.hypothesis import HypothesisDraft
from ..contracts.plan import FalsificationPlan, FalsifierTest
from ..errors import (
    CreationTurn,
    FalsificationComposeError,
    WorkflowError,
)
from ..gates.falsifiability import check_falsifiability
from ..gates.plan import check_falsification_plan
from ..gates.research_model import check_research_model
from ..ledger import append_attempt
from ..primitives import (
    _CREATION_TURN_SCHEMA,
    _FALSIFICATION_PLAN_SCHEMA,
    _HYPOTHESIS_SCHEMA,
)
from ..prompts.creation import (
    _creation_finalize_prompt,
    _creation_turn_prompt,
    _event_output_text,
)
from ..prompts.strategy import _hypothesis_context_lines
from ..services import services_of
from ..state import HypothesisDevelopmentState
from . import (
    _FALSIFICATION_TRIALS,
    _HYPOTHESIS_CREATION,
)

if TYPE_CHECKING:
    from thesis.workflows.core.workflow import Workflow


def run_creation_turn(
    workflow: Workflow[
        HypothesisDevelopmentRequest,
        HypothesisDevelopmentState,
        object,
    ],
    request: HypothesisDevelopmentRequest,
    state: HypothesisDevelopmentState,
) -> HypothesisDevelopmentState:
    """Run the creation turn and continue into trials when it finalized.

    This is the spec executor for the hypothesis-creation phase: it preserves
    the workflow's multi-phase turn continuation, where a creation turn that
    produced the hypothesis and its falsification plan continues into the
    falsification trials within the same turn.
    """
    from .trials import advance_trials

    state = advance_creation(workflow, request, state)
    if state.hypothesis is None or state.plan is None:
        return state
    return advance_trials(workflow, request, state)


def advance_creation(
    workflow: Workflow[
        HypothesisDevelopmentRequest,
        HypothesisDevelopmentState,
        object,
    ],
    request: HypothesisDevelopmentRequest,
    state: HypothesisDevelopmentState,
) -> HypothesisDevelopmentState:
    """Run one structured model judgment, finalizing when it advances."""
    active = workflow.ensure_orchestrator(request, _HYPOTHESIS_CREATION)
    persist_creation_seed(workflow, request, active.sub_agent_id)
    conversation = creation_conversation(workflow, request, active.sub_agent_id)
    first_turn = not any(role == "assistant" for role, _ in conversation)
    revision_turn = bool(conversation and conversation[-1][0] == "system")
    reply = creation_reply(
        workflow, request, active, conversation, first_turn=first_turn
    )
    text = reply.message.strip() or (
        "Tell me more about the regularity you expect so I can refine the model."
    )
    if not first_turn and not revision_turn and reply.advance:
        workflow.emit_orchestrator_text(request, text)
        return finalize_creation(workflow, request, state, active)
    if revision_turn:
        purpose = (
            "confirm the revised hypothesis proposal before running "
            "falsification trials"
        )
    elif reply.advance:
        purpose = "review the proposed hypothesis model before finalizing"
    else:
        purpose = "refine the hypothesis model"
    question = workflow._operator_question(
        request,
        state,
        active=active,
        purpose=purpose,
        detail=(
            f"hypothesis attempt {state.hypothesis_cycles + 1}; the proposal is "
            "ready for operator review"
            if reply.advance
            else f"hypothesis attempt {state.hypothesis_cycles + 1}; the model needs "
            "more operator context to refine the proposal"
        ),
    )
    workflow._deliver_operator_request(
        request,
        active,
        f"{text}\n\n{question}",
        question=question,
        ready=reply.advance and not revision_turn,
    )
    return replace(state, detail=question)


def finalize_creation(
    workflow: Workflow[
        HypothesisDevelopmentRequest,
        HypothesisDevelopmentState,
        object,
    ],
    request: HypothesisDevelopmentRequest,
    state: HypothesisDevelopmentState,
    active: ActiveSubAgent,
) -> HypothesisDevelopmentState:
    """Compose the structured hypothesis + plan and hand off to trials."""
    conversation = creation_conversation(workflow, request, active.sub_agent_id)
    draft, invocation = compose_creation_hypothesis(workflow, request, conversation)
    document = build_hypothesis(workflow, request, draft)
    failure = check_falsifiability(document.front_matter)
    if failure is not None:
        return ask_creation_question(workflow, request, state, active, failure)
    model_failure = check_research_model(draft.model, document.front_matter)
    if model_failure is not None:
        return ask_creation_question(workflow, request, state, active, model_failure)
    services_of(workflow).store.save(document)
    state = replace(
        state,
        draft=draft,
        hypothesis=document,
        hypothesis_cycles=state.hypothesis_cycles + 1,
        model_provenance=MappingProxyType(dict(invocation.provenance.as_dict())),
    )
    append_attempt(
        workflow,
        request,
        state,
        AttemptKind.HYPOTHESIS,
        "falsifiable hypothesis authored",
        {
            "hypothesisId": document.hypothesis_id,
            "disagreement": document.front_matter.disagreement,
            "falsifiers": [
                falsifier.model_dump(mode="json")
                for falsifier in document.front_matter.falsifiers
            ],
        },
    )
    plan = falsification_plan(workflow, request, state)
    plan_failure = check_falsification_plan(plan, document.front_matter)
    if plan_failure is not None:
        return ask_creation_question(workflow, request, state, active, plan_failure)
    state = replace(state, plan=plan)
    append_attempt(
        workflow,
        request,
        state,
        AttemptKind.PARAMETER,
        "falsification plan recorded",
        {
            "tests": [test.test_id for test in plan.tests],
            "controls": [control.control_id for control in plan.controls],
        },
    )
    workflow.emit_orchestrator_text(
        request,
        f"Hypothesis finalization recorded. {len(plan.tests)} falsification "
        "trial(s) planned.",
    )
    workflow.finish_orchestrator(request, status=STATUS_COMPLETED)
    return workflow._transition(
        request, state, _FALSIFICATION_TRIALS, "falsifiable hypothesis confirmed"
    )


def ask_creation_question(
    workflow: Workflow[
        HypothesisDevelopmentRequest,
        HypothesisDevelopmentState,
        object,
    ],
    request: HypothesisDevelopmentRequest,
    state: HypothesisDevelopmentState,
    active: ActiveSubAgent,
    question: str,
) -> HypothesisDevelopmentState:
    """Surface a blocking clarification without losing the conversation."""
    return workflow._request_operator_input(
        request,
        state,
        purpose="finalize the hypothesis model",
        detail=question,
        active=active,
    )


def creation_reply(
    workflow: Workflow[
        HypothesisDevelopmentRequest,
        HypothesisDevelopmentState,
        object,
    ],
    request: HypothesisDevelopmentRequest,
    active: ActiveSubAgent,
    conversation: list[tuple[str, str]],
    *,
    first_turn: bool,
) -> CreationTurn:
    prompt_for = workflow._prompt_builder(
        workspace_scope=request.workspace_id,
        role=active.role,
        context_id=active.sub_agent_id,
    )
    task = _creation_turn_prompt(conversation)
    turn, _ = workflow._compose_with_repair(
        request,
        services_of(workflow).research_role,
        base_task=task,
        prompt_for=prompt_for,
        compose=JsonCreationTurnComposer().compose,
        response_schema=_CREATION_TURN_SCHEMA,
        emit_activity=False,
    )
    return turn


def compose_creation_hypothesis(
    workflow: Workflow[
        HypothesisDevelopmentRequest,
        HypothesisDevelopmentState,
        object,
    ],
    request: HypothesisDevelopmentRequest,
    conversation: list[tuple[str, str]],
) -> tuple[HypothesisDraft, InvocationResult]:
    services = services_of(workflow)
    prompt_for = workflow._prompt_builder(
        workspace_scope=request.workspace_id,
        role=f"Thesis {services.research_role} hypothesis author",
        context_id=f"{request.execution_id}-finalize",
    )
    return workflow._compose_with_repair(
        request,
        services.research_role,
        base_task=_creation_finalize_prompt(conversation),
        prompt_for=prompt_for,
        compose=services.composer.compose,
        response_schema=_HYPOTHESIS_SCHEMA,
        emit_output=False,
    )


def falsification_plan(
    workflow: Workflow[
        HypothesisDevelopmentRequest,
        HypothesisDevelopmentState,
        object,
    ],
    request: HypothesisDevelopmentRequest,
    state: HypothesisDevelopmentState,
) -> FalsificationPlan:
    services = services_of(workflow)
    document = state.hypothesis
    if document is None or state.draft is None:
        raise WorkflowError("falsification planning requires a hypothesis")
    model_reference = state.draft.model.reference(request.workspace_id).key
    prompt_for = workflow._prompt_builder(
        workspace_scope=request.workspace_id,
        role=f"Thesis {services.research_role} falsification planner",
        context_id=f"{request.execution_id}-plan",
    )
    draft, _ = workflow._compose_with_repair(
        request,
        services.research_role,
        base_task="\n".join(
            [
                "Design falsification trials and negative controls for the "
                "hypothesis below. Every test binds to one declared falsifier "
                "by its falsifierId and describes how that observable is "
                "computed from historical data. Every control is a "
                "negative/placebo check (shuffled or permuted returns, an "
                "unrelated asset pair, a placebo period) that should show no "
                "regularity. The plan exists to destroy the hypothesis, not to "
                "confirm it.",
                "",
                *_hypothesis_context_lines(document, model_reference),
            ]
        ),
        prompt_for=prompt_for,
        compose=services.planner.compose,
        response_schema=_FALSIFICATION_PLAN_SCHEMA,
        emit_output=False,
    )
    falsifiers = {
        falsifier.falsifier_id: falsifier
        for falsifier in document.front_matter.falsifiers
        if falsifier.falsifier_id is not None
    }
    tests: list[FalsifierTest] = []
    for planned in draft.tests:
        falsifier = falsifiers.get(planned.falsifier_id)
        if falsifier is None:
            raise FalsificationComposeError(
                f"plan test {planned.test_id!r} references an undeclared "
                f"falsifier {planned.falsifier_id!r}"
            )
        tests.append(
            FalsifierTest(
                test_id=planned.test_id,
                falsifier_id=planned.falsifier_id,
                observable=falsifier.observable,
                condition=falsifier.condition,
                threshold=falsifier.threshold,
                description=planned.description,
            )
        )
    return FalsificationPlan(tests=tuple(tests), controls=draft.controls)


# -- durable creation conversation -------------------------------------------


def trigger_text(
    workflow: Workflow[Any, Any, Any],
    request: HypothesisDevelopmentRequest,
) -> str:
    """Return the operator's launching message, verbatim.

    The workflow seeds its first phase from the durable trigger message the
    launch is bound to.  ``request.ideas`` remains available as the derived
    routing value the launch tool supplied; it is never a paraphrase of the
    operator's own words.
    """
    chat = workflow.chat
    if chat is None:
        return request.ideas
    try:
        message = chat.get_message(request.workspace_id, request.trigger_message_id)
    except RecordNotFound:
        return request.ideas
    return message.body if message.body.strip() else request.ideas


def persist_creation_seed(
    workflow: Workflow[Any, Any, Any],
    request: HypothesisDevelopmentRequest,
    sub_agent_id: str,
) -> None:
    """Persist the launching message as the phase's first durable turn.

    The seed is one first-class user turn on the phase agent's transcript,
    carrying its origin message id, so the operator sees the request that
    started the workflow inside the phase that received it.  Idempotent:
    exactly one seed per trigger message per phase agent.
    """
    chat = workflow.chat
    if chat is None:
        return
    for message in chat.list_messages(request.workspace_id, request.chat_id):
        data = message.data
        if (
            isinstance(data, Mapping)
            and str(data.get("subAgentId")) == sub_agent_id
            and str(data.get("triggerMessageId")) == request.trigger_message_id
        ):
            return
    chat.create_message(
        request.workspace_id,
        request.chat_id,
        actor_id=request.requested_by,
        body=trigger_text(workflow, request),
        role=MessageRole.USER,
        kind=TranscriptItemKind.SUB_AGENT,
        data={
            "subAgentId": sub_agent_id,
            "turn": "seed",
            "triggerMessageId": request.trigger_message_id,
        },
    )


def creation_conversation(
    workflow: Workflow[Any, Any, Any],
    request: HypothesisDevelopmentRequest,
    sub_agent_id: str,
) -> list[tuple[str, str]]:
    """Reconstruct the durable hypothesis-creation transcript in order."""
    chat = workflow.chat
    if chat is None:
        return []
    turns: list[tuple[float, str, str]] = [
        (float("-inf"), "user", trigger_text(workflow, request))
    ]
    for message in chat.list_messages(request.workspace_id, request.chat_id):
        if message.kind is not TranscriptItemKind.SUB_AGENT:
            continue
        data = message.data
        if (
            not isinstance(data, Mapping)
            or str(data.get("subAgentId")) != sub_agent_id
            or message.role
            not in {MessageRole.USER, MessageRole.SYSTEM, MessageRole.ASSISTANT}
            or data.get("turn") == "seed"
        ):
            continue
        role = (
            "user"
            if message.role is MessageRole.USER
            else "system"
            if message.role is MessageRole.SYSTEM
            else "assistant"
        )
        turns.append((message.created_at.timestamp(), role, message.body))
    for event in chat.replay_events(request.workspace_id, request.execution_id).events:
        if event.payload.get("subAgentId") != sub_agent_id:
            continue
        if event.event_type is ExecutionEventType.SUB_AGENT_OUTPUT:
            text = _event_output_text(event.payload)
            if text:
                turns.append((event.occurred_at.timestamp(), "assistant", text))
    turns.sort(key=lambda turn: turn[0])
    return [(role, text) for _, role, text in turns]
