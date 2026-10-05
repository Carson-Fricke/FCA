"""Workflow specification and execution-context contracts.

A workflow is data: an id, a version, a title, an ordered set of phases, the
allowed transitions between them, and the piping between phases.  The engine
assembles and validates that data through
:class:`~thesis.workflows.core.builder.WorkflowBuilder` and resolves it by id
through :class:`~thesis.workflows.core.registry.WorkflowRegistry`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal

from thesis.workflows.core.phase import (
    HandoffContext,
    PhaseSpec,
    PhaseValidator,
    WorkflowSpecError,
)

if TYPE_CHECKING:
    from thesis.agent.runtime.delivery import DeliveryService

__all__ = [
    "HandoffContext",
    "PipingFunction",
    "PhaseSpec",
    "StartHandoff",
    "TransitionRequest",
    "WorkflowContext",
    "WorkflowSpec",
    "WorkflowSpecError",
]


#: A piping function moves typed state across one declared transition.
type PipingFunction[StateT] = Callable[[StateT], StateT]


#: The workflow-start adapter: thread the caller message into the first phase.
#: Returning text overrides the first phase's seed prompt; ``None`` keeps the
#: phase's declared seed.
type StartHandoff[WorkflowT, RequestT] = Callable[
    [WorkflowT, RequestT, HandoffContext], str | None
]


@dataclass(frozen=True, slots=True)
class TransitionRequest[PhaseT]:
    """One executor's request to move the workflow to another phase.

    The request carries the target phase, the phase contract payload the
    executor produced, and the intent epoch it derived the request from.  The
    engine validates the target against the transition table and stamps the
    epoch onto the recorded transition.
    """

    target_phase: PhaseT
    completion: Mapping[str, object] = field(
        default_factory=lambda: MappingProxyType({})
    )
    intent_epoch: int = 0

    def __post_init__(self) -> None:
        if self.intent_epoch < 0:
            raise WorkflowSpecError("intent_epoch must not be negative")
        object.__setattr__(
            self,
            "completion",
            MappingProxyType(
                {str(key): value for key, value in self.completion.items()}
            ),
        )


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
    piping: Mapping[tuple[str, str], PipingFunction[StateT]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    start_handoff: StartHandoff[Any, Any] | None = None
    knowledge_scope: Literal["shared", "workspace", "both"] = "both"
    #: Per-phase knowledge collection allowlist; a phase absent here is unrestricted.
    phase_knowledge: Mapping[str, tuple[str, ...]] = field(
        default_factory=lambda: MappingProxyType({})
    )

    def __post_init__(self) -> None:
        if self.knowledge_scope not in ("shared", "workspace", "both"):
            raise WorkflowSpecError(
                "knowledge_scope must be shared, workspace, or both"
            )
        collections: dict[str, tuple[str, ...]] = {}
        for phase, allowed in self.phase_knowledge.items():
            if not isinstance(phase, str) or not phase.strip():
                raise WorkflowSpecError(
                    "phase_knowledge keys must be non-empty phase names"
                )
            checked = tuple(
                dict.fromkeys(
                    entry
                    for entry in allowed
                    if isinstance(entry, str) and entry.strip()
                )
            )
            if len(checked) != len(allowed):
                raise WorkflowSpecError(
                    "phase_knowledge collections must be non-empty strings"
                )
            collections[phase] = checked
        object.__setattr__(self, "phase_knowledge", MappingProxyType(collections))
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
        object.__setattr__(
            self,
            "piping",
            MappingProxyType(
                {
                    (str(source), str(target)): pipe
                    for (source, target), pipe in self.piping.items()
                }
            ),
        )

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
    def validators(self) -> Mapping[str, PhaseValidator]:
        """Return the contract validator for every phase that declares one."""
        return MappingProxyType(
            {
                phase.phase: phase.validate
                for phase in self.phases
                if phase.validate is not None
            }
        )

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
