"""Composer for the hypothesis-creation conversational turn."""

from __future__ import annotations

from thesis.agent.router import InvocationResult

from ..errors import (
    CreationTurn,
    FalsificationComposeError,
)
from ._shared import _compose_text, _json_document


class JsonCreationTurnComposer:
    """Parse the model's conversational message and advancement judgment."""

    def compose(self, invocation: InvocationResult) -> CreationTurn:
        document = _json_document(invocation, "creation turn")
        message = _compose_text(document, "message", "message")
        advance = document.get("advance")
        if not isinstance(advance, bool):
            raise FalsificationComposeError("creation turn advance must be boolean")
        return CreationTurn(message=message, advance=advance)
