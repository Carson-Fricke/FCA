"""Generic phase vocabulary and phase specifications.

A phase is a named step of a workflow.  ``PhaseSpec`` is the sealed union of
the three phase types — conversation, one-shot, and deterministic — and the
engine dispatches on that union by exhaustive match.  An agent phase's spec is
data only (its seed builds the agent's prompt); a deterministic phase's spec
carries the contract function that is the phase's whole behavior.  Invalid
combinations are unrepresentable: each type admits exactly the fields its
phase kind supports, so the builder has no cross-field validation to perform.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType


class WorkflowSpecError(ValueError):
    """Raised when workflow specification parts are invalid or incomplete."""


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


#: A deterministic phase's contract function: it receives the accepted handoff
#: and returns the phase's completion contract, or ``None`` to not advance.
type DeterministicExecutor[WorkflowT, RequestT, StateT] = Callable[
    [WorkflowT, RequestT, StateT, HandoffContext], Mapping[str, object] | None
]

#: Builds the prompt text seeded as one durable turn on the phase agent from
#: the accepted handoff: the contract carried by the tool call and the pipes.
type PhaseSeed[WorkflowT, RequestT, StateT] = Callable[
    [WorkflowT, RequestT, StateT, HandoffContext], str
]

#: Validates one completion contract; a non-empty string is the repair reason.
type PhaseValidator = Callable[[Mapping[str, object]], str | None]


@dataclass(frozen=True, slots=True)
class _PhaseSpec[WorkflowT, RequestT, StateT]:
    """The fields every phase type shares."""

    phase: str
    terminal: bool = False
    validate: PhaseValidator | None = None
    on_complete: str | None = None
    required: tuple[str, ...] = ()
    completion_schema: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.phase, str) or not self.phase.strip():
            raise WorkflowSpecError("phase name must be non-empty text")
        object.__setattr__(self, "phase", self.phase.strip())
        object.__setattr__(
            self,
            "required",
            tuple(name for name in self.required if isinstance(name, str) and name),
        )


@dataclass(frozen=True, slots=True)
class ConversationPhase[WorkflowT, RequestT, StateT](
    _PhaseSpec[WorkflowT, RequestT, StateT]
):
    """An agent-backed phase whose agent accepts operator input from birth."""

    seed: PhaseSeed[WorkflowT, RequestT, StateT] | None = None


@dataclass(frozen=True, slots=True)
class OneShotPhase[WorkflowT, RequestT, StateT](
    _PhaseSpec[WorkflowT, RequestT, StateT]
):
    """An agent-backed phase whose agent runs to a single terminal answer."""

    seed: PhaseSeed[WorkflowT, RequestT, StateT] | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class DeterministicPhase[WorkflowT, RequestT, StateT](
    _PhaseSpec[WorkflowT, RequestT, StateT]
):
    """A phase implemented by deterministic code: no agent, no conversation.

    The ``contract`` function is the phase's entire behavior: at phase entry
    the engine calls it for the completion contract, validates that contract
    exactly as an agent's completion, and cascades through consecutive
    deterministic phases.
    """

    contract: DeterministicExecutor[WorkflowT, RequestT, StateT]


#: The sealed union of phase types.  The engine dispatches on this union by
#: exhaustive match; adding a phase type is a compile error at every
#: unmatched site.
type PhaseSpec[WorkflowT, RequestT, StateT] = (
    ConversationPhase[WorkflowT, RequestT, StateT]
    | OneShotPhase[WorkflowT, RequestT, StateT]
    | DeterministicPhase[WorkflowT, RequestT, StateT]
)
