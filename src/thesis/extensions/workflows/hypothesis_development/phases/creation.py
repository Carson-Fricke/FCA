"""The workflow-start durable trigger text.

The workflow still starts by handing the operator's launching message to the
first phase (now discovery), so the trigger reader lives here unchanged.
"""

from __future__ import annotations

from thesis_runtime.contracts.records import RecordNotFound

from ..activities import DevelopmentActivities
from ..contracts.creation import HypothesisDevelopmentRequest


def trigger_text(
    workflow: DevelopmentActivities,
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
