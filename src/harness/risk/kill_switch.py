"""Explicit, audited global kill switch for the pre-trade risk pipelines.

The kill switch is the last line of defense for paper and live runtimes: one
operator-owned :class:`~harness.risk.models.RiskRule` that, while engaged,
rejects every candidate order with an explicit, audited decision and marks
the rejection as a halt.  Engagement and disengagement are explicit operator
or engine calls recorded in an immutable audit trail; the switch has no
implicit timeouts, no wall clock, no randomness, and no I/O, so a disengaged
switch leaves the pipeline golden-identical to the same pipeline without the
rule.

Halt composition follows the established taxonomy: the rule rejects with
``halt=True`` and the pipeline's ``on_reject`` policy turns that flag into
the audited ``RiskDecision.halt_scope`` — the default ``reject_order``
policy blocks order flow only, ``halt_strategy`` halts the submitting
strategy, and ``halt_runtime`` aborts the whole session.  Compose the switch
as the first rule so a trip short-circuits every downstream rule.

The switch is single-threaded by construction: the synchronous kernel never
runs concurrently, and every state change happens through an explicit
:meth:`KillSwitch.trip` or :meth:`KillSwitch.release` call, matching the
repository's determinism conventions.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from harness.core.types.common import text
from harness.core.types.orders import OrderRequest
from harness.risk.limits import ConfiguredRiskRule
from harness.risk.models import RiskContext, RuleDecision

DEFAULT_KILL_SWITCH_RULE_ID: Final[str] = "global-kill-switch"


class KillSwitchState(StrEnum):
    """The two explicit states of the global kill switch."""

    ENGAGED = "engaged"
    DISENGAGED = "disengaged"


class KillSwitchAction(StrEnum):
    """The audited control actions recorded by the switch."""

    TRIP = "trip"
    RELEASE = "release"


@dataclass(frozen=True, slots=True)
class KillSwitchEvent:
    """One immutable, audited kill-switch control action.

    Events carry no timestamp: the synchronous kernel has no wall clock, and
    provenance stamping is the deployment boundary's responsibility.  The
    ``sequence`` is a 1-based ordinal in the owning switch's audit trail.
    """

    sequence: int
    action: KillSwitchAction
    reason: str | None = None
    actor: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int):
            raise ValueError("kill switch event sequence must be an integer")
        if self.sequence < 1:
            raise ValueError("kill switch event sequence must be positive")
        if not isinstance(self.action, KillSwitchAction):
            object.__setattr__(self, "action", KillSwitchAction(self.action))
        if self.reason is not None:
            object.__setattr__(self, "reason", text(self.reason, "reason"))
        if self.actor is not None:
            object.__setattr__(self, "actor", text(self.actor, "actor"))


class KillSwitch(ConfiguredRiskRule):
    """Global kill switch rule: reject every order while engaged.

    The switch implements the structural
    :class:`~harness.risk.models.RiskRule` contract, so it composes into
    :class:`~harness.risk.pipeline.RiskPipeline` like any other rule.  While
    disengaged it allows every order unchanged, leaving the pipeline's
    decisions identical to the same pipeline without this rule.  While
    engaged it rejects every order with ``halt=True``, an explicit reason,
    and metrics naming the active trip, so every veto is fully audited
    through the pipeline's existing ``RiskAudit`` trail.

    Trips and releases never expire and never fire on their own: only an
    explicit :meth:`trip` or :meth:`release` call changes the state, and
    every call — including redundant ones — appends one audited
    :class:`KillSwitchEvent`.  The switch has no configurable parameters:
    engagement is an operator action, not configuration, and unknown
    parameters are rejected instead of silently ignored.
    """

    __slots__ = ("_events", "_state")

    def __init__(
        self,
        parameters: Mapping[str, object] | object | None = None,
        *,
        rule_id: str | None = None,
        **values: object,
    ) -> None:
        super().__init__(
            parameters, rule_id=rule_id or DEFAULT_KILL_SWITCH_RULE_ID, **values
        )
        if self._parameters:
            unsupported = ", ".join(sorted(self._parameters))
            raise ValueError(
                f"the kill switch has no configurable parameters; got {unsupported}"
            )
        self._state = KillSwitchState.DISENGAGED
        self._events: tuple[KillSwitchEvent, ...] = ()

    @property
    def state(self) -> KillSwitchState:
        """Return the current explicit switch state."""
        return self._state

    @property
    def engaged(self) -> bool:
        """Whether the switch currently rejects every order."""
        return self._state is KillSwitchState.ENGAGED

    @property
    def events(self) -> tuple[KillSwitchEvent, ...]:
        """Return the complete ordered audit trail of control actions."""
        return self._events

    @property
    def last_event(self) -> KillSwitchEvent | None:
        """Return the most recent control action, if any."""
        return self._events[-1] if self._events else None

    def trip(
        self, *, reason: str | None = None, actor: str | None = None
    ) -> KillSwitchEvent:
        """Engage the switch and record one audited trip action.

        Every subsequent order is rejected with ``halt=True`` until an
        explicit :meth:`release`.  ``reason`` and ``actor`` are optional
        provenance for the audit trail and must be non-empty strings when
        supplied.  No time source is consulted.
        """
        return self._record(
            KillSwitchAction.TRIP, KillSwitchState.ENGAGED, reason, actor
        )

    def release(
        self, *, reason: str | None = None, actor: str | None = None
    ) -> KillSwitchEvent:
        """Disengage the switch and record one audited release action.

        Restores exactly the prior pipeline behavior: the switch allows
        every order unchanged again.  ``reason`` and ``actor`` are optional
        provenance for the audit trail and must be non-empty strings when
        supplied.  No time source is consulted.
        """
        return self._record(
            KillSwitchAction.RELEASE, KillSwitchState.DISENGAGED, reason, actor
        )

    # Operator-vocabulary aliases.
    engage = trip
    disengage = release

    def evaluate(self, order: OrderRequest, context: RiskContext) -> RuleDecision:
        """Return the kill-switch decision for one candidate order.

        While engaged, every order is rejected with ``halt=True`` and an
        explicit reason.  While disengaged, the order is allowed unchanged.
        The explicit context is accepted for the rule contract but is not
        consulted: the switch is global and changes only through explicit
        operator actions.
        """
        del context
        if self._state is KillSwitchState.ENGAGED:
            return RuleDecision.reject(
                order,
                f"{self.rule_id} engaged",
                metrics=self._metrics(),
                halt=True,
            )
        return RuleDecision.allow(order, metrics=self._metrics())

    def _record(
        self,
        action: KillSwitchAction,
        state: KillSwitchState,
        reason: str | None,
        actor: str | None,
    ) -> KillSwitchEvent:
        event = KillSwitchEvent(
            sequence=len(self._events) + 1,
            action=action,
            reason=reason,
            actor=actor,
        )
        self._state = state
        self._events = (*self._events, event)
        return event

    def _metrics(self) -> dict[str, object]:
        metrics: dict[str, object] = {
            "switch": self.rule_id,
            "state": self._state.value,
        }
        last = self.last_event
        if last is not None and last.action is KillSwitchAction.TRIP:
            metrics["trip_sequence"] = last.sequence
            metrics["trip_reason"] = last.reason
            metrics["trip_actor"] = last.actor
        return metrics


GlobalKillSwitch = KillSwitch


__all__ = [
    "DEFAULT_KILL_SWITCH_RULE_ID",
    "GlobalKillSwitch",
    "KillSwitch",
    "KillSwitchAction",
    "KillSwitchEvent",
    "KillSwitchState",
]
