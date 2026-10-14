"""Local FCA activity declarations, translated into portable host metadata."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from thesis_runtime.workflows import phase as host


@dataclass(frozen=True, slots=True)
class LocalPhase:
    phase: str
    terminal: bool = False
    validate: Callable[[Mapping[str, object]], str | None] | None = None
    on_complete: str | None = None
    required: tuple[str, ...] = ()
    completion_schema: Mapping[str, object] | None = None

    def host_spec(self) -> host.PhaseSpec[Any, Any, Any]:
        common: dict[str, Any] = dict(
            phase=self.phase,
            terminal=self.terminal,
            validate=None if self.validate is None else "validate",
            on_complete=self.on_complete,
            required=self.required,
            completion_schema=self.completion_schema,
        )
        if isinstance(self, DeterministicPhase):
            return host.DeterministicPhase(**common, contract="execute")
        if isinstance(self, ConversationPhase):
            return host.ConversationPhase(
                **common, seed=None if self.seed is None else "seed"
            )
        if isinstance(self, OneShotPhase):
            return host.OneShotPhase(
                **common, seed=None if self.seed is None else "seed"
            )
        common.pop("terminal")
        return host.TerminalPhase(
            **common, outcome="failed" if self.phase == "failed" else "completed"
        )


@dataclass(frozen=True, slots=True)
class ConversationPhase(LocalPhase):
    seed: Callable[..., str] | None = None


@dataclass(frozen=True, slots=True)
class OneShotPhase(LocalPhase):
    seed: Callable[..., str] | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class DeterministicPhase(LocalPhase):
    contract: Callable[..., Mapping[str, object] | None]


@dataclass(frozen=True, slots=True)
class TerminalPhase(LocalPhase):
    terminal: bool = True


type PhaseSpec[WorkflowT, RequestT, StateT] = LocalPhase


type PipingFunction[StateT] = Callable[[StateT], StateT]
