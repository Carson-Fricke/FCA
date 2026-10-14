"""Provider-neutral incremental output; fragments never enter the session inbox."""

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class OutputDelta:
    """An append to one visible channel of an in-flight model invocation."""

    channel: Literal["reasoning", "text"]
    text: str

    def __post_init__(self) -> None:
        if self.channel not in ("reasoning", "text") or not isinstance(self.text, str):
            raise ValueError("output delta requires a known channel and text")
        self.text.encode("utf-8")
