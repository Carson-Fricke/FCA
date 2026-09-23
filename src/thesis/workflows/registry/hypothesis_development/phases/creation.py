"""The hypothesis-creation phase's durable trigger text."""

from __future__ import annotations

from typing import Any

from thesis.chat.contracts import RecordNotFound
from thesis.workflows.core.workflow import Workflow

from ..contracts.creation import HypothesisDevelopmentRequest


def trigger_text(
    workflow: Workflow[Any, Any, Any],
    request: HypothesisDevelopmentRequest,
) -> str:
    """Return the operator's launching message, verbatim.

    The workflow seeds its first phase from the durable trigger message the
    launch is bound to.  ``request.ideas`` remains available as the derived
    routing value the launch tool supplied; it is never a paraphrase of the
    operator's own words.
    """
    chat = workflow.chat
    if chat is None:
        return request.ideas
    try:
        message = chat.get_message(request.workspace_id, request.trigger_message_id)
    except RecordNotFound:
        return request.ideas
    return message.body if message.body.strip() else request.ideas
