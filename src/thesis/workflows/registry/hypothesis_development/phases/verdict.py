"""Verdict phase executor."""

from __future__ import annotations

from collections.abc import Mapping

from thesis.chat.contracts import (
    MessageRole,
)

from ..composers._shared import _json_document
from ..contracts.creation import HypothesisDevelopmentRequest
from ..contracts.verdict import FalsificationVerdict
from ..errors import (
    WorkflowError,
)
from ..gates.strategy import check_verdict_advances
from ..jobs import stop_for_cycle_budget
from ..piping import reset_premise
from ..primitives import (
    VERDICT_INCONCLUSIVE,
    VERDICT_REFUTED,
    VERDICT_SUPPORTED,
    _format_observed,
)
from ..services import DevelopmentEngine, services_of
from ..state import HypothesisDevelopmentState
from . import (
    _HYPOTHESIS_CREATION,
    _VERDICT,
    _ActiveSubAgent,
)
from .backtesting import run_strategy_pipeline
from .creation import advance_creation

#: Structured intent the verdict gate asks the model to return.
_VERDICT_INTENT_SCHEMA = (
    '{"action": "check_off"|"circle_back"|"ambiguous", "message": string}'
)


def advance_verdict(
    workflow: DevelopmentEngine,
    request: HypothesisDevelopmentRequest,
    state: HypothesisDevelopmentState,
) -> HypothesisDevelopmentState:
    """Apply operator input to the durable verdict conversation."""
    active = workflow._activate_orchestrator(request, _VERDICT)
    body = latest_sub_agent_user_message(workflow, request, active.sub_agent_id)
    action = interpret_verdict_intent(workflow, request, state, active, body)
    if action is None:
        return workflow._request_operator_input(
            request,
            state,
            purpose="apply the falsification verdict",
            detail=(
                "the operator's reply did not clearly check off or circle "
                "back the recorded verdict"
            ),
            active=active,
        )
    return continue_after_verdict(workflow, request, state, action=action)


def interpret_verdict_intent(
    workflow: DevelopmentEngine,
    request: HypothesisDevelopmentRequest,
    state: HypothesisDevelopmentState,
    active: _ActiveSubAgent,
    body: str,
) -> str | None:
    """Interpret the operator's verdict reply with the model, not keywords.

    The phase agent reads the operator's prose and returns a structured
    intent. Ambiguity or an unavailable model returns ``None`` so the
    workflow asks the operator again instead of guessing.
    """
    verdict = state.verdict
    if verdict is None:
        return None
    prompt_for = workflow._prompt_builder(
        workspace_scope=request.workspace_id,
        role=active.role,
        context_id=f"{request.execution_id}-verdict-intent",
    )
    task = "\n".join(
        [
            "You are the workflow's verdict gate.",
            f"Recorded falsification verdict: {verdict.outcome}. {verdict.reason}",
            f"Operator's latest message: {body}",
            "",
            "Decide the operator's intent: 'check_off' when the reply clearly "
            "accepts or applies the recorded verdict, 'circle_back' when it "
            "clearly asks to revise the hypothesis, or 'ambiguous' when it is "
            "unclear. Reply with exactly one JSON object and nothing else:",
            _VERDICT_INTENT_SCHEMA,
        ]
    )
    try:
        services = services_of(workflow)
        assert services.router is not None
        invocation = services.router.invoke(
            services.research_role,
            prompt_for(task),
            response_format={"type": "json_object"},
        )
        document = _json_document(invocation, "verdict intent")
    except Exception:  # noqa: BLE001 - an unavailable model is not a decision
        return None
    action = document.get("action")
    if isinstance(action, str) and action in {"check_off", "circle_back"}:
        return action
    return None


def continue_after_verdict(
    workflow: DevelopmentEngine,
    request: HypothesisDevelopmentRequest,
    state: HypothesisDevelopmentState,
    *,
    action: str,
) -> HypothesisDevelopmentState:
    """Apply a checked-off verdict or an explicit circle-back request."""
    verdict = state.verdict
    if verdict is None:
        raise WorkflowError("verdict review requires a recorded verdict")
    document = state.hypothesis
    if document is None:
        raise WorkflowError("verdict review requires a hypothesis")
    if action == "circle_back" or check_verdict_advances(verdict) is not None:
        state = workflow._transition(
            request,
            state,
            _HYPOTHESIS_CREATION,
            "premise refuted; returning to hypothesis creation",
        )
        workflow._post_phase_handoff(
            request,
            premise_handoff(workflow, state, verdict),
            kind="falsification_findings",
        )
        if state.hypothesis_cycles >= services_of(workflow).max_hypothesis_cycles:
            return stop_for_cycle_budget(
                workflow,
                request,
                state,
                purpose="recover from an exhausted hypothesis revision budget",
                detail=(
                    "the hypothesis revision budget of 5 attempts is exhausted "
                    "after repeated falsifications; review the falsification "
                    "history and provide operator direction"
                ),
            )
        state = reset_premise(state)
        return advance_creation(workflow, request, state)
    return run_strategy_pipeline(workflow, request, state)


def latest_sub_agent_user_message(
    workflow: DevelopmentEngine,
    request: HypothesisDevelopmentRequest,
    sub_agent_id: str,
) -> str:
    """Return the latest operator message addressed to one phase node."""
    chat = workflow.chat
    assert chat is not None
    for message in reversed(chat.list_messages(request.workspace_id, request.chat_id)):
        if message.role is not MessageRole.USER:
            continue
        data = message.data
        if isinstance(data, Mapping) and data.get("subAgentId") == sub_agent_id:
            return message.body
    return ""


def evaluate_verdict(
    workflow: DevelopmentEngine, state: HypothesisDevelopmentState
) -> FalsificationVerdict:
    document = state.hypothesis
    if document is None:
        raise WorkflowError("verdict requires a hypothesis")
    by_falsifier = {trial.falsifier_id: trial for trial in state.trials}
    missing: list[str] = []
    for falsifier in document.front_matter.falsifiers:
        key = falsifier.falsifier_id
        trial = None if key is None else by_falsifier.get(key)
        if trial is None or trial.observed is None:
            missing.append(key or falsifier.observable)
    if missing:
        return FalsificationVerdict(
            outcome=VERDICT_INCONCLUSIVE,
            reason=(
                "insufficient evidence: no numeric observation is bound for "
                f"{sorted(missing)}"
            ),
            trials=state.trials,
        )
    hits = [trial for trial in state.trials if trial.falsified]
    if hits:
        return FalsificationVerdict(
            outcome=VERDICT_REFUTED,
            reason=(
                "at least one falsifier was observed: "
                + ", ".join(sorted(trial.falsifier_id for trial in hits))
            ),
            trials=state.trials,
        )
    return FalsificationVerdict(
        outcome=VERDICT_SUPPORTED,
        reason=(
            "no falsifier was observed across the planned trials; the "
            "hypothesis survived falsification"
        ),
        trials=state.trials,
    )


def emit_verdict_review(
    workflow: DevelopmentEngine,
    request: HypothesisDevelopmentRequest,
    state: HypothesisDevelopmentState,
) -> str:
    """Render the verdict as a conversational review, not a workflow gate."""
    verdict = state.verdict
    active = workflow.active_orchestrators.get(request.execution_id)
    if verdict is None or active is None:
        raise WorkflowError("verdict review requires an active verdict node")
    question = workflow._operator_question(
        request,
        state,
        active=active,
        purpose="review and apply the falsification verdict",
        detail=(
            f"verdict {verdict.outcome}: {verdict.reason}. The operator may "
            "check off the verdict to apply it, or circle back to revise the "
            "hypothesis."
        ),
    )
    lines = [
        "### Falsification Verdict",
        "",
        f"**{verdict.outcome.capitalize()}**: {verdict.reason}",
        "",
        question,
    ]
    workflow._deliver_operator_request(
        request,
        active,
        "\n".join(lines),
        question=question,
        verdict=verdict.outcome,
    )
    return question


def premise_handoff(
    workflow: DevelopmentEngine,
    state: HypothesisDevelopmentState,
    verdict: FalsificationVerdict,
) -> str:
    """Render falsification findings for the next hypothesis proposal turn."""
    lines = [
        f"Falsification findings from hypothesis attempt {state.hypothesis_cycles}:",
        verdict.reason,
    ]
    tests = {test.test_id: test for test in state.plan.tests} if state.plan else {}
    for trial in state.trials:
        planned = tests.get(trial.test_id)
        lines.append(
            f"- {trial.test_id}: {planned.observable if planned else trial.falsifier_id} "
            f"observed={_format_observed(trial.observed)}; "
            f"falsified={str(trial.falsified).lower()}"
        )
    lines.append(
        "Revise the statistical premise, model, horizon, or controls in response "
        "to these findings, then present the updated proposal for operator review."
    )
    return "\n".join(lines)
