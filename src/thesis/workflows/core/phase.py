"""Generic phase vocabulary and phase specifications.

A phase is a named step of a workflow with an executor.  The executor is the
current implementation boundary: an agent invocation or deterministic code
that advances the workflow's typed state.  ``PhaseSpec`` is workflow-agnostic;
the concrete phase names, transitions, and executor bodies belong to the
registered workflow's own package.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType


class WorkflowSpecError(ValueError):
    """Raised when workflow specification parts are invalid or incomplete."""


class PhaseKind(StrEnum):
    """How a phase advances: a model-backed agent or deterministic code."""

    AGENT = "agent"
    DETERMINISTIC = "deterministic"


@dataclass(frozen=True, slots=True)
class HandoffContext:
    """The deterministic handoff facts passed to a seed or adapter.

    ``source`` is ``None`` at workflow start; ``completion`` is the accepted
    contract for a transition and empty at start.  Everything here is supplied
    by the runtime and the accepted tool call, never read from another agent's
    transcript.
    """

    source: str | None
    target: str
    completion: Mapping[str, object] = field(
        default_factory=lambda: MappingProxyType({})
    )
    trigger_message_id: str | None = None
    sub_agent_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "completion",
            MappingProxyType(
                {str(key): value for key, value in self.completion.items()}
            ),
        )


type PhaseExecutor[WorkflowT, RequestT, StateT] = Callable[
    [WorkflowT, RequestT, StateT], StateT
]

#: Builds the prompt text seeded as one durable turn on the phase agent from
#: the accepted handoff: the contract carried by the tool call and the pipes.
type PhaseSeed[WorkflowT, RequestT, StateT] = Callable[
    [WorkflowT, RequestT, StateT, HandoffContext], str
]

#: Validates one completion contract; a non-empty string is the repair reason.
type PhaseValidator = Callable[[Mapping[str, object]], str | None]


@dataclass(frozen=True, slots=True)
class PhaseSpec[WorkflowT, RequestT, StateT]:
    """One named phase and how its agent is prepared and completes.

    A phase agent owns its turns: it is spawned with a prompt built by ``seed``,
    and it advances the workflow only by calling the completion tool, whose
    contract is checked by ``validate`` and by the declared required fields.
    ``on_complete`` declares the phase's single deterministic target; a phase
    with several allowed targets lets the agent name one in the completion.
    """

    phase: str
    executor: PhaseExecutor[WorkflowT, RequestT, StateT] | None = None
    kind: PhaseKind = PhaseKind.AGENT
    terminal: bool = False
    seed: PhaseSeed[WorkflowT, RequestT, StateT] | None = None
    validate: PhaseValidator | None = None
    on_complete: str | None = None
    required: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.phase, str) or not self.phase.strip():
            raise WorkflowSpecError("phase name must be non-empty text")
        object.__setattr__(self, "phase", self.phase.strip())
        object.__setattr__(
            self,
            "required",
            tuple(name for name in self.required if isinstance(name, str) and name),
        )
