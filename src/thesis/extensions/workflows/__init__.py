"""Register concrete workflows with the engine registry.

This package is the composition bridge between the general engine under
``thesis_runtime.workflows`` and each concrete workflow's own package.  The
hypothesis-development workflow lives under
``registry/hypothesis_development/``; this module resolves its assembled
:class:`~thesis_runtime.workflows.workflow.Workflow` object and binds it to the
generic launch/execute entry point.
"""

from __future__ import annotations

from collections.abc import Callable

from thesis.extensions.workflows.hypothesis_development import (
    HYPOTHESIS_DEVELOPMENT_WORKFLOW,
    HypothesisDevelopmentOutcome,
    HypothesisDevelopmentRequest,
    HypothesisDevelopmentState,
    build_hypothesis_development_workflow,
)
from thesis.extensions.workflows.hypothesis_development.launch_schema import (
    hypothesis_launch_schema,
)
from thesis.extensions.workflows.hypothesis_development.spec import (
    build_hypothesis_development_spec,
)
from thesis.extensions.workflows.research_investigation import (
    build_research_investigation_workflow,
)
from thesis.extensions.workflows.research_investigation.contracts import LAUNCH_SCHEMA
from thesis_runtime.workflows.registry import (
    WorkflowExecute,
    WorkflowRegistration,
    WorkflowRegistry,
)
from thesis_runtime.workflows.workflow import Workflow

__all__ = [
    "HYPOTHESIS_DEVELOPMENT_WORKFLOW_ID",
    "build_hypothesis_development_spec",
    "build_workflow_registry",
    "build_research_investigation_workflow",
    "hypothesis_development_registration",
    "register_hypothesis_development",
]

#: The registered id of the falsification-first hypothesis-development workflow.
HYPOTHESIS_DEVELOPMENT_WORKFLOW_ID = HYPOTHESIS_DEVELOPMENT_WORKFLOW.workflow_id


def hypothesis_development_registration(
    execute: WorkflowExecute,
    resume=None,
    *,
    workflow_factory: (
        Callable[
            [],
            Workflow[
                HypothesisDevelopmentRequest,
                HypothesisDevelopmentState,
                HypothesisDevelopmentOutcome,
            ],
        ]
        | None
    ) = None,
) -> WorkflowRegistration[
    HypothesisDevelopmentRequest,
    HypothesisDevelopmentState,
    HypothesisDevelopmentOutcome,
]:
    """Bind the general workflow object to its launch/execute entry point."""
    return WorkflowRegistration(
        execute=execute,
        resume=resume,
        launch_schema=hypothesis_launch_schema(),
        workflow_factory=(
            build_hypothesis_development_workflow
            if workflow_factory is None
            else workflow_factory
        ),
    )


def register_hypothesis_development(
    registry: WorkflowRegistry,
    *,
    execute: WorkflowExecute,
    resume=None,
    workflow_factory: (
        Callable[
            [],
            Workflow[
                HypothesisDevelopmentRequest,
                HypothesisDevelopmentState,
                HypothesisDevelopmentOutcome,
            ],
        ]
        | None
    ) = None,
) -> WorkflowRegistration[
    HypothesisDevelopmentRequest,
    HypothesisDevelopmentState,
    HypothesisDevelopmentOutcome,
]:
    """Register the hypothesis workflow into ``registry`` and return it."""
    registration = hypothesis_development_registration(
        execute, resume=resume, workflow_factory=workflow_factory
    )
    registry.register(registration)
    return registration


def build_workflow_registry(
    *,
    hypothesis_execute: WorkflowExecute,
    hypothesis_resume=None,
    research_registration: WorkflowRegistration | None = None,
    hypothesis_workflow_factory: (
        Callable[
            [],
            Workflow[
                HypothesisDevelopmentRequest,
                HypothesisDevelopmentState,
                HypothesisDevelopmentOutcome,
            ],
        ]
        | None
    ) = None,
) -> WorkflowRegistry:
    """Build the production registry with every concrete workflow registered."""
    registry = WorkflowRegistry()
    register_hypothesis_development(
        registry,
        execute=hypothesis_execute,
        resume=hypothesis_resume,
        workflow_factory=hypothesis_workflow_factory,
    )
    registry.register(
        research_registration
        if research_registration is not None
        else WorkflowRegistration(
            execute=_research_unavailable,
            launch_schema=LAUNCH_SCHEMA,
            workflow_factory=build_research_investigation_workflow,
        )
    )
    return registry


def _research_unavailable(context, inputs):
    raise ValueError("research investigation execution services are unavailable")
