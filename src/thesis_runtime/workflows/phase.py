"""Host activity bindings and handoff metadata.

Conversation, one-shot and deterministic implementations remain host concerns.
The graph consumes only their named inputs, declared routes and terminal outcome;
seeds and validators name extension operations rather than live callbacks.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal

from thesis_runtime.workflows.graph.contracts import WorkflowSpecError


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
    inputs: Mapping[str, Mapping[str, object]] = field(default_factory=dict)
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


@dataclass(frozen=True, slots=True)
class _PhaseSpec[WorkflowT, RequestT, StateT]:
    """The fields every phase type shares."""

    phase: str
    inputs: tuple[str, ...] = ("input",)
    terminal: bool = False
    validate: str | None = None
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

    seed: str | None = None


@dataclass(frozen=True, slots=True)
class OneShotPhase[WorkflowT, RequestT, StateT](
    _PhaseSpec[WorkflowT, RequestT, StateT]
):
    """An agent-backed phase whose agent runs to a single terminal answer."""

    seed: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class DeterministicPhase[WorkflowT, RequestT, StateT](
    _PhaseSpec[WorkflowT, RequestT, StateT]
):
    """A phase implemented by deterministic code: no agent, no conversation.

    The ``contract`` function is the phase's entire behavior: at phase entry
    the engine calls it for the completion contract and validates that contract
    exactly as an agent's completion. When paired with a split, it is a pure
    join reducer over the fixed branch reports supplied in the handoff.
    """

    contract: str
    replay_safe: bool = False


@dataclass(frozen=True, slots=True)
class TerminalPhase[WorkflowT, RequestT, StateT](
    _PhaseSpec[WorkflowT, RequestT, StateT]
):
    """A terminal outcome with no executor or completion contract."""

    terminal: bool = field(default=True, init=False)
    outcome: Literal["completed", "failed"] = "completed"


#: Host activity bindings; generic graph nodes do not know these execution kinds.
type PhaseSpec[WorkflowT, RequestT, StateT] = (
    ConversationPhase[WorkflowT, RequestT, StateT]
    | OneShotPhase[WorkflowT, RequestT, StateT]
    | DeterministicPhase[WorkflowT, RequestT, StateT]
    | TerminalPhase[WorkflowT, RequestT, StateT]
)
