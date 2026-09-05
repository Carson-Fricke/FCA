"""The enforced go-live gate: audited live-deployment eligibility.

The gate is the fail-closed state machine between paper rehearsal and live
deployment.  It accumulates auditable evidence — paper session outcomes,
paper/backtest parity reports, universe declarations, risk pipeline and fee
schedule identities, and pre-trade veto records — and turns that evidence
into an explicit eligibility decision through five independent, individually
auditable criteria:

1. ``clean_session_streak`` — at least ``required_clean_sessions``
   (default ten) *consecutive* clean paper sessions; any aborted, halted, or
   otherwise unclean session resets the streak.
2. ``parity_evidence`` — a recorded :class:`~harness.testing.paper_parity.
   PaperParityReport` whose latest run is clean, so the paper runtime is
   proven ledger-identical to the backtest engine before live capital is
   considered.  The gate composes the parity harness directly
   (:meth:`GoLiveGate.record_parity`) or accepts recorded reports.
3. ``universe_parity`` — every paper session was run over one identical
   strategy universe, and — when the gate is bound to a live candidate
   configuration — that universe equals the candidate's.
4. ``risk_parity`` — every paper session ran with one identical pre-trade
   risk pipeline and fee schedule (canonical configuration hashes), and —
   when bound — they equal the candidate's.
5. ``risk_veto_sanity`` — every recorded pre-trade veto carries an explicit
   reason and no veto requested a runtime halt, so risk behavior during
   rehearsal is explainable and never broken at the runtime boundary.

Evaluation drives the ``not_eligible → accumulating → eligible`` state
machine.  Every evaluation appends an auditable decision, every state change
appends a transition record, every criterion carries its own evidence, and a
refusal enumerates every unmet criterion.  Nothing in this module reads a
wall clock, draws randomness, or performs I/O: evidence enters through
explicit records, and eligibility is a pure function of the accumulated
evidence.  :func:`validate_live_deployment` composes the gate with
deployment validation so an ineligible or mismatched configuration can never
pass as a live strategy deployment.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from harness.configuration import TraderConfig, canonical_hash
from harness.core.types.common import text
from harness.runtime.paper import PaperSessionResult, PaperSessionStatus
from harness.testing.paper_parity import (
    PaperParityReport,
    PaperParityScenario,
    run_paper_parity,
)

_GATE_SCHEMA: Final = "fca-go-live-decision-v1"


class GoLiveGateError(RuntimeError):
    """Base class for every go-live gate error."""


class GateState(StrEnum):
    """The three explicit states of the go-live gate.

    ``NOT_ELIGIBLE`` is the initial state before any evidence exists;
    ``ACCUMULATING`` means evidence exists but at least one criterion is
    unmet; ``ELIGIBLE`` means every criterion is satisfied with its
    evidence attached.  Transitions are audited in evaluation order and a
    new unmet criterion — such as a dirty session resetting the clean
    streak — moves an eligible gate back.
    """

    NOT_ELIGIBLE = "not_eligible"
    ACCUMULATING = "accumulating"
    ELIGIBLE = "eligible"


class GoLiveRefused(GoLiveGateError):
    """Raised when a live deployment or gate requirement is refused.

    ``refusals`` enumerates every unmet requirement with its evidence-linked
    detail; ``decision`` carries the full auditable gate decision when the
    refusal came from an evaluation.
    """

    def __init__(
        self,
        refusals: tuple[str, ...],
        *,
        decision: GateDecision | None = None,
    ) -> None:
        self.refusals = refusals
        self.decision = decision
        super().__init__(
            ("go-live refused: " + "; ".join(refusals)) if refusals else "refused"
        )


# --------------------------------------------------------------------------
# Evidence records
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PaperSessionEvidence:
    """One auditable paper-session outcome supplied to the gate.

    ``clean`` is the session-level verdict: an aborted session, a halted or
    errored strategy teardown, or a lookahead violation makes the session
    unclean and resets the clean streak.  ``universe`` is the sorted,
    deduplicated strategy symbol universe the session ran over;
    ``risk_pipeline_hash`` and ``fee_schedule_hash`` pin the exact risk and
    fee identities used.  Veto records carry the pre-trade risk outcome:
    every veto must be explainable and none may request a runtime halt.
    """

    run_id: str
    clean: bool
    universe: tuple[str, ...]
    risk_pipeline_hash: str
    fee_schedule_hash: str
    unclean_reason: str | None = None
    veto_count: int = 0
    unexplained_vetoes: int = 0
    runtime_halt_vetoes: int = 0
    veto_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", text(self.run_id, "run_id"))
        if not isinstance(self.clean, bool):
            raise ValueError("session evidence clean must be a boolean")
        if not self.clean and self.unclean_reason is None:
            raise ValueError("unclean session evidence requires an unclean_reason")
        if self.unclean_reason is not None:
            object.__setattr__(
                self, "unclean_reason", text(self.unclean_reason, "unclean_reason")
            )
        if not self.universe:
            raise ValueError("session evidence requires a non-empty universe")
        if any(not isinstance(symbol, str) or not symbol for symbol in self.universe):
            raise ValueError("session evidence universe symbols must be non-empty")
        if len(set(self.universe)) != len(self.universe):
            raise ValueError("session evidence universe symbols must be unique")
        if self.universe != tuple(sorted(self.universe)):
            raise ValueError("session evidence universe must be sorted")
        object.__setattr__(
            self,
            "risk_pipeline_hash",
            text(self.risk_pipeline_hash, "risk_pipeline_hash"),
        )
        object.__setattr__(
            self, "fee_schedule_hash", text(self.fee_schedule_hash, "fee_schedule_hash")
        )
        for name in ("veto_count", "unexplained_vetoes", "runtime_halt_vetoes"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(
                    f"session evidence {name} must be a non-negative integer"
                )
        if self.unexplained_vetoes > self.veto_count:
            raise ValueError(
                "session evidence unexplained_vetoes cannot exceed veto_count"
            )
        if self.runtime_halt_vetoes > self.veto_count:
            raise ValueError(
                "session evidence runtime_halt_vetoes cannot exceed veto_count"
            )
        if len(self.veto_reasons) != self.veto_count - self.unexplained_vetoes:
            raise ValueError(
                "session evidence veto_reasons must carry one reason per explained veto"
            )
        object.__setattr__(self, "veto_reasons", tuple(self.veto_reasons))

    @classmethod
    def from_paper_result(
        cls,
        result: PaperSessionResult,
        configuration: TraderConfig,
    ) -> PaperSessionEvidence:
        """Build one session evidence record from a real paper session.

        Cleanliness follows the session status and lifecycle: an aborted
        session, halted strategies, teardown errors, or lookahead violations
        make the session unclean with the first offending fact as the
        reason.  Veto records derive from the session's complete risk
        decisions.
        """
        if not isinstance(result, PaperSessionResult):
            raise TypeError("result must be a PaperSessionResult")
        if not isinstance(configuration, TraderConfig):
            raise TypeError("configuration must be a TraderConfig")
        unclean: list[str] = []
        if result.status is PaperSessionStatus.ABORTED:
            unclean.append(f"session aborted: {result.reason}")
        if result.halted_strategies:
            unclean.append(f"halted strategies: {', '.join(result.halted_strategies)}")
        if result.teardown_errors:
            unclean.append(
                f"strategy teardown errors: {', '.join(result.teardown_errors)}"
            )
        if result.lookahead_violations:
            unclean.append(
                "lookahead violations: " + ", ".join(result.lookahead_violations)
            )
        vetoes = [decision for decision in result.risk_decisions if decision.rejected]
        reasons: list[str] = []
        unexplained = 0
        runtime_halts = 0
        for decision in vetoes:
            reason = decision.reason
            if reason is None and decision.reasons:
                reason = decision.reasons[-1]
            if reason is None:
                unexplained += 1
            else:
                reasons.append(reason)
            if decision.halt_scope == "halt_runtime":
                runtime_halts += 1
        return cls(
            run_id=result.run_id,
            clean=not unclean,
            unclean_reason=unclean[0] if unclean else None,
            universe=_config_universe(configuration),
            risk_pipeline_hash=canonical_hash(configuration.risk_pipeline),
            fee_schedule_hash=canonical_hash(configuration.fees),
            veto_count=len(vetoes),
            unexplained_vetoes=unexplained,
            runtime_halt_vetoes=runtime_halts,
            veto_reasons=tuple(reasons),
        )


def _config_universe(configuration: TraderConfig) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                str(symbol)
                for instance in configuration.strategy_instances
                for symbol in instance.symbols
            }
        )
    )


# --------------------------------------------------------------------------
# Gate decisions
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GateCriterion:
    """One independently auditable gate criterion and its evidence."""

    name: str
    satisfied: bool
    detail: str
    evidence: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", text(self.name, "name"))
        if not isinstance(self.satisfied, bool):
            raise ValueError("criterion satisfied must be a boolean")
        object.__setattr__(self, "detail", text(self.detail, "detail"))
        object.__setattr__(self, "evidence", MappingProxyType(dict(self.evidence)))


@dataclass(frozen=True, slots=True)
class GateTransition:
    """One audited state change of the go-live gate."""

    sequence: int
    from_state: GateState
    to_state: GateState

    def __post_init__(self) -> None:
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int):
            raise ValueError("gate transition sequence must be an integer")
        if self.sequence < 1:
            raise ValueError("gate transition sequence must be positive")
        object.__setattr__(self, "from_state", GateState(self.from_state))
        object.__setattr__(self, "to_state", GateState(self.to_state))


@dataclass(frozen=True, slots=True)
class GateDecision:
    """One auditable eligibility evaluation of the go-live gate."""

    sequence: int
    state: GateState
    eligible: bool
    criteria: tuple[GateCriterion, ...]
    refusals: tuple[str, ...]

    def __post_init__(self) -> None:
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int):
            raise ValueError("gate decision sequence must be an integer")
        if self.sequence < 1:
            raise ValueError("gate decision sequence must be positive")
        object.__setattr__(self, "state", GateState(self.state))
        object.__setattr__(self, "criteria", tuple(self.criteria))
        object.__setattr__(self, "refusals", tuple(self.refusals))
        unmet = tuple(
            criterion.name for criterion in self.criteria if not criterion.satisfied
        )
        if self.eligible and unmet:
            raise ValueError("an eligible gate decision cannot have unmet criteria")
        if not self.eligible and not unmet:
            raise ValueError("an ineligible gate decision must name unmet criteria")

    @property
    def unmet(self) -> tuple[str, ...]:
        """Return the names of every unmet criterion in evaluation order."""
        return tuple(
            criterion.name for criterion in self.criteria if not criterion.satisfied
        )

    def require_eligible(self) -> GateDecision:
        """Return this decision or raise :class:`GoLiveRefused` on refusal."""
        if not self.eligible:
            raise GoLiveRefused(self.refusals, decision=self)
        return self

    def artifacts(self) -> dict[str, object]:
        """Return the canonical, JSON-ready gate decision."""
        return {
            "schema": _GATE_SCHEMA,
            "sequence": self.sequence,
            "state": self.state.value,
            "eligible": self.eligible,
            "criteria": [
                {
                    "name": criterion.name,
                    "satisfied": criterion.satisfied,
                    "detail": criterion.detail,
                    "evidence": dict(criterion.evidence),
                }
                for criterion in self.criteria
            ],
            "refusals": list(self.refusals),
        }


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------


class GoLiveGate:
    """The enforced, evidence-accumulating gate before live deployment.

    Evidence enters only through explicit ``record_*`` calls; each call is
    validated and stored immutably.  :meth:`evaluate` is the single state
    transition point: it recomputes every criterion from the accumulated
    evidence, appends one auditable :class:`GateDecision`, and records one
    :class:`GateTransition` whenever the gate state actually changes.  The
    gate refuses — never warns — when any criterion is unmet.
    """

    def __init__(
        self,
        *,
        required_clean_sessions: int = 10,
        live_configuration: TraderConfig | None = None,
    ) -> None:
        if isinstance(required_clean_sessions, bool) or not isinstance(
            required_clean_sessions, int
        ):
            raise ValueError("required_clean_sessions must be an integer")
        if required_clean_sessions < 1:
            raise ValueError("required_clean_sessions must be positive")
        if live_configuration is not None and not isinstance(
            live_configuration, TraderConfig
        ):
            raise ValueError("live_configuration must be a TraderConfig")
        self._required_clean_sessions = required_clean_sessions
        self._live_configuration = live_configuration
        self._sessions: tuple[PaperSessionEvidence, ...] = ()
        self._parity_reports: tuple[PaperParityReport, ...] = ()
        self._state = GateState.NOT_ELIGIBLE
        self._decisions: tuple[GateDecision, ...] = ()
        self._transitions: tuple[GateTransition, ...] = ()

    # -- evidence accumulation ----------------------------------------------

    def record_paper_session(self, evidence: PaperSessionEvidence) -> None:
        """Record one validated paper-session outcome."""
        if not isinstance(evidence, PaperSessionEvidence):
            raise TypeError("evidence must be a PaperSessionEvidence")
        self._sessions = (*self._sessions, evidence)

    def record_paper_result(
        self,
        result: PaperSessionResult,
        configuration: TraderConfig,
    ) -> PaperSessionEvidence:
        """Build evidence from a real paper session and record it."""
        evidence = PaperSessionEvidence.from_paper_result(result, configuration)
        self.record_paper_session(evidence)
        return evidence

    def record_parity_report(self, report: PaperParityReport) -> None:
        """Record one parity harness report as gate evidence."""
        if not isinstance(report, PaperParityReport):
            raise TypeError("report must be a PaperParityReport")
        self._parity_reports = (*self._parity_reports, report)

    def record_parity(self, scenario: PaperParityScenario) -> PaperParityReport:
        """Run the parity harness for ``scenario`` and record its report."""
        report = run_paper_parity(scenario)
        self.record_parity_report(report)
        return report

    # -- observed state ------------------------------------------------------

    @property
    def state(self) -> GateState:
        """Return the current gate state."""
        return self._state

    @property
    def required_clean_sessions(self) -> int:
        """Return how many consecutive clean sessions eligibility requires."""
        return self._required_clean_sessions

    @property
    def live_configuration(self) -> TraderConfig | None:
        """Return the bound live candidate configuration, if any."""
        return self._live_configuration

    @property
    def sessions(self) -> tuple[PaperSessionEvidence, ...]:
        """Return every recorded session evidence in record order."""
        return self._sessions

    @property
    def parity_reports(self) -> tuple[PaperParityReport, ...]:
        """Return every recorded parity report in record order."""
        return self._parity_reports

    @property
    def decisions(self) -> tuple[GateDecision, ...]:
        """Return every evaluation decision in evaluation order."""
        return self._decisions

    @property
    def transitions(self) -> tuple[GateTransition, ...]:
        """Return every audited state transition in transition order."""
        return self._transitions

    # -- evaluation ----------------------------------------------------------

    def evaluate(self) -> GateDecision:
        """Recompute every criterion and advance the gate state machine.

        The new state is :attr:`GateState.ELIGIBLE` only when every
        criterion is satisfied; :attr:`GateState.ACCUMULATING` once any
        session evidence exists, and :attr:`GateState.NOT_ELIGIBLE` before
        any evidence.  Every evaluation appends one decision; every actual
        state change appends one transition.
        """
        criteria = (
            self._criterion_clean_streak(),
            self._criterion_parity_evidence(),
            self._criterion_universe_parity(),
            self._criterion_risk_parity(),
            self._criterion_risk_veto_sanity(),
        )
        unmet = tuple(
            criterion.name for criterion in criteria if not criterion.satisfied
        )
        eligible = not unmet
        if eligible:
            new_state = GateState.ELIGIBLE
        elif self._sessions:
            new_state = GateState.ACCUMULATING
        else:
            new_state = GateState.NOT_ELIGIBLE
        decision = GateDecision(
            sequence=len(self._decisions) + 1,
            state=new_state,
            eligible=eligible,
            criteria=criteria,
            refusals=tuple(
                f"{criterion.name}: {criterion.detail}"
                for criterion in criteria
                if not criterion.satisfied
            ),
        )
        self._decisions = (*self._decisions, decision)
        if new_state is not self._state:
            self._transitions = (
                *self._transitions,
                GateTransition(
                    sequence=len(self._transitions) + 1,
                    from_state=self._state,
                    to_state=new_state,
                ),
            )
            self._state = new_state
        return decision

    def require_eligible(self) -> GateDecision:
        """Evaluate and return the decision, or raise :class:`GoLiveRefused`."""
        return self.evaluate().require_eligible()

    # -- criteria ------------------------------------------------------------

    def _criterion_clean_streak(self) -> GateCriterion:
        streak = _clean_streak(self._sessions)
        satisfied = streak >= self._required_clean_sessions
        if satisfied:
            detail = (
                f"{streak} consecutive clean sessions meet the required "
                f"{self._required_clean_sessions}"
            )
        elif not self._sessions:
            detail = (
                f"no paper session evidence; {self._required_clean_sessions} "
                "consecutive clean sessions required"
            )
        else:
            detail = (
                f"{streak} consecutive clean sessions of the required "
                f"{self._required_clean_sessions}"
            )
        evidence: dict[str, object] = {
            "streak": streak,
            "required": self._required_clean_sessions,
            "recorded_sessions": len(self._sessions),
            "sessions": [
                {
                    "run_id": session.run_id,
                    "clean": session.clean,
                    "unclean_reason": session.unclean_reason,
                }
                for session in self._sessions
            ],
        }
        return GateCriterion("clean_session_streak", satisfied, detail, evidence)

    def _criterion_parity_evidence(self) -> GateCriterion:
        reports = [
            {
                "run_id": report.run_id,
                "parity_clean": report.parity_clean,
                "scenario_hash": report.scenario_hash,
                "divergences": [divergence.value for divergence in report.divergences],
            }
            for report in self._parity_reports
        ]
        if not self._parity_reports:
            return GateCriterion(
                "parity_evidence",
                False,
                "no paper/backtest parity report recorded",
                {"reports": reports},
            )
        latest = self._parity_reports[-1]
        if not latest.parity_clean:
            detail = (
                f"latest parity report for {latest.run_id!r} diverged: "
                + ", ".join(divergence.value for divergence in latest.divergences)
            )
            return GateCriterion("parity_evidence", False, detail, {"reports": reports})
        return GateCriterion(
            "parity_evidence",
            True,
            f"latest parity report for {latest.run_id!r} is clean",
            {"reports": reports},
        )

    def _criterion_universe_parity(self) -> GateCriterion:
        candidate = (
            None
            if self._live_configuration is None
            else _config_universe(self._live_configuration)
        )
        evidence: dict[str, object] = {
            "sessions": [list(session.universe) for session in self._sessions],
            "candidate_universe": None if candidate is None else list(candidate),
        }
        if not self._sessions:
            return GateCriterion(
                "universe_parity",
                False,
                "no paper session evidence to compare universes",
                evidence,
            )
        universes = {session.universe for session in self._sessions}
        if len(universes) > 1:
            return GateCriterion(
                "universe_parity",
                False,
                "paper sessions ran over more than one strategy universe",
                evidence,
            )
        if candidate is not None and self._sessions[0].universe != candidate:
            return GateCriterion(
                "universe_parity",
                False,
                "paper session universe differs from the live candidate universe",
                evidence,
            )
        return GateCriterion(
            "universe_parity",
            True,
            "paper sessions ran over one strategy universe matching the live candidate"
            if candidate is not None
            else "paper sessions ran over one strategy universe",
            evidence,
        )

    def _criterion_risk_parity(self) -> GateCriterion:
        candidate: tuple[str, str] | None = None
        if self._live_configuration is not None:
            configuration = self._live_configuration
            candidate = (
                canonical_hash(configuration.risk_pipeline),
                canonical_hash(configuration.fees),
            )
        evidence: dict[str, object] = {
            "sessions": [
                {
                    "run_id": session.run_id,
                    "risk_pipeline_hash": session.risk_pipeline_hash,
                    "fee_schedule_hash": session.fee_schedule_hash,
                }
                for session in self._sessions
            ],
            "candidate_risk_pipeline_hash": (
                None if candidate is None else candidate[0]
            ),
            "candidate_fee_schedule_hash": (
                None if candidate is None else candidate[1]
            ),
        }
        if not self._sessions:
            return GateCriterion(
                "risk_parity",
                False,
                "no paper session evidence to compare risk identities",
                evidence,
            )
        identities = {
            (session.risk_pipeline_hash, session.fee_schedule_hash)
            for session in self._sessions
        }
        if len(identities) > 1:
            return GateCriterion(
                "risk_parity",
                False,
                "paper sessions ran under more than one risk pipeline or "
                "fee schedule identity",
                evidence,
            )
        if candidate is not None and next(iter(identities)) != candidate:
            return GateCriterion(
                "risk_parity",
                False,
                "paper session risk pipeline or fee schedule differs from "
                "the live candidate",
                evidence,
            )
        return GateCriterion(
            "risk_parity",
            True,
            "paper sessions ran under one risk pipeline and fee schedule "
            "matching the live candidate"
            if candidate is not None
            else "paper sessions ran under one risk pipeline and fee schedule",
            evidence,
        )

    def _criterion_risk_veto_sanity(self) -> GateCriterion:
        sessions = [
            {
                "run_id": session.run_id,
                "veto_count": session.veto_count,
                "unexplained_vetoes": session.unexplained_vetoes,
                "runtime_halt_vetoes": session.runtime_halt_vetoes,
            }
            for session in self._sessions
        ]
        evidence: dict[str, object] = {"sessions": sessions}
        if not self._sessions:
            return GateCriterion(
                "risk_veto_sanity",
                False,
                "no paper session evidence to audit risk vetoes",
                evidence,
            )
        unexplained = sum(session.unexplained_vetoes for session in self._sessions)
        runtime_halts = sum(session.runtime_halt_vetoes for session in self._sessions)
        if unexplained:
            detail = f"{unexplained} vetoes without an explicit reason"
            return GateCriterion("risk_veto_sanity", False, detail, evidence)
        if runtime_halts:
            detail = f"{runtime_halts} vetoes requested a runtime halt"
            return GateCriterion("risk_veto_sanity", False, detail, evidence)
        total = sum(session.veto_count for session in self._sessions)
        return GateCriterion(
            "risk_veto_sanity",
            True,
            f"{total} recorded vetoes all carry explicit reasons and none "
            "requested a runtime halt",
            evidence,
        )


def _clean_streak(sessions: tuple[PaperSessionEvidence, ...]) -> int:
    streak = 0
    for session in reversed(sessions):
        if not session.clean:
            break
        streak += 1
    return streak


# --------------------------------------------------------------------------
# Deployment validation
# --------------------------------------------------------------------------


def validate_live_deployment(
    gate: GoLiveGate, configuration: TraderConfig
) -> GateDecision:
    """Validate one live strategy deployment against the gate's evidence.

    The deployment is refused unless the configuration is a live-mode trader
    configuration, the gate is eligible, the gate was evaluated against this
    exact configuration when bound, and the session evidence matches the
    configuration's universe and risk identities.  Every refusal is
    evidence-linked and enumerated.
    """
    if not isinstance(gate, GoLiveGate):
        raise TypeError("gate must be a GoLiveGate")
    if not isinstance(configuration, TraderConfig):
        raise TypeError("configuration must be a TraderConfig")
    decision = gate.evaluate()
    refusals: list[str] = []
    if configuration.mode != "live":
        refusals.append(
            f"live deployment requires mode 'live', got {configuration.mode!r}"
        )
    if gate.live_configuration is not None and (
        canonical_hash(gate.live_configuration) != canonical_hash(configuration)
    ):
        refusals.append(
            "deployment configuration differs from the gate's evaluated "
            "live configuration"
        )
    universe = _config_universe(configuration)
    if gate.sessions and any(session.universe != universe for session in gate.sessions):
        refusals.append(
            "deployment universe differs from the recorded paper session universe"
        )
    risk_hash = canonical_hash(configuration.risk_pipeline)
    fee_hash = canonical_hash(configuration.fees)
    if gate.sessions and any(
        session.risk_pipeline_hash != risk_hash or session.fee_schedule_hash != fee_hash
        for session in gate.sessions
    ):
        refusals.append(
            "deployment risk pipeline or fee schedule differs from the "
            "recorded paper session evidence"
        )
    if not decision.eligible:
        refusals.extend(decision.refusals)
    if refusals:
        raise GoLiveRefused(tuple(refusals), decision=decision)
    return decision


__all__ = [
    "GateCriterion",
    "GateDecision",
    "GateState",
    "GateTransition",
    "GoLiveGate",
    "GoLiveGateError",
    "GoLiveRefused",
    "PaperSessionEvidence",
    "validate_live_deployment",
]
