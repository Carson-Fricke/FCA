"""Assemble and validate a workflow specification and its general object.

The builder is the one construction path for a
:class:`~thesis.workflows.core.spec.WorkflowSpec`.  It fails closed: a spec
with a missing piece or an inconsistent table never reaches the registry.
``build`` rejects a spec whose transitions reference undeclared phases, whose
piping references an undeclared transition, or that declares no terminal
phase, then returns the single general
:class:`~thesis.workflows.core.workflow.Workflow` object that runs it.

Phases arrive as sealed-union objects (conversation, one-shot, deterministic),
so an invalid combination cannot be expressed here: the builder performs no
cross-field validation, only structural checks over names and tables.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any, Literal, Self, cast

from thesis.workflows.core.phase import PhaseSpec, WorkflowSpecError
from thesis.workflows.core.spec import (
    PipingFunction,
    StartHandoff,
    WorkflowSpec,
)
from thesis.workflows.core.state import WorkflowState
from thesis.workflows.core.workflow import Workflow, WorkflowRequest

if TYPE_CHECKING:
    from thesis.workflows.core.workflow import WorkflowDoctrine, WorkflowPorts


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
        self._knowledge_scope: Literal["shared", "workspace", "both"] = "both"
        self._phase_knowledge: dict[str, tuple[str, ...]] = {}
        self._phases: list[PhaseSpec[WorkflowT, RequestT, StateT]] = []
        self._transitions: dict[str, set[str]] = {}
        self._piping: dict[tuple[str, str], PipingFunction[StateT]] = {}
        self._start_handoff: StartHandoff[Any, Any] | None = None
        self._doctrine: WorkflowDoctrine[RequestT, StateT, Any] | None = None
        self._services: object | None = None
        self._ports: WorkflowPorts | None = None

    def workflow(
        self,
        workflow_id: str,
        *,
        version: str,
        title: str,
        knowledge_scope: Literal["shared", "workspace", "both"] = "both",
    ) -> Self:
        """Set the workflow identity and version."""
        self._workflow_id = workflow_id
        self._version = version
        self._title = title
        self._knowledge_scope = knowledge_scope
        return self

    def phase(self, spec: PhaseSpec[WorkflowT, RequestT, StateT]) -> Self:
        """Declare one ordered phase as a sealed-union spec object."""
        self._phases.append(spec)
        return self

    def transition(self, source: str, target: str) -> Self:
        """Declare one allowed transition between two phases."""
        self._transitions.setdefault(source, set()).add(target)
        return self

    def transitions(self, table: Mapping[str, Iterable[str]]) -> Self:
        """Declare every transition in one table.

        A source with no reachable phases is still declared, so a terminal
        phase keeps its empty allowed-target set in the assembled spec.
        """
        for source, targets in table.items():
            self._transitions.setdefault(source, set())
            for target in targets:
                self.transition(source, target)
        return self

    def piping(self, source: str, target: str, pipe: PipingFunction[StateT]) -> Self:
        """Declare the typed-state pipe applied across one transition."""
        self._piping[(source, target)] = pipe
        return self

    def on_start(self, handoff: StartHandoff[WorkflowT, RequestT]) -> Self:
        """Declare the workflow-start adapter that threads the caller message.

        The adapter runs when the first phase agent is prepared; its returned
        text overrides the first phase's seed prompt, and ``None`` keeps the
        phase's declared seed.
        """
        self._start_handoff = cast(StartHandoff[Any, Any], handoff)
        return self

    def knowledge(self, phase: str, collections: Iterable[str]) -> Self:
        """Restrict one phase's knowledge retrieval to these collections."""
        existing = dict(self._phase_knowledge)
        merged = tuple(dict.fromkeys((*existing.get(phase, ()), *collections)))
        existing[phase] = merged
        self._phase_knowledge = existing
        return self

    def doctrine(self, doctrine: WorkflowDoctrine[RequestT, StateT, Any]) -> Self:
        """Bind the workflow-specific phase data the engine reads."""
        self._doctrine = doctrine
        return self

    def services(self, services: object) -> Self:
        """Bind the opaque workflow-specific services bundle."""
        self._services = services
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
            transitions={
                phase: frozenset(targets)
                for phase, targets in self._transitions.items()
            },
            piping=dict(self._piping),
            start_handoff=self._start_handoff,
            knowledge_scope=self._knowledge_scope,
            phase_knowledge=self._phase_knowledge,
        )
        return Workflow(
            spec,
            doctrine=self._doctrine,
            services=self._services,
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
                for source, targets in self._transitions.items()
                if source == phase.phase
                for target in targets
            }:
                errors.append(
                    f"phase {phase.phase!r} declares on_complete "
                    f"{phase.on_complete!r}, which is not a declared transition"
                )
        for source, targets in self._transitions.items():
            if source not in declared:
                errors.append(f"transition source {source!r} is not a phase")
            for target in targets:
                if target not in declared:
                    errors.append(f"transition target {target!r} is not a phase")
        for phase in self._phases:
            if phase.phase not in self._transitions:
                errors.append(
                    f"phase {phase.phase!r} has no explicit transition declaration"
                )
            if phase.terminal and self._transitions.get(phase.phase):
                errors.append(
                    f"terminal phase {phase.phase!r} has outgoing transitions"
                )
        if not any(phase.terminal for phase in self._phases):
            errors.append("workflow requires at least one terminal phase")
        for source, target in self._piping:
            if target not in self._transitions.get(source, set()):
                errors.append(
                    f"piping for {source!r} -> {target!r} is not a declared transition"
                )
        return errors
