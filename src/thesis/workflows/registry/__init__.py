"""Register concrete workflows with the engine registry.

This package is the composition bridge between the general engine under
``thesis.workflows.core`` and each concrete workflow's own package.  The
hypothesis-development workflow lives under
``registry/hypothesis_development/``; this module resolves its assembled
:class:`~thesis.workflows.core.workflow.Workflow` object and binds it to the
generic launch/execute entry point.
"""

from __future__ import annotations

from collections.abc import Callable

from thesis.workflows.core.registry import (
    WorkflowExecute,
    WorkflowRegistration,
    WorkflowRegistry,
)
from thesis.workflows.core.workflow import Workflow
from thesis.workflows.registry.hypothesis_development import (
    HYPOTHESIS_DEVELOPMENT_WORKFLOW,
    HypothesisDevelopmentOutcome,
    HypothesisDevelopmentRequest,
    HypothesisDevelopmentState,
    build_hypothesis_development_workflow,
)
from thesis.workflows.registry.hypothesis_development.spec import (
    build_hypothesis_development_spec,
)

__all__ = [
    "HYPOTHESIS_DEVELOPMENT_WORKFLOW_ID",
    "build_hypothesis_development_spec",
    "build_workflow_registry",
    "hypothesis_development_registration",
    "register_hypothesis_development",
]

#: The registered id of the falsification-first hypothesis-development workflow.
HYPOTHESIS_DEVELOPMENT_WORKFLOW_ID = HYPOTHESIS_DEVELOPMENT_WORKFLOW.workflow_id


def hypothesis_development_registration(
    execute: WorkflowExecute,
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
        execute, workflow_factory=workflow_factory
    )
    registry.register(registration)
    return registration


def build_workflow_registry(
    *,
    hypothesis_execute: WorkflowExecute,
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
        workflow_factory=hypothesis_workflow_factory,
    )
    return registry
