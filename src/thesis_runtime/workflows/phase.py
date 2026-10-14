"""Generic phase vocabulary and phase specifications.

A phase is a named structural step: conversation, one-shot, deterministic,
terminal or fixed split. Seeds, validation and deterministic contracts are extension handler
names rather than native callbacks. Terminal phases declare their host outcome.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal


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


@dataclass(frozen=True, slots=True)
class _PhaseSpec[WorkflowT, RequestT, StateT]:
    """The fields every phase type shares."""

    phase: str
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


@dataclass(frozen=True, slots=True)
class TerminalPhase[WorkflowT, RequestT, StateT](
    _PhaseSpec[WorkflowT, RequestT, StateT]
):
    """A terminal outcome with no executor or completion contract."""

    terminal: bool = field(default=True, init=False)
    outcome: Literal["completed", "failed"] = "completed"


@dataclass(frozen=True, slots=True)
class SplitPhase[WorkflowT, RequestT, StateT](_PhaseSpec[WorkflowT, RequestT, StateT]):
    """Fixed named branches with one matching deterministic join; no executor."""

    branches: Mapping[str, str] = field(default_factory=dict)
    join: str = ""

    def __post_init__(self) -> None:
        _PhaseSpec.__post_init__(self)
        object.__setattr__(self, "branches", MappingProxyType(dict(self.branches)))
        if len(self.branches) < 2 or any(
            not isinstance(name, str)
            or not name.strip()
            or not isinstance(target, str)
            or not target.strip()
            for name, target in self.branches.items()
        ):
            raise WorkflowSpecError("a split requires at least two named branches")
        if not isinstance(self.join, str) or not self.join.strip():
            raise WorkflowSpecError("a split requires its matching join name")
        if (
            self.terminal
            or self.validate
            or self.on_complete
            or self.required
            or self.completion_schema
        ):
            raise WorkflowSpecError("a structural split cannot declare executor policy")


#: The supported node vocabulary; structural splits never create executors.
type PhaseSpec[WorkflowT, RequestT, StateT] = (
    ConversationPhase[WorkflowT, RequestT, StateT]
    | OneShotPhase[WorkflowT, RequestT, StateT]
    | DeterministicPhase[WorkflowT, RequestT, StateT]
    | TerminalPhase[WorkflowT, RequestT, StateT]
    | SplitPhase[WorkflowT, RequestT, StateT]
)
