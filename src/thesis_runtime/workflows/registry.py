"""Resolve a workflow id to its registered workflow.

Dispatch is data, not code branches: the workflow tool asks the registry for
the requested id's registration, and an unknown id is rejected here with one
stable failure shape.  Adding a workflow is a registration of one general
:class:`~thesis_runtime.workflows.workflow.Workflow` instance (or its factory),
not an edit to the tool or the engine.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from thesis_runtime.tools.schema import (
    check_schema,
    render_violations,
    validate_arguments,
)
from thesis_runtime.workflows.execution_store import ExecutionIdentity
from thesis_runtime.workflows.spec import WorkflowContext, WorkflowSpec
from thesis_runtime.workflows.state import WorkflowState
from thesis_runtime.workflows.workflow import (
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
        inputs: Mapping[str, object],
    ) -> Mapping[str, object]:
        """Launch the workflow at ``context``'s anchor and return its handle."""


@dataclass(frozen=True, slots=True)
class WorkflowRegistration[RequestT: WorkflowRequest, StateT: WorkflowState, OutcomeT]:
    """One registered workflow: its general instance factory and entry point."""

    execute: WorkflowExecute
    launch_schema: Mapping[str, object] = field(
        default_factory=lambda: {"type": "object"}
    )
    resume: (
        Callable[
            [ExecutionIdentity, Mapping[str, object], Mapping[str, object] | None], None
        ]
        | None
    ) = None
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
        key = registration.spec.workflow_id
        if key in self._registrations:
            raise ValueError(f"workflow {key!r} is already registered")
        check_schema(registration.launch_schema)
        self._registrations[key] = registration

    def resolve(
        self, workflow_id: str, version: str | None = None
    ) -> WorkflowRegistration[Any, Any, Any]:
        try:
            registration = self._registrations[workflow_id]
        except KeyError:
            raise UnknownWorkflowError(
                workflow_id if version is None else f"{workflow_id}@{version}"
            ) from None
        if version is not None and version != registration.spec.version:
            raise UnknownWorkflowError(f"outdated workflow {workflow_id}@{version}")
        return registration

    def launch(
        self, workflow_id: str, context: WorkflowContext, inputs: Mapping[str, object]
    ) -> Mapping[str, object]:
        registration = self.resolve(workflow_id)
        errors = validate_arguments(registration.launch_schema, inputs)
        if errors:
            raise ValueError(render_violations(errors))
        result = dict(registration.execute(context, inputs))
        result.update(
            workflowId=registration.spec.workflow_id, version=registration.spec.version
        )
        return result

    def resume(
        self,
        identity: ExecutionIdentity,
        launch_request: Mapping[str, object],
        work: Mapping[str, object] | None = None,
    ) -> None:
        registration = self.resolve(identity.workflow_id, identity.workflow_version)
        if registration.resume is None:
            raise WorkflowEngineError(
                f"workflow {identity.workflow_id}@{identity.workflow_version} has no recovery binding"
            )
        registration.resume(identity, launch_request, work)

    def contains(self, workflow_id: str) -> bool:
        """Return whether ``workflow_id`` is registered."""
        return workflow_id in self._registrations

    def ids(self) -> tuple[str, ...]:
        """Return the registered workflow ids in registration order."""
        return tuple(self._registrations)
