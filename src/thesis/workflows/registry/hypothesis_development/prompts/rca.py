"""Root-cause prompt protocol."""

from __future__ import annotations

from typing import Protocol

from thesis.agent.router import InvocationResult

from ..contracts import (
    RcaDiagnosis,
)


class RcaComposer(Protocol):
    """Injected boundary that turns model output into an RCA diagnosis."""

    def compose(self, invocation: InvocationResult) -> RcaDiagnosis: ...
