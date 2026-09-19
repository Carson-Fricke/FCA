"""Workflow-owned tool jobs and cycle-budget stops.

The research-model and backtest runs publish a visible tool job on the chat
transcript and record its typed result; the cycle-budget stop delivers an
operator request for an exhausted revision loop.  These are domain operations
over the general workflow object, not engine members.
"""

from __future__ import annotations

from thesis.execution.events import ExecutionEventType
from thesis.research.models import ResearchModel
from thesis.strategies.bundles import StrategyBundle
from thesis.workflows.backtests import (
    BacktestRunRequest,
    BacktestRunResult,
)
from thesis.workflows.core.workflow import Workflow
from thesis.workflows.research_models import (
    ResearchModelRunRequest,
    ResearchModelRunResult,
)

from .contracts.creation import HypothesisDevelopmentRequest
from .errors import _PhaseFailure
from .primitives import (
    _MODEL_TOOL_NAME,
    _TOOL_NAME,
    _WORKFLOW_ACTOR,
)
from .services import services_of
from .state import HypothesisDevelopmentState

__all__ = [
    "backtest",
    "run_research_model",
    "stop_for_cycle_budget",
]


def run_research_model(
    workflow: Workflow[
        HypothesisDevelopmentRequest,
        HypothesisDevelopmentState,
        object,
    ],
    request: HypothesisDevelopmentRequest,
    state: HypothesisDevelopmentState,
    *,
    model: ResearchModel,
    correlation_id: str,
    label: str,
) -> ResearchModelRunResult:
    """Publish and run one workspace model behind a visible tool job."""
    chat = workflow.chat
    assert chat is not None
    services = services_of(workflow)
    assert services.research_runner is not None
    experiment_id = services.experiment_id or f"experiment-{request.execution_id}"
    job = chat.submit_tool_job(
        request.workspace_id,
        request.execution_id,
        tool_name=_MODEL_TOOL_NAME,
        actor_id=_WORKFLOW_ACTOR,
        correlation_id=correlation_id,
    )
    chat.start_tool_job(request.workspace_id, job.tool_job_id, actor_id=_WORKFLOW_ACTOR)
    active = workflow.active_orchestrators.get(request.execution_id)
    if active is not None:
        workflow.append_sub_agent(
            request,
            ExecutionEventType.SUB_AGENT_PROGRESS,
            sub_agent_id=active.sub_agent_id,
            parent_sub_agent_id=None,
            phase=active.phase,
            role=active.role,
            context=active.context,
            accepts_input=False,
            extra={
                "kind": "tool",
                "toolCall": {
                    "callId": job.tool_job_id,
                    "tool": _MODEL_TOOL_NAME,
                },
            },
        )
    try:
        model_run = services.research_runner.run(
            ResearchModelRunRequest(
                run_id=job.correlation_id,
                workspace_id=request.workspace_id,
                model=model,
                workflow_execution_id=request.execution_id,
                experiment_id=experiment_id,
                universe=(
                    state.hypothesis.front_matter.universe
                    if state.hypothesis is not None
                    else ()
                ),
            )
        )
    except Exception as error:
        chat.record_tool_result(
            request.workspace_id,
            job.tool_job_id,
            result=None,
            actor_id=_WORKFLOW_ACTOR,
            success=False,
            error_message=str(error),
            intent_epoch=state.intent_epoch,
        )
        if active is not None:
            workflow.append_sub_agent(
                request,
                ExecutionEventType.SUB_AGENT_PROGRESS,
                sub_agent_id=active.sub_agent_id,
                parent_sub_agent_id=None,
                phase=active.phase,
                role=active.role,
                context=active.context,
                accepts_input=False,
                extra={
                    "kind": "tool",
                    "toolResult": {
                        "callId": job.tool_job_id,
                        "failure": {"message": str(error)},
                        "ok": False,
                        "structured": {},
                        "summary": "Falsification execution failed",
                        "text": str(error),
                        "tool": _MODEL_TOOL_NAME,
                    },
                },
            )
        raise
    result = model_run.backtest
    chat.record_tool_result(
        request.workspace_id,
        job.tool_job_id,
        result={
            **result.as_dict(),
            "researchModelReference": model_run.model_reference.key,
            "indicatorReferences": [
                reference.key for reference in model_run.indicator_references
            ],
            "workspaceArtifact": model_run.workspace_artifact,
            "fittedModelReference": (
                None
                if model_run.fitted_reference is None
                else model_run.fitted_reference.key
            ),
        },
        actor_id=_WORKFLOW_ACTOR,
        success=result.succeeded,
        error_message=(
            None if result.succeeded else f"state {result.state!r} ({label})"
        ),
        intent_epoch=state.intent_epoch,
    )
    if active is not None:
        workflow.append_sub_agent(
            request,
            ExecutionEventType.SUB_AGENT_PROGRESS,
            sub_agent_id=active.sub_agent_id,
            parent_sub_agent_id=None,
            phase=active.phase,
            role=active.role,
            context=active.context,
            accepts_input=False,
            extra={
                "kind": "tool",
                "toolResult": {
                    "callId": job.tool_job_id,
                    "failure": None,
                    "ok": result.succeeded,
                    "structured": result.as_dict(),
                    "summary": "Falsification execution completed",
                    "text": "",
                    "tool": _MODEL_TOOL_NAME,
                },
            },
        )
    return model_run


def backtest(
    workflow: Workflow[
        HypothesisDevelopmentRequest,
        HypothesisDevelopmentState,
        object,
    ],
    request: HypothesisDevelopmentRequest,
    state: HypothesisDevelopmentState,
    *,
    bundle: StrategyBundle,
    correlation_id: str,
    label: str,
) -> BacktestRunResult:
    """Publish and run one strategy backtest behind a visible tool job."""
    chat = workflow.chat
    assert chat is not None
    services = services_of(workflow)
    assert services.runner is not None
    experiment_id = services.experiment_id or f"experiment-{request.execution_id}"
    job = chat.submit_tool_job(
        request.workspace_id,
        request.execution_id,
        tool_name=_TOOL_NAME,
        actor_id=_WORKFLOW_ACTOR,
        correlation_id=correlation_id,
        strategy_ids=(bundle.strategy_id,),
    )
    chat.start_tool_job(request.workspace_id, job.tool_job_id, actor_id=_WORKFLOW_ACTOR)
    try:
        result = services.runner.run(
            BacktestRunRequest(
                run_id=job.correlation_id,
                workspace_id=request.workspace_id,
                bundle=bundle,
                workflow_execution_id=request.execution_id,
                experiment_id=experiment_id,
            )
        )
    except Exception as error:
        chat.record_tool_result(
            request.workspace_id,
            job.tool_job_id,
            result=None,
            actor_id=_WORKFLOW_ACTOR,
            success=False,
            error_message=str(error),
            intent_epoch=state.intent_epoch,
        )
        raise
    chat.record_tool_result(
        request.workspace_id,
        job.tool_job_id,
        result=result.as_dict(),
        actor_id=_WORKFLOW_ACTOR,
        success=result.succeeded,
        error_message=None if result.succeeded else f"state {result.state!r} ({label})",
        intent_epoch=state.intent_epoch,
    )
    return result


def stop_for_cycle_budget(
    workflow: Workflow[
        HypothesisDevelopmentRequest,
        HypothesisDevelopmentState,
        object,
    ],
    request: HypothesisDevelopmentRequest,
    state: HypothesisDevelopmentState,
    *,
    purpose: str,
    detail: str,
) -> HypothesisDevelopmentState:
    """Stop a loop that exhausted its auditable attempt budget."""
    active = workflow.active_orchestrators.get(request.execution_id)
    message = workflow._operator_question(
        request,
        state,
        active=active,
        purpose=purpose,
        detail=detail,
    )
    if active is not None:
        workflow._deliver_node_turn(
            request,
            active,
            message,
            question=message,
        )
    raise _PhaseFailure(message, state)
