"""Launch a deterministic workflow from trusted tool context.

The workflow-launch control tool delegates here.  The launcher binds the chat
service once at composition, so the tool handler only supplies the workflow id
and the idea; the runtime resolves workspace, chat, actor, trigger message, and
caller anchor from the trusted
:class:`~thesis.agent.runtime.context.ToolContext`.  This replaces the former
chat-message trigger protocol: a workflow starts only through a model tool call,
always bound to the requesting message, and never through a synthetic trigger.

Launch is asynchronous by construction.  The tool returns the execution-handle
datum as soon as the durable execution exists; the workflow executes detached,
and its outcome is delivered later through the unified
:meth:`~thesis.agent.runtime.delivery.DeliveryService.deliver` path at the
caller's anchor, which persists the datum and schedules that anchor's turn.  A
workflow never executes inside the tool call that launched it.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Protocol, cast

from thesis.agent.runtime.datum import Datum, DatumType, datum_from_result
from thesis.agent.runtime.delivery import Anchor, DeliveryService, TurnScheduler
from thesis.chat.contracts import RecordNotFound
from thesis.chat.responder import DevelopmentRuntime, development_summary
from thesis.chat.service import ChatService

if TYPE_CHECKING:
    from thesis.workflows.hypothesis_development import (
        HypothesisDevelopmentOutcome,
        HypothesisDevelopmentRequest,
    )

#: Publishes the durable workflow anchor to the live chat stream.
AnchorPublisher = Callable[[str, str, str], None]

#: Runs one zero-argument callable out of band.
Detach = Callable[[Callable[[], None]], None]

#: The initial phase of a freshly launched execution, before the detached run
#: advances it.
_LAUNCHED_PHASE = "requested"


def _daemon_thread(function: Callable[[], None]) -> None:
    """Default detach seam: a daemon thread that never blocks the turn."""
    threading.Thread(target=function, daemon=True).start()


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
        detach: Detach | None = None,
        turn_scheduler: TurnScheduler | None = None,
        delivery: DeliveryService | None = None,
    ) -> None:
        self._runtime = runtime
        self._chat = chat
        self._publish_anchor = publish_anchor
        self._detach = detach or _daemon_thread
        self._delivery = delivery or DeliveryService(chat, schedule=turn_scheduler)

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
        """Launch one hypothesis-development execution and return its handle.

        Every launch binds to the requesting operator message; a launch without
        one fails instead of inventing a trigger.  A supplied
        ``investigation_id`` is a link, not a prerequisite: an id the workspace
        does not hold is dropped with a warning so a model-invented slug can
        never abort an otherwise valid launch.

        The execution runs detached.  The immediate result is the execution
        handle; the workflow's outcome is delivered at the caller's anchor as a
        durable ``workflow_execution`` datum once the detached run returns.
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
        if self._publish_anchor is not None:
            self._publish_anchor(workspace_id, chat_id, request.execution_id)
        anchor = Anchor(workspace_id, chat_id, sub_agent_id=anchor_sub_agent_id)
        self._detach(lambda: self._finish(request, anchor))
        return {
            "executionId": request.execution_id,
            "phase": _LAUNCHED_PHASE,
            "summary": (
                f"Hypothesis development {request.execution_id} started and is "
                f"running ({_LAUNCHED_PHASE})."
            ),
            "verdict": None,
            "warnings": warnings,
        }

    def _finish(
        self,
        request: HypothesisDevelopmentRequest,
        anchor: Anchor,
    ) -> None:
        """Run the detached execution and deliver its outcome at the anchor."""
        try:
            outcome = self._runtime.execute_hypothesis_development(
                request, chat=self._chat
            )
        except Exception as error:  # noqa: BLE001 - a launch fault is delivered
            message = str(error) or type(error).__name__
            datum = datum_from_result(
                DatumType.WORKFLOW_EXECUTION.value,
                ok=False,
                structured={
                    "executionId": request.execution_id,
                    "phase": "failed",
                    "verdict": None,
                },
                text=(
                    f"Hypothesis development {request.execution_id} failed: {message}."
                ),
                failure={"kind": "workflow", "message": message},
            )
        else:
            datum = self._outcome_datum(outcome)
        self._delivery.deliver(
            anchor,
            datum,
            execution_id=request.execution_id,
            data={"kind": "workflow_outcome", "executionId": request.execution_id},
        )

    def _outcome_datum(self, outcome: HypothesisDevelopmentOutcome) -> Datum:
        """Project one workflow outcome into its delivered datum."""
        verdict = outcome.verdict
        return datum_from_result(
            DatumType.WORKFLOW_EXECUTION.value,
            ok=outcome.phase.value != "failed",
            structured={
                "executionId": outcome.execution_id,
                "phase": outcome.phase.value,
                "verdict": (
                    None
                    if verdict is None
                    else {"outcome": verdict.outcome, "reason": verdict.reason}
                ),
            },
            text=development_summary(outcome),
        )


__all__ = [
    "AnchorPublisher",
    "AnchoredWorkflowLaunch",
    "Detach",
    "RuntimeWorkflowLauncher",
]
