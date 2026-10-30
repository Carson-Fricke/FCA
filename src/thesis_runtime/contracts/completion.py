"""Immutable request options shared by model callers and provider adapters.

Callers choose output limits, tool eligibility and whether input is a transcript
to inspect. Adapters own protocol encoding; these values carry no delivery or
execution authority.
"""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class CompletionOptions:
    max_output_tokens: int
    allow_tool_calls: bool = True
    input_mode: Literal["conversation", "transcript"] = "conversation"

    def __post_init__(self) -> None:
        if (
            isinstance(self.max_output_tokens, bool)
            or not isinstance(self.max_output_tokens, int)
            or self.max_output_tokens < 1
        ):
            raise ValueError("max_output_tokens must be positive")
        if not isinstance(self.allow_tool_calls, bool):
            raise ValueError("allow_tool_calls must be boolean")
        if self.input_mode not in {"conversation", "transcript"}:
            raise ValueError("unknown completion input mode")
