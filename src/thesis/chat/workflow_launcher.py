"""Launch a deterministic workflow from trusted tool context.

The workflow-launch control tool delegates here.  The launcher binds the chat
service once at composition, so the tool handler only supplies the workflow id
and the idea; the runtime resolves workspace, chat, actor, trigger message, and
caller anchor from the trusted
:class:`~thesis.agent.runtime.context.ToolContext`.  This replaces the former
chat-message trigger protocol: a workflow starts only through a model tool call,
always bound to the requesting message, and never through a synthetic trigger.

The immediate result is the execution handle and its terminal or running phase;
the deterministic workflow owns every phase transition.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Protocol, cast

from thesis.chat.contracts import RecordNotFound
from thesis.chat.responder import DevelopmentRuntime, development_summary
from thesis.chat.service import ChatService

if TYPE_CHECKING:
    from thesis.workflows.hypothesis_development import (
        HypothesisDevelopmentRequest,
    )

#: Publishes the durable workflow anchor to the live chat stream.
AnchorPublisher = Callable[[str, str, str], None]


class AnchoredWorkflowLaunch(Protocol):
    """The anchor-aware launch surface of the development workflow."""

    def launch(
        self,
        workspace_id: str,
        chat_id: str,
        *,
        actor_id: str,
        ideas: str,
        trigger_message_id: str | None = None,
        investigation_id: str | None = None,
        anchor_sub_agent_id: str | None = None,
    ) -> HypothesisDevelopmentRequest:
        """Persist the chat trigger and return the runnable request."""


class RuntimeWorkflowLauncher:
    """Adapt the workflow-development runtime to the workflow-launch tool."""

    def __init__(
        self,
        runtime: DevelopmentRuntime,
        chat: ChatService,
        *,
        publish_anchor: AnchorPublisher | None = None,
    ) -> None:
        self._runtime = runtime
        self._chat = chat
        self._publish_anchor = publish_anchor

    def start_hypothesis_development(
        self,
        *,
        workspace_id: str,
        chat_id: str,
        actor_id: str,
        ideas: str,
        investigation_id: str | None = None,
        trigger_message_id: str | None = None,
        anchor_sub_agent_id: str | None = None,
    ) -> Mapping[str, object]:
        """Launch and run one hypothesis-development execution.

        Every launch binds to the requesting operator message; a launch without
        one fails instead of inventing a trigger.  A supplied
        ``investigation_id`` is a link, not a prerequisite: an id the workspace
        does not hold is dropped with a warning so a model-invented slug can
        never abort an otherwise valid launch.
        """
        if trigger_message_id is None or not trigger_message_id.strip():
            raise ValueError(
                "a workflow launch requires the requesting message; no synthetic "
                "trigger is created"
            )
        resolved_investigation = investigation_id
        warnings: list[str] = []
        if investigation_id is not None:
            try:
                self._chat.get_investigation(workspace_id, investigation_id)
            except RecordNotFound:
                resolved_investigation = None
                warnings.append(
                    f"investigation {investigation_id!r} was not found in this "
                    "workspace; the execution was launched without an "
                    "investigation link"
                )
        workflow = cast(
            AnchoredWorkflowLaunch,
            self._runtime.build_hypothesis_development(chat=self._chat),
        )
        request = workflow.launch(
            workspace_id,
            chat_id,
            actor_id=actor_id,
            ideas=ideas,
            investigation_id=resolved_investigation,
            trigger_message_id=trigger_message_id,
            anchor_sub_agent_id=anchor_sub_agent_id,
        )
        outcome = self._runtime.execute_hypothesis_development(request, chat=self._chat)
        if self._publish_anchor is not None:
            self._publish_anchor(workspace_id, chat_id, request.execution_id)
        verdict = outcome.verdict
        return {
            "executionId": outcome.execution_id,
            "phase": outcome.phase.value,
            "summary": development_summary(outcome),
            "verdict": (
                None
                if verdict is None
                else {
                    "outcome": verdict.outcome,
                    "reason": verdict.reason,
                }
            ),
            "warnings": warnings,
        }


__all__ = [
    "AnchorPublisher",
    "AnchoredWorkflowLaunch",
    "RuntimeWorkflowLauncher",
]
