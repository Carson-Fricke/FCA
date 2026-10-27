"""Assemble and validate a workflow specification and its general object.

The builder is the one construction path for a
:class:`~thesis_runtime.workflows.spec.WorkflowSpec`.  It fails closed: a spec
with a missing piece or an inconsistent table never reaches the registry.
``build`` rejects a spec whose routes reference undeclared ports or that
declares no terminal phase, then returns the single general
:class:`~thesis_runtime.workflows.workflow.Workflow` object that runs it.

Phases arrive as sealed-union objects (conversation, one-shot, deterministic),
so an invalid combination cannot be expressed here: the builder performs no
cross-field validation, only structural checks over names and tables.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any, Self

from thesis_runtime.workflows.graph.contracts import Destination
from thesis_runtime.workflows.phase import PhaseSpec, WorkflowSpecError
from thesis_runtime.workflows.spec import (
    WorkflowSpec,
)
from thesis_runtime.workflows.state import WorkflowState
from thesis_runtime.workflows.workflow import Workflow, WorkflowRequest

if TYPE_CHECKING:
    from thesis_runtime.workflows.workflow import WorkflowDoctrine, WorkflowPorts


class WorkflowBuilder[
    WorkflowT,
    RequestT: WorkflowRequest,
    StateT: WorkflowState,
]:
    """Collect workflow parts and validate them into one spec."""

    def __init__(self) -> None:
        self._workflow_id = ""
        self._version = ""
        self._title = ""
        self._phases: list[PhaseSpec[WorkflowT, RequestT, StateT]] = []
        self._routes: dict[str, dict[str, tuple[Destination, ...]]] = {}
        self._doctrine: WorkflowDoctrine[RequestT, StateT, Any] | None = None
        self._ports: WorkflowPorts | None = None

    def workflow(
        self,
        workflow_id: str,
        *,
        version: str,
        title: str,
    ) -> Self:
        """Set the workflow identity and version."""
        self._workflow_id = workflow_id
        self._version = version
        self._title = title
        return self

    def phase(self, spec: PhaseSpec[WorkflowT, RequestT, StateT]) -> Self:
        """Declare one ordered phase as a sealed-union spec object."""
        self._phases.append(spec)
        self._routes.setdefault(spec.phase, {})
        return self

    def route(self, source: str, name: str, *destinations: str) -> Self:
        """Wire one named route to explicit node.input destinations."""
        routes = self._routes.setdefault(source, {})
        if name in routes:
            raise WorkflowSpecError(f"route {source}.{name} is declared twice")
        routes[name] = tuple(Destination.parse(value) for value in destinations)
        return self

    def routes(self, table: Mapping[str, Mapping[str, Iterable[str]]]) -> Self:
        """Declare routes; the table is the sole authority for graph connections."""
        for source, routes in table.items():
            self._routes.setdefault(source, {})
            for name, destinations in routes.items():
                self.route(source, name, *destinations)
        return self

    def doctrine(self, doctrine: WorkflowDoctrine[RequestT, StateT, Any]) -> Self:
        """Bind the workflow-specific phase data the engine reads."""
        self._doctrine = doctrine
        return self

    def ports(self, ports: WorkflowPorts) -> Self:
        """Bind the engine ports that turn the workflow into a live object."""
        self._ports = ports
        return self

    def build(self) -> Workflow[RequestT, StateT, Any]:
        """Validate every part, assemble the spec, and return the workflow."""
        errors = self._validate()
        if errors:
            raise WorkflowSpecError("; ".join(errors))
        spec = WorkflowSpec(
            workflow_id=self._workflow_id,
            version=self._version,
            title=self._title,
            phases=tuple(self._phases),
            routes=self._routes,
        )
        return Workflow(
            spec,
            doctrine=self._doctrine,
            ports=self._ports,
        )

    def _validate(self) -> list[str]:
        errors: list[str] = []
        if not self._workflow_id.strip():
            errors.append("workflow requires an id")
        if not self._version.strip():
            errors.append("workflow requires a version")
        if not self._title.strip():
            errors.append("workflow requires a title")
        if not self._phases:
            errors.append("workflow requires at least one phase")
        declared: set[str] = set()
        for phase in self._phases:
            if phase.phase in declared:
                errors.append(f"phase {phase.phase!r} is declared twice")
            declared.add(phase.phase)
            if phase.on_complete is not None and phase.on_complete not in {
                target
                for source, targets in self._routes.items()
                if source == phase.phase
                for target in targets
            }:
                errors.append(
                    f"phase {phase.phase!r} declares on_complete "
                    f"{phase.on_complete!r}, which is not a declared route"
                )
        for phase in self._phases:
            if phase.terminal and self._routes.get(phase.phase):
                errors.append(f"terminal phase {phase.phase!r} has outgoing routes")
        for source in self._routes:
            if source not in declared:
                errors.append(f"route source {source!r} is not a node")
        if not any(phase.terminal for phase in self._phases):
            errors.append("workflow requires at least one terminal phase")
        return errors
