"""Research workflow launch and outcome contracts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from thesis.extensions.workflows.hypothesis_development import (
        HypothesisDevelopmentOutcome,
        HypothesisDevelopmentRequest,
    )


class DevelopmentRuntime(Protocol):
    """The narrow runtime surface for the hypothesis-development pipeline."""

    def build_hypothesis_development(
        self, *, chat: object
    ) -> LaunchableDevelopmentWorkflow:
        """Compose the hypothesis-development workflow over the runtime ports."""


class LaunchableDevelopmentWorkflow(Protocol):
    """The launch surface of the hypothesis-development workflow."""

    def prepare_phase(self, request: HypothesisDevelopmentRequest) -> str | None: ...
    def enter_scheduled_phase(
        self, request: HypothesisDevelopmentRequest, payload: Mapping[str, object]
    ) -> str | None: ...

    def launch(
        self,
        workspace_id: str,
        chat_id: str,
        *,
        actor_id: str,
        ideas: str,
        investigation_id: str | None = None,
        trigger_message_id: str | None = None,
    ) -> HypothesisDevelopmentRequest:
        """Persist the chat trigger and return the runnable request."""


#: Active (non-terminal) hypothesis-development phases, by wire value.
_ACTIVE_DEVELOPMENT_PHASES = frozenset(
    {
        "requested",
        "discovery",
        "model_creation",
        "falsification_trials",
        "rca",
        "verdict",
        "strategy_design",
        "optimization",
        "null_testing",
        "variation",
        "forward_validation",
    }
)


def development_summary(outcome: HypothesisDevelopmentOutcome) -> str:
    """Return the assistant summary for one hypothesis-development execution.

    A running execution is reported as started/running; only a genuine terminal
    failure is phrased as a failure, and a model-format compose problem is
    delivered as an operator request instead of ending the run.
    """
    phase = outcome.phase.value
    execution_id = outcome.execution_id
    if phase in _ACTIVE_DEVELOPMENT_PHASES:
        return (
            f"Hypothesis development {execution_id} started and is running ({phase})."
        )
    if phase == "failed":
        reason = (outcome.detail or "").strip() or "an infrastructure error"
        return f"Hypothesis development {execution_id} failed: {reason}."
    verdict = outcome.verdict
    if verdict is None:
        return (
            f"Hypothesis development {execution_id} finished ({phase}); "
            "no falsification verdict was recorded."
        )
    return (
        f"Hypothesis development {execution_id} finished ({phase}); "
        f"falsification verdict: {verdict.outcome}. {verdict.reason}"
    )
