"""Workflow specification and execution-context contracts.

A workflow is data: an id, a version, a title, an ordered set of phases, the
allowed transitions between them, and extension handler metadata.  The engine
assembles and validates that data through
:class:`~thesis_runtime.workflows.builder.WorkflowBuilder` and resolves it by id
through :class:`~thesis_runtime.workflows.registry.WorkflowRegistry`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING

from thesis_runtime.workflows.phase import (
    HandoffContext,
    PhaseSpec,
    WorkflowSpecError,
)

if TYPE_CHECKING:
    from thesis_runtime.conversations.delivery import DeliveryService

__all__ = [
    "HandoffContext",
    "PhaseSpec",
    "WorkflowContext",
    "WorkflowSpec",
    "WorkflowSpecError",
]


@dataclass(frozen=True, slots=True)
class WorkflowContext:
    """The trusted per-execution context the engine plumbing operates on.

    Workspace, chat, execution, anchor, and actor identity are supplied by
    the runtime, never by a model.  The delivery port stays optional so the
    context can also describe a launch before the execution exists.
    """

    workspace_id: str
    chat_id: str
    actor_id: str
    execution_id: str | None = None
    trigger_message_id: str | None = None
    intent_epoch: int = 0
    anchor_sub_agent_id: str | None = None
    delivery: DeliveryService | None = None


@dataclass(frozen=True, slots=True)
class WorkflowSpec[WorkflowT, RequestT, StateT]:
    """The assembled, validated definition of one workflow."""

    workflow_id: str
    version: str
    title: str
    phases: tuple[PhaseSpec[WorkflowT, RequestT, StateT], ...]
    transitions: Mapping[str, frozenset[str]]

    def __post_init__(self) -> None:
        if not isinstance(self.workflow_id, str) or not self.workflow_id.strip():
            raise WorkflowSpecError("workflow_id must be non-empty text")
        object.__setattr__(self, "workflow_id", self.workflow_id.strip())
        object.__setattr__(
            self,
            "transitions",
            MappingProxyType(
                {
                    str(phase): frozenset(str(target) for target in targets)
                    for phase, targets in self.transitions.items()
                }
            ),
        )
        from thesis_runtime.workflows.graph.topology import validate_topology

        if self.phases:
            validate_topology(self.phase_specs, self.transitions, self.phases[0].phase)

    @property
    def phase_specs(self) -> Mapping[str, PhaseSpec[WorkflowT, RequestT, StateT]]:
        """Return the declared spec for every phase, by name."""
        return MappingProxyType({phase.phase: phase for phase in self.phases})

    @property
    def phase_names(self) -> tuple[str, ...]:
        """Return the declared phase names in order."""
        return tuple(phase.phase for phase in self.phases)

    @property
    def terminal_phases(self) -> tuple[str, ...]:
        """Return the declared terminal phase names."""
        return tuple(phase.phase for phase in self.phases if phase.terminal)

    @property
    def on_complete(self) -> Mapping[str, str]:
        """Return the declared deterministic target for single-target phases."""
        return MappingProxyType(
            {
                phase.phase: phase.on_complete
                for phase in self.phases
                if phase.on_complete is not None
            }
        )

    @property
    def required(self) -> Mapping[str, tuple[str, ...]]:
        """Return the required completion-contract fields for every phase."""
        return MappingProxyType(
            {phase.phase: phase.required for phase in self.phases if phase.required}
        )

    @property
    def completion_schemas(self) -> Mapping[str, Mapping[str, object]]:
        """Return the advertised completion JSON Schema for every phase.

        A phase that declares a schema gets it merged into the
        ``request_phase_completion`` tool its agent sees, so the provider
        constrains the contract at decode time; a phase without one keeps the
        generic loose object schema.
        """
        return MappingProxyType(
            {
                phase.phase: phase.completion_schema
                for phase in self.phases
                if phase.completion_schema is not None
            }
        )
