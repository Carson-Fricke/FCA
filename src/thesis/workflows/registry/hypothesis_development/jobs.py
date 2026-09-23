"""Workflow-owned tool jobs.

The research-model run publishes a visible tool job on the chat transcript and
records its typed result.  This is a domain operation over the general
workflow object, not an engine member.

A deterministic phase is not a sub-agent (REQ-C2): the falsification run's
progress is recorded as execution-scoped ``workflow.progress`` events carrying
the phase name — never as orchestrator-attributed ``sub_agent.*`` events.
"""

from __future__ import annotations

from thesis.research.models import ResearchModel
from thesis.workflows.core.workflow import Workflow
from thesis.workflows.research_models import (
    ResearchModelRunRequest,
    ResearchModelRunResult,
)

from .contracts.creation import HypothesisDevelopmentRequest
from .primitives import (
    _MODEL_TOOL_NAME,
    _WORKFLOW_ACTOR,
)
from .services import services_of
from .state import HypothesisDevelopmentState

__all__ = [
    "run_research_model",
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
    workflow.emit_phase_progress(
        request,
        state.phase,
        kind="tool",
        text=f"Running the falsification model ({label}).",
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
        workflow.emit_phase_progress(
            request,
            state.phase,
            kind="tool",
            text=f"Falsification execution failed: {error}",
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
    workflow.emit_phase_progress(
        request,
        state.phase,
        kind="tool",
        text=f"Falsification execution completed in state {result.state}.",
    )
    return model_run
