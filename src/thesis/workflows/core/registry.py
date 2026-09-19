"""Resolve a workflow id to its registered workflow.

Dispatch is data, not code branches: the workflow tool asks the registry for
the requested id's registration, and an unknown id is rejected here with one
stable failure shape.  Adding a workflow is a registration of one general
:class:`~thesis.workflows.core.workflow.Workflow` instance (or its factory),
not an edit to the tool or the engine.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from thesis.workflows.core.spec import WorkflowContext, WorkflowSpec
from thesis.workflows.core.state import WorkflowState
from thesis.workflows.core.workflow import (
    Workflow,
    WorkflowEngineError,
    WorkflowRequest,
)


class UnknownWorkflowError(LookupError):
    """Raised when a requested workflow id is not registered."""

    def __init__(self, workflow_id: str) -> None:
        super().__init__(f"unknown workflow: {workflow_id!r}")
        self.workflow_id = workflow_id


class WorkflowExecute(Protocol):
    """The generic launch-and-execute entry point of a registered workflow."""

    def __call__(
        self,
        context: WorkflowContext,
        /,
        *,
        ideas: str,
        investigation_id: str | None = None,
    ) -> Mapping[str, object]:
        """Launch the workflow at ``context``'s anchor and return its handle."""


@dataclass(frozen=True, slots=True)
class WorkflowRegistration[RequestT: WorkflowRequest, StateT: WorkflowState, OutcomeT]:
    """One registered workflow: its general instance factory and entry point."""

    execute: WorkflowExecute
    workflow_factory: Callable[[], Workflow[RequestT, StateT, OutcomeT]] | None = None
    _workflow: Workflow[RequestT, StateT, OutcomeT] | None = field(
        default=None, compare=False, repr=False
    )

    @property
    def workflow(self) -> Workflow[RequestT, StateT, OutcomeT]:
        """Return the general workflow instance this registration runs."""
        cached = self._workflow
        if cached is not None:
            return cached
        factory = self.workflow_factory
        if factory is None:
            raise WorkflowEngineError("registration has no workflow factory")
        workflow = factory()
        object.__setattr__(self, "_workflow", workflow)
        return workflow

    @property
    def spec(self) -> WorkflowSpec[Any, RequestT, StateT]:
        """Return the registered workflow's assembled specification."""
        return self.workflow.spec


class WorkflowRegistry:
    """The workflow id registry: register, resolve, reject unknown."""

    def __init__(self) -> None:
        self._registrations: dict[str, WorkflowRegistration[Any, Any, Any]] = {}

    def register(self, registration: WorkflowRegistration[Any, Any, Any]) -> None:
        """Register one workflow, rejecting a duplicate id."""
        workflow_id = registration.spec.workflow_id
        if workflow_id in self._registrations:
            raise ValueError(f"workflow {workflow_id!r} is already registered")
        self._registrations[workflow_id] = registration

    def resolve(self, workflow_id: str) -> WorkflowRegistration[Any, Any, Any]:
        """Return the registration for ``workflow_id`` or reject it."""
        try:
            return self._registrations[workflow_id]
        except KeyError:
            raise UnknownWorkflowError(workflow_id) from None

    def contains(self, workflow_id: str) -> bool:
        """Return whether ``workflow_id`` is registered."""
        return workflow_id in self._registrations

    def ids(self) -> tuple[str, ...]:
        """Return the registered workflow ids in registration order."""
        return tuple(self._registrations)
