"""Workflow-owned tool jobs.

The research-model run publishes a visible tool job on the chat transcript and
records its typed result.  This is a domain operation over the general
workflow object, not an engine member.

A deterministic phase is not a sub-agent (REQ-C2): the falsification run's
progress is recorded as execution-scoped ``workflow.progress`` events carrying
the phase name — never as orchestrator-attributed ``sub_agent.*`` events.
"""

from __future__ import annotations

from collections.abc import Mapping

from thesis.chat.contracts import HARNESS_RESEARCH_MODEL_TOOL, ToolJobState
from thesis.research.models import ResearchModel
from thesis.workflows.core.workflow import Workflow
from thesis.workflows.research_models import (
    ResearchModelRunRequest,
    ResearchModelRunResult,
)

from .contracts.creation import HypothesisDevelopmentRequest
from .primitives import (
    _WORKFLOW_ACTOR,
)
from .services import services_of
from .state import HypothesisDevelopmentState

__all__ = [
    "research_model_attribution",
    "run_research_model",
]


def research_model_attribution(model: ResearchModel) -> Mapping[str, object]:
    """Return the immutable revision attribution for one model execution."""
    return {"modelSha256": model.content_sha256}


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
    job_id: str | None = None,
    label: str,
) -> ResearchModelRunResult:
    """Publish and run one workspace model behind one visible job id.

    The job id is allocated here when the caller has no persisted value, then
    passed unchanged to the tool job and the harness run so one execution has
    exactly one identity.  Registration carries the model revision attribution
    so a resumed submission joins the same tracked job instead of overwriting
    it or allocating a replacement.
    """
    chat = workflow.chat
    assert chat is not None
    services = services_of(workflow)
    assert services.research_runner is not None
    experiment_id = services.experiment_id or f"experiment-{request.execution_id}"
    resolved_job_id = workflow.id_factory("job") if job_id is None else job_id
    job = chat.submit_tool_job(
        request.workspace_id,
        request.execution_id,
        tool_name=HARNESS_RESEARCH_MODEL_TOOL,
        actor_id=_WORKFLOW_ACTOR,
        job_id=resolved_job_id,
        attribution=research_model_attribution(model),
    )
    if job.state is ToolJobState.SUBMITTED:
        chat.start_tool_job(request.workspace_id, job.job_id, actor_id=_WORKFLOW_ACTOR)
    workflow.emit_phase_progress(
        request,
        state.phase,
        kind="tool",
        text=f"Running the falsification model ({label}).",
    )
    try:
        model_run = services.research_runner.run(
            ResearchModelRunRequest(
                job_id=resolved_job_id,
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
            job.job_id,
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
        job.job_id,
        result={
            **result.as_dict(),
            "researchModelReference": model_run.model_reference.key,
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
