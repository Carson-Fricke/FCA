"""Read-only workflow definitions for transition-graph clients.

Expose declared topology, not execution state or executable phase callbacks.
"""

from __future__ import annotations

from collections.abc import Mapping

from fastapi import APIRouter, HTTPException

from thesis.http.wire import WorkflowDefinitionWire
from thesis.workflows.core.registry import UnknownWorkflowError, WorkflowRegistry
from thesis.workflows.core.spec import WorkflowContext
from thesis.workflows.registry import build_workflow_registry


def _execution_unavailable(
    context: WorkflowContext,
    *,
    ideas: str,
    investigation_id: str | None = None,
    launch_options: Mapping[str, object] | None = None,
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

    @router.get("/{workflow_id}", response_model=WorkflowDefinitionWire)
    async def get_workflow_definition(
        workflow_id: str, version: str | None = None
    ) -> WorkflowDefinitionWire:
        """Return the current topology, rejecting unsupported requested versions."""
        try:
            spec = workflows.resolve(workflow_id).spec
        except UnknownWorkflowError as error:
            raise HTTPException(
                status_code=404,
                detail={"code": "unknown_workflow", "message": str(error)},
            ) from error
        if version is not None and version != spec.version:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "unsupported_workflow_version",
                    "message": f"unsupported workflow version: {version!r}",
                    "details": {
                        "requested_version": version,
                        "current_version": spec.version,
                    },
                },
            )
        return WorkflowDefinitionWire(
            workflow_id=spec.workflow_id,
            version=spec.version,
            title=spec.title,
            phases=spec.phase_names,
            terminal_phases=spec.terminal_phases,
            transitions={
                phase: sorted(spec.transitions.get(phase, ()))
                for phase in spec.phase_names
            },
        )

    return router
