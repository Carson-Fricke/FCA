"""Read-only workflow definitions for transition-graph clients.

Expose declared topology, not execution state or executable phase callbacks.
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from thesis.workflows.core.registry import UnknownWorkflowError, WorkflowRegistry
from thesis.workflows.core.spec import WorkflowContext
from thesis.workflows.registry import build_workflow_registry


def _execution_unavailable(
    context: WorkflowContext,
    *,
    ideas: str,
    investigation_id: str | None = None,
) -> dict[str, object]:
    raise RuntimeError("the definition-only registry cannot execute workflows")


def create_workflow_router(registry: WorkflowRegistry | None = None) -> APIRouter:
    """Expose the serving registry, or built-in definitions in chat-only mode."""
    workflows = (
        registry
        if registry is not None
        else build_workflow_registry(hypothesis_execute=_execution_unavailable)
    )
    router = APIRouter(prefix="/v1/workflows", tags=["workflows"])

    @router.get("/{workflow_id}")
    async def get_workflow_definition(workflow_id: str) -> JSONResponse:
        """Return ordered phases and all legal edges, including terminal nodes."""
        try:
            spec = workflows.resolve(workflow_id).spec
        except UnknownWorkflowError as error:
            return JSONResponse(
                status_code=404,
                content={"error": {"code": "unknown_workflow", "message": str(error)}},
            )
        return JSONResponse(
            content={
                "workflow_id": spec.workflow_id,
                "version": spec.version,
                "title": spec.title,
                "phases": list(spec.phase_names),
                "terminal_phases": list(spec.terminal_phases),
                "transitions": {
                    phase: sorted(spec.transitions.get(phase, ()))
                    for phase in spec.phase_names
                },
            }
        )

    return router
