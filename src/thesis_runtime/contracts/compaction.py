"""Provider-neutral context budgets and optional, audited evidence selection ports.

These contracts carry capacity and exact canonical references, not provider roles,
conversation effects or task-specific preservation policy.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from thesis_runtime.contracts.messages import SessionMessage

EvidenceAudit = Callable[
    [
        Sequence[SessionMessage],
        Mapping[str, object],
        Callable[[], Mapping[str, object]],
    ],
    Mapping[str, object],
]


class EvidenceRanker(Protocol):
    """Rank exact message groups for optional retention, never summarization."""

    def rank(
        self,
        groups: Sequence[tuple[SessionMessage, ...]],
        *,
        focus: str,
        audit: EvidenceAudit,
    ) -> Sequence[int]: ...


@dataclass(frozen=True, slots=True)
class ContextPolicy:
    """Explicit model capacity and bounded checkpoint budgets, in tokens."""

    window_tokens: int = 128_000
    output_tokens: int = 32_768
    summary_tokens: int = 8_192
    recent_tokens: int = 15_000
    evidence_tokens: int = 4_000
    reserve_tokens: int = 16_000

    def __post_init__(self) -> None:
        for name in (
            "window_tokens",
            "output_tokens",
            "summary_tokens",
            "recent_tokens",
            "evidence_tokens",
            "reserve_tokens",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        if min(self.window_tokens, self.output_tokens, self.summary_tokens) < 1:
            raise ValueError("context, output and summary budgets must be positive")
        if (
            self.input_limit
            <= self.summary_tokens + self.evidence_tokens + self.recent_tokens
        ):
            raise ValueError(
                "context must leave room for summary, evidence and recent history"
            )

    @property
    def input_limit(self) -> int:
        return self.window_tokens - self.output_tokens - self.reserve_tokens
