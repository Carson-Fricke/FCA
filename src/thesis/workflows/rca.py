"""Deterministic root-cause-analysis and bounded revision-loop workflow.

This module mirrors the established Thesis workflow state machine and applies
it to the research iteration loop: a recorded harness run is classified into a
deterministic failure taxonomy (data, harness, implementation, strategy,
market-regime), the classification drives an explicitly bounded number of
revision cycles, and every run and material adaptation is appended to the
append-only revision lineage in :mod:`thesis.research.lineage`.

Design rules:

- Workflow transitions are explicit and auditable: every move between phases
  goes through :func:`transition_phase` and is recorded as an immutable
  :class:`PhaseTransition`.  The machine is a real loop: ``recording`` may
  return to ``revising`` so each bounded cycle is individually observable.
- No wall-clock participates in state decisions.  Time and identifiers are
  injected, and steering fences are computed from durable intent epochs only.
- Classification is deterministic: the same run artifacts and pre-registered
  rules always produce the same :class:`RcaFinding`, including its recorded
  supporting and disconfirming evidence, competing explanations, confidence,
  and discriminating follow-up tests.
- Every external effect sits behind an injected port: backtests through
  :class:`thesis.workflows.backtests.BacktestRunner`, revisions through the
  :class:`RevisionProposer` port, durability through chat persistence, the
  revision lineage, and the command journal.  No live model is required: a
  deterministic proposer fake is sufficient to drive the loop in tests.
- The loop is bounded by construction: ``max_iterations`` is explicit on the
  request, validated at intake, and recorded on the outcome.  Engine and data
  defects are never "revised away"; they stop the loop as ``blocked`` so a
  strategy revision cannot mask a broken premise or a broken engine.

A failed hypothesis can only become positive through a newly recorded claim:
the lineage enforces this at append time and the workflow surfaces the
violation as a failure instead of silently rewriting the claim.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol

from thesis.chat.contracts import (
    RecordNotFound,
    WorkflowDefinition,
    WorkflowExecution,
    WorkflowExecutionState,
    replace_record,
)
from thesis.chat.service import ChatService
from thesis.execution.events import (
    ActorScope,
    EventReplay,
    ExecutionEvent,
    ExecutionEventType,
)
from thesis.experiments import ExperimentContract
from thesis.persistence.journal import ArtifactRef, CommandJournal, content_hash
from thesis.research.lineage import (
    LineageError,
    RevisionLineage,
    RevisionNode,
    RevisionOutcome,
    RunIdentity,
)
from thesis.strategies.bundles import StrategyBundle
from thesis.workflows.backtests import (
    BacktestRunError,
    BacktestRunner,
    BacktestRunRequest,
    BacktestRunResult,
    HarnessError,
)
from thesis.workflows.evidence import EXPLORATORY_DISCLOSURE

_WORKFLOW_ID = "rca-revision-loop"
_WORKFLOW_VERSION = "1.0.0"
_WORKFLOW_TITLE = "RCA and strategy revision loop"
_TOOL_NAME = "harness.backtest"
_TOOL_CORRELATION_PREFIX = "rca"
_WORKFLOW_ACTOR = "thesis-workflow"
_REPORT_MEDIA_TYPE = "application/json"
_REPORT_SCHEMA = "fca-thesis-rca-loop-report-v1"

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
_WORKSPACE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")

DEFAULT_MAX_ITERATIONS = 3
CRITERION_OPERATORS: frozenset[str] = frozenset({"gte", "lte", "gt", "lt"})
REVISABLE_CLASSES: frozenset[str] = frozenset(
    {
        "implementation",
        "market-regime",
        "strategy",
    }
)


class WorkflowError(RuntimeError):
    """Base error for invalid RCA revision-loop workflow operations."""


class InvalidPhaseTransition(WorkflowError):
    """A phase cannot move directly to the requested phase."""


class RcaIntakeError(WorkflowError):
    """Raised when a revision-loop request is not a valid intake record."""


class ClassificationError(WorkflowError):
    """Raised when run artifacts cannot be classified deterministically."""


class RevisionProposalError(WorkflowError):
    """Raised when an injected revision proposal is not usable."""


class _Fenced(Exception):
    """Internal signal that a continuation became stale and must pause.

    Carries the partial state recorded so far so in-flight results are not
    lost when the workflow pauses instead of continuing.
    """

    def __init__(
        self,
        message: str,
        state: RcaRevisionLoopState | None = None,
    ) -> None:
        super().__init__(message)
        self.state = state


class _PhaseFailure(WorkflowError):
    """Internal failure signal that carries the partial workflow state."""

    def __init__(self, message: str, state: RcaRevisionLoopState) -> None:
        super().__init__(message)
        self.state = state


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


class FailureClass(StrEnum):
    """The deterministic failure taxonomy for recorded run artifacts."""

    DATA = "data"
    HARNESS = "harness"
    IMPLEMENTATION = "implementation"
    STRATEGY = "strategy"
    MARKET_REGIME = "market-regime"


FAILURE_CLASSES: tuple[str, ...] = tuple(item.value for item in FailureClass)

_REVISABLE = frozenset(FailureClass(item) for item in REVISABLE_CLASSES)


class RcaLoopPhase(StrEnum):
    """The explicit, ordered phases of the RCA revision-loop workflow."""

    REQUESTED = "requested"
    ANALYZING = "analyzing"
    CLASSIFYING = "classifying"
    REVISING = "revising"
    RECORDING = "recording"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"


_REQUESTED = RcaLoopPhase.REQUESTED
_ANALYZING = RcaLoopPhase.ANALYZING
_CLASSIFYING = RcaLoopPhase.CLASSIFYING
_REVISING = RcaLoopPhase.REVISING
_RECORDING = RcaLoopPhase.RECORDING
_COMPLETED = RcaLoopPhase.COMPLETED
_FAILED = RcaLoopPhase.FAILED
_PAUSED = RcaLoopPhase.PAUSED

_TRANSITIONS: Mapping[RcaLoopPhase, frozenset[RcaLoopPhase]] = {
    _REQUESTED: frozenset({_ANALYZING, _PAUSED, _FAILED}),
    _ANALYZING: frozenset({_CLASSIFYING, _PAUSED, _FAILED}),
    _CLASSIFYING: frozenset({_RECORDING, _PAUSED, _FAILED}),
    _RECORDING: frozenset({_REVISING, _COMPLETED, _PAUSED, _FAILED}),
    _REVISING: frozenset({_ANALYZING, _PAUSED, _FAILED}),
    _PAUSED: frozenset({_ANALYZING, _CLASSIFYING, _REVISING, _RECORDING}),
    _COMPLETED: frozenset(),
    _FAILED: frozenset(),
}

_TERMINAL_EXECUTION_STATES = frozenset(
    {
        WorkflowExecutionState.COMPLETED,
        WorkflowExecutionState.FAILED,
        WorkflowExecutionState.STOPPED,
        WorkflowExecutionState.CANCELLED,
    }
)

RCA_LOOP_PHASES: tuple[str, ...] = (
    _REQUESTED.value,
    _ANALYZING.value,
    _CLASSIFYING.value,
    _REVISING.value,
    _RECORDING.value,
)

RCA_REVISION_LOOP_WORKFLOW = WorkflowDefinition(
    workflow_id=_WORKFLOW_ID,
    version=_WORKFLOW_VERSION,
    title=_WORKFLOW_TITLE,
    phases=RCA_LOOP_PHASES,
)


def transition_phase(current: RcaLoopPhase, target: RcaLoopPhase) -> RcaLoopPhase:
    """Move between phases through the explicit transition table."""
    if target not in _TRANSITIONS[current]:
        raise InvalidPhaseTransition(
            f"workflow phase cannot move from {current.value} to {target.value}"
        )
    return target


def _workspace_id(value: str) -> str:
    if not isinstance(value, str) or _WORKSPACE_ID.fullmatch(value) is None:
        raise WorkflowError("workspace_id must be a safe workspace identifier")
    return value


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise WorkflowError(f"{label} must be a safe identifier")
    return value


def _text(value: object, label: str, *, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkflowError(f"{label} must be non-empty")
    if len(value) > max_length or any(ord(character) < 32 for character in value):
        raise WorkflowError(f"{label} contains unsupported characters")
    return value


@dataclass(frozen=True, slots=True)
class PhaseTransition:
    """One immutable, ordered record of a workflow phase change."""

    sequence: int
    from_phase: RcaLoopPhase
    to_phase: RcaLoopPhase
    at: datetime
    intent_epoch: int
    reason: str

    def __post_init__(self) -> None:
        if self.sequence < 1:
            raise WorkflowError("phase transition sequence starts at one")
        if self.at.tzinfo is None or self.at.utcoffset() is None:
            raise WorkflowError("phase transition timestamp must be timezone-aware")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise WorkflowError("phase transition reason must be non-empty")
        object.__setattr__(self, "at", self.at.astimezone(UTC))

    def as_dict(self) -> dict[str, object]:
        """Return the audit representation of this transition."""
        return {
            "at": self.at.isoformat().replace("+00:00", "Z"),
            "fromPhase": self.from_phase.value,
            "intentEpoch": self.intent_epoch,
            "reason": self.reason,
            "sequence": self.sequence,
            "toPhase": self.to_phase.value,
        }


@dataclass(frozen=True, slots=True)
class MetricRule:
    """One pre-registered numeric success rule used by classification."""

    metric: str
    operator: str
    threshold: float
    label: str | None = None

    def __post_init__(self) -> None:
        try:
            _text(self.metric, "rule metric", max_length=64)
        except WorkflowError as error:
            raise RcaIntakeError(str(error)) from error
        object.__setattr__(self, "metric", self.metric.strip())
        if self.operator not in CRITERION_OPERATORS:
            raise RcaIntakeError(
                f"rule operator must be one of {sorted(CRITERION_OPERATORS)}"
            )
        if isinstance(self.threshold, bool) or not isinstance(
            self.threshold, int | float
        ):
            raise RcaIntakeError("rule threshold must be a finite number")
        threshold = float(self.threshold)
        if threshold != threshold or threshold in (float("inf"), float("-inf")):
            raise RcaIntakeError("rule threshold must be a finite number")
        object.__setattr__(self, "threshold", threshold)
        if self.label is not None:
            _text(self.label, "rule label", max_length=128)

    def evaluate(self, observed: float) -> bool:
        """Return whether one observed metric value satisfies the rule."""
        if self.operator == "gte":
            return observed >= self.threshold
        if self.operator == "lte":
            return observed <= self.threshold
        if self.operator == "gt":
            return observed > self.threshold
        return observed < self.threshold

    def accepts(self, observed: object | None) -> bool:
        """Return whether one metric value is a finite number for this rule."""
        return (
            observed is not None
            and not isinstance(observed, bool)
            and isinstance(observed, int | float)
            and observed == observed
            and observed not in (float("inf"), float("-inf"))
        )

    def as_dict(self) -> dict[str, object]:
        """Return the portable rule representation."""
        return {
            "label": self.label,
            "metric": self.metric,
            "operator": self.operator,
            "threshold": self.threshold,
        }


@dataclass(frozen=True, slots=True)
class RuleEvaluation:
    """The recorded outcome of one pre-registered rule against the metrics."""

    metric: str
    operator: str
    threshold: float
    observed: float | None
    passed: bool | None

    def __post_init__(self) -> None:
        MetricRule(self.metric, self.operator, self.threshold)
        if self.observed is not None:
            if isinstance(self.observed, bool) or not isinstance(
                self.observed, int | float
            ):
                raise ClassificationError("rule observed value must be numeric")
        if self.passed is not None and self.observed is None:
            raise ClassificationError("rule outcome requires an observed value")

    def as_dict(self) -> dict[str, object]:
        """Return the portable evaluation representation."""
        return {
            "metric": self.metric,
            "observed": self.observed,
            "operator": self.operator,
            "passed": self.passed,
            "threshold": self.threshold,
        }


@dataclass(frozen=True, slots=True)
class RunSignals:
    """The deterministic signals extracted from one recorded run."""

    run_state: str | None
    succeeded: bool | None
    manifest_present: bool
    metrics_present: bool
    data_issues: tuple[str, ...]
    harness_error: str | None
    strategy_error: str | None
    regime_segments: tuple[str, ...]
    evaluations: tuple[RuleEvaluation, ...]

    def as_dict(self) -> dict[str, object]:
        """Return the portable signal representation."""
        return {
            "dataIssues": list(self.data_issues),
            "evaluations": [item.as_dict() for item in self.evaluations],
            "harnessError": self.harness_error,
            "manifestPresent": self.manifest_present,
            "metricsPresent": self.metrics_present,
            "regimeSegments": list(self.regime_segments),
            "runState": self.run_state,
            "strategyError": self.strategy_error,
            "succeeded": self.succeeded,
        }


def _string_tuple(value: object, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise ClassificationError(f"{label} must be a sequence of strings")
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ClassificationError(f"{label} must contain non-empty strings")
    return tuple(dict.fromkeys(item.strip() for item in value))


def collect_signals(
    run: BacktestRunResult | None, rules: Sequence[MetricRule]
) -> RunSignals:
    """Extract the deterministic classification signals from one run.

    Canonical metadata keys carried on the opaque run result drive the
    engine-side signals: ``dataIssues`` names blocked data checks,
    ``harnessError`` records an engine fault, ``strategyError`` records a
    strategy-code fault, and ``regimeBreakdown`` maps declared regime
    segments to their metric breakdowns.  Everything else is intrinsic to
    the run record: terminal state, manifest presence, and the recorded
    metrics evaluated against the pre-registered rules.
    """
    if not isinstance(rules, Sequence) or isinstance(rules, str) or not rules:
        raise ClassificationError("classification requires pre-registered rules")
    if run is None:
        return RunSignals(
            run_state=None,
            succeeded=None,
            manifest_present=False,
            metrics_present=False,
            data_issues=(),
            harness_error=None,
            strategy_error=None,
            regime_segments=(),
            evaluations=tuple(
                RuleEvaluation(
                    metric=rule.metric,
                    operator=rule.operator,
                    threshold=rule.threshold,
                    observed=None,
                    passed=None,
                )
                for rule in rules
            ),
        )
    if not isinstance(run, BacktestRunResult):
        raise ClassificationError("run must be a BacktestRunResult")
    metadata = run.metadata if isinstance(run.metadata, Mapping) else {}
    data_issues = _string_tuple(metadata.get("dataIssues"), "dataIssues")
    harness_error = metadata.get("harnessError")
    strategy_error = metadata.get("strategyError")
    if harness_error is not None and not isinstance(harness_error, str):
        raise ClassificationError("harnessError must be a string when recorded")
    if strategy_error is not None and not isinstance(strategy_error, str):
        raise ClassificationError("strategyError must be a string when recorded")
    breakdown = metadata.get("regimeBreakdown")
    if breakdown is not None and not isinstance(breakdown, Mapping):
        raise ClassificationError("regimeBreakdown must be a mapping")
    regime_segments = (
        () if breakdown is None else _string_tuple(tuple(breakdown), "regimeBreakdown")
    )
    metrics = run.metrics if isinstance(run.metrics, Mapping) else None
    evaluations: list[RuleEvaluation] = []
    for rule in rules:
        observed = metrics.get(rule.metric) if metrics is not None else None
        if (
            observed is not None
            and not isinstance(observed, bool)
            and isinstance(observed, int | float)
            and observed == observed
            and observed not in (float("inf"), float("-inf"))
        ):
            value = float(observed)
            evaluations.append(
                RuleEvaluation(
                    metric=rule.metric,
                    operator=rule.operator,
                    threshold=rule.threshold,
                    observed=value,
                    passed=rule.evaluate(value),
                )
            )
        else:
            evaluations.append(
                RuleEvaluation(
                    metric=rule.metric,
                    operator=rule.operator,
                    threshold=rule.threshold,
                    observed=None,
                    passed=None,
                )
            )
    return RunSignals(
        run_state=run.state,
        succeeded=run.succeeded,
        manifest_present=(
            isinstance(run.manifest, Mapping)
            and isinstance(run.manifest.get("manifestId"), str)
            and isinstance(run.manifest.get("sha256"), str)
        ),
        metrics_present=bool(metrics),
        data_issues=data_issues,
        harness_error=harness_error,
        strategy_error=strategy_error,
        regime_segments=regime_segments,
        evaluations=tuple(evaluations),
    )


@dataclass(frozen=True, slots=True)
class AlternativeExplanation:
    """One recorded competing explanation with its own confidence."""

    failure_class: FailureClass
    confidence: float
    rationale: str

    def __post_init__(self) -> None:
        if not isinstance(self.failure_class, FailureClass):
            raise ClassificationError("alternatives must carry a FailureClass")
        if not 0.0 <= self.confidence <= 1.0:
            raise ClassificationError("alternative confidence must be within [0, 1]")
        if not isinstance(self.rationale, str) or not self.rationale.strip():
            raise ClassificationError("alternative rationale must be non-empty")

    def as_dict(self) -> dict[str, object]:
        """Return the portable alternative representation."""
        return {
            "confidence": self.confidence,
            "failureClass": self.failure_class.value,
            "rationale": self.rationale,
        }


@dataclass(frozen=True, slots=True)
class RcaFinding:
    """One deterministic, evidenced classification of a recorded run.

    A ``failure_class`` of ``None`` records a passing run.  Failed runs
    carry the primary class, its confidence, the recorded supporting and
    disconfirming evidence, competing explanations with lower confidence,
    and the discriminating follow-up tests that would separate the
    explanations.
    """

    rule: str
    rationale: str
    signals: RunSignals
    failure_class: FailureClass | None = None
    confidence: float | None = None
    supporting: tuple[str, ...] = ()
    disconfirming: tuple[str, ...] = ()
    alternatives: tuple[AlternativeExplanation, ...] = ()
    follow_ups: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.rule, str) or not self.rule.strip():
            raise ClassificationError("finding rule must be non-empty")
        if not isinstance(self.rationale, str) or not self.rationale.strip():
            raise ClassificationError("finding rationale must be non-empty")
        if not isinstance(self.signals, RunSignals):
            raise ClassificationError("findings must record their signals")
        if self.failure_class is None:
            if self.confidence is not None:
                raise ClassificationError("passing findings record no confidence")
        else:
            if not isinstance(self.failure_class, FailureClass):
                raise ClassificationError(
                    "failure_class must be a FailureClass or None"
                )
            if self.confidence is None or not 0.0 <= self.confidence <= 1.0:
                raise ClassificationError(
                    "failed findings record confidence within [0, 1]"
                )
        for name in ("supporting", "disconfirming", "follow_ups"):
            values = getattr(self, name)
            if isinstance(values, str) or not isinstance(values, Sequence):
                raise ClassificationError(f"{name} must be a sequence of strings")
            for item in values:
                if not isinstance(item, str) or not item.strip():
                    raise ClassificationError(f"{name} must contain strings")
            object.__setattr__(self, name, tuple(dict.fromkeys(values)))
        for alternative in self.alternatives:
            if not isinstance(alternative, AlternativeExplanation):
                raise ClassificationError("alternatives must be recorded outcomes")
            if alternative.failure_class is self.failure_class:
                raise ClassificationError(
                    "alternatives must differ from the primary class"
                )

    @property
    def failed(self) -> bool:
        """Return whether the finding records a failure class."""
        return self.failure_class is not None

    @property
    def revisable(self) -> bool:
        """Return whether the taxonomy allows a strategy revision to proceed."""
        return self.failure_class in _REVISABLE

    def as_dict(self) -> dict[str, object]:
        """Return the portable finding representation."""
        return {
            "alternatives": [item.as_dict() for item in self.alternatives],
            "confidence": self.confidence,
            "disconfirming": list(self.disconfirming),
            "failureClass": (
                None if self.failure_class is None else self.failure_class.value
            ),
            "followUps": list(self.follow_ups),
            "rationale": self.rationale,
            "rule": self.rule,
            "signals": self.signals.as_dict(),
            "supporting": list(self.supporting),
        }


_FOLLOW_UPS: Mapping[FailureClass, str] = MappingProxyType(
    {
        FailureClass.DATA: (
            "re-validate the recorded window with corrected data feeds and "
            "re-run the recorded readiness checks before any revision"
        ),
        FailureClass.HARNESS: (
            "re-run the unchanged bundle on a clean harness worker and "
            "compare the recorded manifest digests"
        ),
        FailureClass.IMPLEMENTATION: (
            "replay the recorded bundle under the unit-test harness and "
            "reproduce the recorded exception before touching the premise"
        ),
        FailureClass.STRATEGY: (
            "re-test the unchanged premise out-of-sample with purged "
            "partitions under the same experiment contract"
        ),
        FailureClass.MARKET_REGIME: (
            "re-estimate the pre-registered rules per recorded regime "
            "segment and test premise robustness across segments"
        ),
    }
)


def classify_run(
    run: BacktestRunResult | None, rules: Sequence[MetricRule]
) -> RcaFinding:
    """Classify one recorded run through the deterministic rule precedence.

    Precedence (first match wins): a strategy fault recorded on the run is
    an implementation defect; recorded data issues are data defects; a
    recorded harness fault is a harness defect; a run that never reached a
    successful terminal state, or succeeded without usable metrics, is a
    harness defect; a run whose pre-registered rules cannot be evaluated
    from its metrics is a harness defect; a run whose rules all pass is
    healthy; otherwise the premise failed, as a market-regime failure when
    a regime breakdown was recorded and as a strategy failure otherwise.
    """
    signals = collect_signals(run, rules)
    if run is None:
        return RcaFinding(
            rule="no-run-recorded",
            rationale="no harness run was recorded for classification",
            signals=signals,
            failure_class=FailureClass.HARNESS,
            confidence=0.9,
            supporting=("runState:absent",),
            disconfirming=(),
            alternatives=(
                AlternativeExplanation(
                    failure_class=FailureClass.DATA,
                    confidence=0.15,
                    rationale=(
                        "a data outage could have prevented the run from being recorded"
                    ),
                ),
            ),
            follow_ups=(_FOLLOW_UPS[FailureClass.HARNESS],),
        )
    if signals.strategy_error is not None:
        return RcaFinding(
            rule="strategy-error-recorded",
            rationale="the run recorded a strategy-code fault",
            signals=signals,
            failure_class=FailureClass.IMPLEMENTATION,
            confidence=0.85,
            supporting=("strategyError",),
            disconfirming=("runSucceeded",) if signals.succeeded else (),
            alternatives=(
                AlternativeExplanation(
                    failure_class=FailureClass.HARNESS,
                    confidence=0.25,
                    rationale=("an engine fault can surface as a strategy exception"),
                ),
            ),
            follow_ups=(_FOLLOW_UPS[FailureClass.IMPLEMENTATION],),
        )
    if signals.data_issues:
        return RcaFinding(
            rule="data-issues-recorded",
            rationale="the run recorded blocking data issues",
            signals=signals,
            failure_class=FailureClass.DATA,
            confidence=0.85,
            supporting=("dataIssues",),
            disconfirming=("runSucceeded",) if signals.succeeded else (),
            alternatives=(
                AlternativeExplanation(
                    failure_class=FailureClass.HARNESS,
                    confidence=0.2,
                    rationale=(
                        "the engine may have failed to load otherwise valid data"
                    ),
                ),
            ),
            follow_ups=(_FOLLOW_UPS[FailureClass.DATA],),
        )
    if signals.harness_error is not None:
        return RcaFinding(
            rule="harness-error-recorded",
            rationale="the run recorded a harness fault",
            signals=signals,
            failure_class=FailureClass.HARNESS,
            confidence=0.85,
            supporting=("harnessError",),
            disconfirming=("runSucceeded",) if signals.succeeded else (),
            alternatives=(),
            follow_ups=(_FOLLOW_UPS[FailureClass.HARNESS],),
        )
    if not signals.succeeded:
        return RcaFinding(
            rule="run-not-terminal-success",
            rationale="the run never reached a successful terminal state",
            signals=signals,
            failure_class=FailureClass.HARNESS,
            confidence=0.8,
            supporting=("runState",),
            disconfirming=(),
            alternatives=(),
            follow_ups=(_FOLLOW_UPS[FailureClass.HARNESS],),
        )
    if not signals.metrics_present:
        return RcaFinding(
            rule="metrics-missing",
            rationale="the run succeeded without recording usable metrics",
            signals=signals,
            failure_class=FailureClass.HARNESS,
            confidence=0.8,
            supporting=("metricsPresent:absent",),
            disconfirming=("runSucceeded",),
            alternatives=(),
            follow_ups=(_FOLLOW_UPS[FailureClass.HARNESS],),
        )
    evaluations = signals.evaluations
    if any(evaluation.passed is None for evaluation in evaluations):
        return RcaFinding(
            rule="metrics-incomplete",
            rationale=(
                "the recorded metrics cannot evaluate every pre-registered rule"
            ),
            signals=signals,
            failure_class=FailureClass.HARNESS,
            confidence=0.75,
            supporting=("metricsIncomplete",),
            disconfirming=("runSucceeded",),
            alternatives=(
                AlternativeExplanation(
                    failure_class=FailureClass.DATA,
                    confidence=0.2,
                    rationale=(
                        "partial data coverage can leave recorded metrics incomplete"
                    ),
                ),
            ),
            follow_ups=(_FOLLOW_UPS[FailureClass.HARNESS],),
        )
    if all(evaluation.passed for evaluation in evaluations):
        return RcaFinding(
            rule="rules-passed",
            rationale="every pre-registered rule passed against the metrics",
            signals=signals,
            failure_class=None,
            supporting=(),
            disconfirming=(),
            alternatives=(),
            follow_ups=(),
        )
    if signals.regime_segments:
        return RcaFinding(
            rule="regime-breakdown-recorded",
            rationale=(
                "the pre-registered rules failed and the run recorded a "
                "regime breakdown"
            ),
            signals=signals,
            failure_class=FailureClass.MARKET_REGIME,
            confidence=0.8,
            supporting=("criteriaFailed", "regimeBreakdown"),
            disconfirming=(),
            alternatives=(
                AlternativeExplanation(
                    failure_class=FailureClass.STRATEGY,
                    confidence=0.35,
                    rationale=("the premise may fail uniformly rather than per regime"),
                ),
            ),
            follow_ups=(_FOLLOW_UPS[FailureClass.MARKET_REGIME],),
        )
    return RcaFinding(
        rule="rules-failed",
        rationale="the pre-registered rules failed against the metrics",
        signals=signals,
        failure_class=FailureClass.STRATEGY,
        confidence=0.75,
        supporting=("criteriaFailed",),
        disconfirming=("regimeBreakdown:absent",),
        alternatives=(
            AlternativeExplanation(
                failure_class=FailureClass.MARKET_REGIME,
                confidence=0.3,
                rationale=(
                    "a regime breakdown could explain the failure but none was recorded"
                ),
            ),
        ),
        follow_ups=(_FOLLOW_UPS[FailureClass.STRATEGY],),
    )


@dataclass(frozen=True, slots=True)
class RevisionProposal:
    """One material adaptation proposed by the injected revision boundary."""

    bundle: StrategyBundle
    contract: ExperimentContract
    claim_id: str
    rationale: str
    supersedes_claim_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.bundle, StrategyBundle):
            raise RevisionProposalError("proposals require a StrategyBundle")
        if not isinstance(self.contract, ExperimentContract):
            raise RevisionProposalError("proposals require an ExperimentContract")
        _identifier(self.claim_id, "claim_id")
        _text(self.rationale, "proposal rationale", max_length=4_096)
        if self.supersedes_claim_id is not None:
            _identifier(self.supersedes_claim_id, "supersedes_claim_id")
            if self.supersedes_claim_id == self.claim_id:
                raise RevisionProposalError("a claim cannot supersede itself")


class RevisionProposer(Protocol):
    """Injected boundary that turns a finding into a material adaptation."""

    def propose(
        self,
        finding: RcaFinding,
        run: BacktestRunResult | None,
        attempt: int,
    ) -> RevisionProposal:
        """Propose the next revision, or raise to stop the loop."""


class RcaLoopDisposition(StrEnum):
    """The terminal disposition of one bounded revision loop."""

    PASSED = "passed"
    BLOCKED = "blocked"
    EXHAUSTED = "exhausted"


@dataclass(frozen=True, slots=True)
class RcaRevisionLoopRequest:
    """The chat request context that triggers one revision-loop execution."""

    workspace_id: str
    chat_id: str
    execution_id: str
    trigger_message_id: str
    requested_by: str
    objective: str
    claim_id: str
    initial_bundle: StrategyBundle
    initial_contract: ExperimentContract
    rules: tuple[MetricRule, ...]
    max_iterations: int = DEFAULT_MAX_ITERATIONS
    initial_run: BacktestRunResult | None = None
    intent_epoch: int = 0
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        _workspace_id(self.workspace_id)
        for name in ("chat_id", "execution_id", "trigger_message_id", "requested_by"):
            _identifier(getattr(self, name), name)
        _text(self.objective, "objective", max_length=8_192)
        _identifier(self.claim_id, "claim_id")
        if not isinstance(self.initial_bundle, StrategyBundle):
            raise RcaIntakeError("initial_bundle must be a StrategyBundle")
        if not isinstance(self.initial_contract, ExperimentContract):
            raise RcaIntakeError("initial_contract must be an ExperimentContract")
        if isinstance(self.rules, str) or not isinstance(self.rules, Sequence):
            raise RcaIntakeError("rules must be a sequence of MetricRule values")
        rules = tuple(self.rules)
        if not rules or any(not isinstance(rule, MetricRule) for rule in rules):
            raise RcaIntakeError("rules must contain at least one MetricRule")
        object.__setattr__(self, "rules", rules)
        if (
            isinstance(self.max_iterations, bool)
            or not isinstance(self.max_iterations, int)
            or self.max_iterations < 1
        ):
            raise RcaIntakeError("max_iterations must be a positive integer")
        if self.initial_run is not None and not isinstance(
            self.initial_run, BacktestRunResult
        ):
            raise RcaIntakeError("initial_run must be a BacktestRunResult when set")
        if self.intent_epoch < 0:
            raise RcaIntakeError("intent_epoch must not be negative")
        if self.created_at is not None:
            if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
                raise RcaIntakeError("created_at must be timezone-aware")
            object.__setattr__(self, "created_at", self.created_at.astimezone(UTC))

    def as_dict(self) -> dict[str, object]:
        """Return the detached request representation for transport."""
        created_at = (
            None
            if self.created_at is None
            else self.created_at.isoformat().replace("+00:00", "Z")
        )
        return {
            "chatId": self.chat_id,
            "claimId": self.claim_id,
            "createdAt": created_at,
            "executionId": self.execution_id,
            "initialRun": (
                None if self.initial_run is None else self.initial_run.as_dict()
            ),
            "intentEpoch": self.intent_epoch,
            "maxIterations": self.max_iterations,
            "objective": self.objective,
            "requestedBy": self.requested_by,
            "rules": [rule.as_dict() for rule in self.rules],
            "triggerMessageId": self.trigger_message_id,
            "workspaceId": self.workspace_id,
        }


@dataclass(frozen=True, slots=True)
class RevisionCycle:
    """One immutable, ordered record of one bounded revision cycle."""

    attempt: int
    revision: int
    finding: RcaFinding
    run: BacktestRunResult | None
    node_id: str | None
    contract_transition: str | None
    proposal_rationale: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.attempt, bool) or self.attempt < 1:
            raise WorkflowError("revision cycle attempt starts at one")
        if isinstance(self.revision, bool) or self.revision < 0:
            raise WorkflowError("revision cycle revision must not be negative")
        if not isinstance(self.finding, RcaFinding):
            raise WorkflowError("revision cycles record a classification finding")
        if self.run is not None and not isinstance(self.run, BacktestRunResult):
            raise WorkflowError("revision cycles record a BacktestRunResult")
        if self.node_id is not None:
            _identifier(self.node_id, "lineage node_id")
        if self.proposal_rationale is not None and not (
            isinstance(self.proposal_rationale, str) and self.proposal_rationale.strip()
        ):
            raise WorkflowError("proposal rationale must be non-empty when set")

    def as_dict(self) -> dict[str, object]:
        """Return the portable cycle representation."""
        return {
            "attempt": self.attempt,
            "contractTransition": self.contract_transition,
            "finding": self.finding.as_dict(),
            "lineageNodeId": self.node_id,
            "proposalRationale": self.proposal_rationale,
            "revision": self.revision,
            "run": None if self.run is None else self.run.as_dict(),
        }


def _run_identity(run: BacktestRunResult | None) -> RunIdentity:
    if run is None:
        raise WorkflowError("lineage records require a recorded run")
    manifest = run.manifest if isinstance(run.manifest, Mapping) else None
    manifest_id = manifest.get("manifestId") if manifest is not None else None
    manifest_sha256 = manifest.get("sha256") if manifest is not None else None
    return RunIdentity(
        run_id=run.job_id,
        manifest_id=manifest_id if isinstance(manifest_id, str) else None,
        manifest_sha256=(
            manifest_sha256
            if isinstance(manifest_sha256, str) and _DIGEST.fullmatch(manifest_sha256)
            else None
        ),
    )


@dataclass(frozen=True, slots=True)
class RcaLoopReport:
    """The complete, labeled record of one bounded revision loop."""

    workspace_id: str
    execution_id: str
    claim_id: str
    objective: str
    disposition: RcaLoopDisposition
    max_iterations: int
    iterations_used: int
    lineage_size: int
    strategy_id: str
    strategy_version: str
    contract_sha256: str
    cycles: tuple[RevisionCycle, ...]
    final_finding: RcaFinding
    schema: str = _REPORT_SCHEMA
    historical: bool = True
    limitations: tuple[str, ...] = (EXPLORATORY_DISCLOSURE,)
    journal_id: str | None = None

    def __post_init__(self) -> None:
        _workspace_id(self.workspace_id)
        _identifier(self.execution_id, "execution_id")
        _identifier(self.claim_id, "claim_id")
        _text(self.objective, "objective", max_length=8_192)
        if not isinstance(self.disposition, RcaLoopDisposition):
            raise WorkflowError("reports record an RcaLoopDisposition")
        if (
            isinstance(self.max_iterations, bool)
            or not isinstance(self.max_iterations, int)
            or self.max_iterations < 1
        ):
            raise WorkflowError("reports record a positive max_iterations")
        if (
            isinstance(self.iterations_used, bool)
            or not isinstance(self.iterations_used, int)
            or self.iterations_used < 0
            or self.iterations_used > self.max_iterations
        ):
            raise WorkflowError("iterations_used must fall within the budget")
        if self.lineage_size < 1:
            raise WorkflowError("reports require at least one lineage node")
        _identifier(self.strategy_id, "strategy_id")
        _text(self.strategy_version, "strategy version", max_length=64)
        if (
            not isinstance(self.contract_sha256, str)
            or _DIGEST.fullmatch(self.contract_sha256) is None
        ):
            raise WorkflowError("contract_sha256 must be a lowercase SHA-256 digest")
        if not self.cycles or any(
            not isinstance(cycle, RevisionCycle) for cycle in self.cycles
        ):
            raise WorkflowError("reports require recorded revision cycles")
        if not isinstance(self.final_finding, RcaFinding):
            raise WorkflowError("reports require a final finding")
        if self.schema != _REPORT_SCHEMA:
            raise WorkflowError(f"reports must carry the {_REPORT_SCHEMA!r} schema")
        if self.historical is not True:
            raise WorkflowError("Phase 1 evidence must be labeled as history")
        if isinstance(self.limitations, str) or not isinstance(
            self.limitations, Sequence
        ):
            raise WorkflowError("limitations must be a sequence of strings")
        for value in self.limitations:
            if not isinstance(value, str) or not value.strip():
                raise WorkflowError("limitations must contain non-empty strings")
        limitations = tuple(dict.fromkeys(self.limitations))
        if EXPLORATORY_DISCLOSURE not in limitations:
            limitations = (EXPLORATORY_DISCLOSURE, *limitations)
        object.__setattr__(self, "limitations", limitations)

    def as_dict(self) -> dict[str, object]:
        """Return the portable report representation for transport and Git."""
        return {
            "claimId": self.claim_id,
            "contractSha256": self.contract_sha256,
            "cycles": [cycle.as_dict() for cycle in self.cycles],
            "disposition": self.disposition.value,
            "executionId": self.execution_id,
            "finalFinding": self.final_finding.as_dict(),
            "historical": self.historical,
            "iterationsUsed": self.iterations_used,
            "journalId": self.journal_id,
            "limitations": list(self.limitations),
            "lineageSize": self.lineage_size,
            "maxIterations": self.max_iterations,
            "objective": self.objective,
            "schema": self.schema,
            "strategyId": self.strategy_id,
            "strategyVersion": self.strategy_version,
            "workspaceId": self.workspace_id,
        }

    def to_bytes(self) -> bytes:
        """Return deterministic report bytes suitable for Git publication."""
        return (
            json.dumps(
                self.as_dict(),
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8")

    @property
    def content_sha256(self) -> str:
        """Return the content digest of the serialized report."""
        return hashlib.sha256(self.to_bytes()).hexdigest()


def build_rca_loop_report(
    *,
    workspace_id: str,
    execution_id: str,
    claim_id: str,
    objective: str,
    disposition: RcaLoopDisposition,
    max_iterations: int,
    iterations_used: int,
    lineage: RevisionLineage,
    bundle: StrategyBundle,
    contract: ExperimentContract,
    cycles: Sequence[RevisionCycle],
    final_finding: RcaFinding,
    journal_id: str | None = None,
) -> RcaLoopReport:
    """Assemble the labeled loop report from recorded phase outputs."""
    return RcaLoopReport(
        workspace_id=workspace_id,
        execution_id=execution_id,
        claim_id=claim_id,
        objective=objective,
        disposition=disposition,
        max_iterations=max_iterations,
        iterations_used=iterations_used,
        lineage_size=len(lineage),
        strategy_id=bundle.strategy_id,
        strategy_version=bundle.version,
        contract_sha256=contract.contract_sha256,
        cycles=tuple(cycles),
        final_finding=final_finding,
        journal_id=journal_id,
    )


@dataclass(frozen=True, slots=True)
class RcaRevisionLoopState:
    """The immutable state snapshot after each workflow transition."""

    workspace_id: str
    execution_id: str
    phase: RcaLoopPhase
    intent_epoch: int
    transitions: tuple[PhaseTransition, ...] = ()
    bundle: StrategyBundle | None = None
    contract: ExperimentContract | None = None
    claim_id: str | None = None
    run: BacktestRunResult | None = None
    finding: RcaFinding | None = None
    cycles: tuple[RevisionCycle, ...] = ()
    revisions_used: int = 0
    disposition: RcaLoopDisposition | None = None
    pending_rationale: str | None = None
    pending_supersedes: str | None = None
    report: RcaLoopReport | None = None
    journal_id: str | None = None
    error_message: str | None = None
    detail: str | None = None
    fenced: bool = False


@dataclass(frozen=True, slots=True)
class RcaRevisionLoopOutcome:
    """The detached result of one workflow run, safe for UI transport."""

    workspace_id: str
    execution_id: str
    phase: RcaLoopPhase
    fenced: bool
    transitions: tuple[PhaseTransition, ...]
    disposition: RcaLoopDisposition | None = None
    cycles: tuple[RevisionCycle, ...] = ()
    revisions_used: int = 0
    finding: RcaFinding | None = None
    report: RcaLoopReport | None = None
    journal_id: str | None = None
    detail: str | None = None

    def as_dict(self) -> dict[str, object]:
        """Return the transport representation of the outcome."""
        return {
            "cycles": [cycle.as_dict() for cycle in self.cycles],
            "detail": self.detail,
            "disposition": (
                None if self.disposition is None else self.disposition.value
            ),
            "executionId": self.execution_id,
            "fenced": self.fenced,
            "finding": None if self.finding is None else self.finding.as_dict(),
            "journalId": self.journal_id,
            "phase": self.phase.value,
            "report": None if self.report is None else self.report.as_dict(),
            "revisionsUsed": self.revisions_used,
            "transitions": [item.as_dict() for item in self.transitions],
            "workspaceId": self.workspace_id,
        }


class RcaRevisionLoopWorkflow:
    """Orchestrate the documented classify-and-revise research loop."""

    def __init__(
        self,
        *,
        chat: ChatService,
        runner: BacktestRunner,
        proposer: RevisionProposer,
        lineage: RevisionLineage,
        journal: CommandJournal | None = None,
        clock: Callable[[], datetime] = _utc_now,
        id_factory: Callable[[str], str] = _new_id,
    ) -> None:
        self._chat = chat
        self._runner = runner
        self._proposer = proposer
        self._lineage = lineage
        self._journal = journal
        self._clock = clock
        self._id_factory = id_factory

    @property
    def lineage(self) -> RevisionLineage:
        """Return the append-only revision lineage this workflow appends to."""
        return self._lineage

    @property
    def definition(self) -> WorkflowDefinition:
        """Return the versioned workflow definition registered in chat."""
        return RCA_REVISION_LOOP_WORKFLOW

    def launch(
        self,
        workspace_id: str,
        chat_id: str,
        *,
        actor_id: str,
        objective: str,
        investigation_id: str | None = None,
    ) -> WorkflowExecution:
        """Persist the chat trigger and return the durable execution."""
        return self._chat.launch_workflow(
            workspace_id,
            chat_id,
            workflow=self.definition,
            actor_id=actor_id,
            body=objective,
            investigation_id=investigation_id,
        )

    def steer(
        self,
        workspace_id: str,
        execution_id: str,
        *,
        actor_id: str,
        body: str,
        message_id: str | None = None,
    ) -> WorkflowExecution:
        """Record immediate, non-destructive steering on the execution."""
        return self._chat.steer_workflow(
            workspace_id,
            execution_id,
            actor_id=actor_id,
            body=body,
            message_id=message_id,
        )

    def observe(
        self,
        workspace_id: str,
        execution_id: str,
        *,
        after_cursor: int = 0,
        limit: int | None = None,
    ) -> EventReplay:
        """Return an ordered replay page for reconnecting clients."""
        return self._chat.replay_events(
            workspace_id,
            execution_id,
            after_cursor=after_cursor,
            limit=limit,
        )

    def run(self, request: RcaRevisionLoopRequest) -> RcaRevisionLoopOutcome:
        """Execute the documented phases once for this request snapshot."""
        state = RcaRevisionLoopState(
            workspace_id=request.workspace_id,
            execution_id=request.execution_id,
            phase=_REQUESTED,
            intent_epoch=request.intent_epoch,
            bundle=request.initial_bundle,
            contract=request.initial_contract,
            claim_id=request.claim_id,
            run=request.initial_run,
        )
        try:
            self._require_execution(request)
            self._check_stale_request(request)
            self._chat.start_workflow(
                request.workspace_id,
                request.execution_id,
                actor_id=request.requested_by,
            )
            state = self._advance(state, _ANALYZING, "chat request accepted")
            while True:
                if state.run is None:
                    baseline = self._backtest(
                        request,
                        state,
                        bundle=self._current_bundle(state),
                        correlation_label="baseline",
                    )
                    state = replace(state, run=baseline)
                state = self._advance(state, _CLASSIFYING, "harness run recorded")
                finding = classify_run(state.run, request.rules)
                state = replace(state, finding=finding)
                state = self._advance(
                    state,
                    _RECORDING,
                    (
                        "run passed the pre-registered rules"
                        if finding.failure_class is None
                        else f"run classified as {finding.failure_class.value}"
                    ),
                )
                state = self._record_lineage_node(request, state, finding)
                if finding.failure_class is None:
                    state = self._complete(
                        request,
                        state,
                        RcaLoopDisposition.PASSED,
                        "loop recorded a passing run",
                    )
                    break
                if not finding.revisable:
                    state = self._complete(
                        request,
                        state,
                        RcaLoopDisposition.BLOCKED,
                        f"{finding.failure_class.value} defects are not "
                        "strategy-revisable",
                    )
                    break
                if state.revisions_used >= request.max_iterations:
                    state = self._complete(
                        request,
                        state,
                        RcaLoopDisposition.EXHAUSTED,
                        "revision budget exhausted",
                    )
                    break
                state = self._advance(state, _REVISING, "revision budget available")
                self._require_current(request, state)
                proposal = self._propose(state, finding)
                revised_run = self._backtest(
                    request,
                    state,
                    bundle=proposal.bundle,
                    correlation_label=f"rev{state.revisions_used + 1}",
                )
                self._require_current(request, state)
                state = replace(
                    state,
                    bundle=proposal.bundle,
                    contract=proposal.contract,
                    claim_id=proposal.claim_id,
                    run=revised_run,
                    revisions_used=state.revisions_used + 1,
                    finding=None,
                    pending_rationale=proposal.rationale,
                    pending_supersedes=proposal.supersedes_claim_id,
                )
                state = self._advance(state, _ANALYZING, "material adaptation recorded")
            self._finish_execution(
                request,
                WorkflowExecutionState.COMPLETED,
                ExecutionEventType.WORKFLOW_COMPLETED,
                payload=self._completion_payload(state),
            )
        except _Fenced as fence:
            state = self._pause(
                fence.state if fence.state is not None else state, fence
            )
        except _PhaseFailure as failure:
            state = self._fail(request, failure.state, failure)
        except (
            BacktestRunError,
            ClassificationError,
            HarnessError,
            LineageError,
            RcaIntakeError,
            RevisionProposalError,
            WorkflowError,
        ) as error:
            state = self._fail(request, state, error)
        return self._outcome(state)

    # -- phases ------------------------------------------------------------

    def _current_bundle(self, state: RcaRevisionLoopState) -> StrategyBundle:
        if state.bundle is None:
            raise WorkflowError("the loop requires an initial strategy bundle")
        return state.bundle

    def _propose(
        self, state: RcaRevisionLoopState, finding: RcaFinding
    ) -> RevisionProposal:
        attempt = state.revisions_used + 1
        try:
            return self._proposer.propose(finding, state.run, attempt)
        except (
            BacktestRunError,
            ClassificationError,
            LineageError,
            RevisionProposalError,
            WorkflowError,
        ):
            raise
        except Exception as error:
            raise RevisionProposalError(f"revision proposer failed: {error}") from error

    def _backtest(
        self,
        request: RcaRevisionLoopRequest,
        state: RcaRevisionLoopState,
        *,
        bundle: StrategyBundle,
        correlation_label: str,
    ) -> BacktestRunResult:
        self._require_current(request, state)
        correlation_id = (
            f"{_TOOL_CORRELATION_PREFIX}-{request.execution_id}-{correlation_label}"
        )
        experiment_id = request.initial_contract.experiment_id
        job = self._chat.submit_tool_job(
            request.workspace_id,
            request.execution_id,
            tool_name=_TOOL_NAME,
            actor_id=_WORKFLOW_ACTOR,
            correlation_id=correlation_id,
            strategy_ids=(bundle.strategy_id,),
            experiment_ids=(experiment_id,),
        )
        self._chat.start_tool_job(
            request.workspace_id, job.tool_job_id, actor_id=_WORKFLOW_ACTOR
        )
        result = self._runner.run(
            BacktestRunRequest(
                run_id=job.correlation_id,
                bundle=bundle,
                workflow_execution_id=request.execution_id,
                experiment_id=experiment_id,
            )
        )
        self._chat.record_tool_result(
            request.workspace_id,
            job.tool_job_id,
            result=result.as_dict(),
            actor_id=_WORKFLOW_ACTOR,
            success=result.succeeded,
            error_message=None if result.succeeded else f"state {result.state!r}",
            intent_epoch=state.intent_epoch,
        )
        try:
            self._require_current(request, state)
        except _Fenced as fence:
            raise _Fenced(str(fence), state) from fence
        return result

    def _record_lineage_node(
        self,
        request: RcaRevisionLoopRequest,
        state: RcaRevisionLoopState,
        finding: RcaFinding,
    ) -> RcaRevisionLoopState:
        parent = self._last_node()
        is_revision = parent is not None
        revision = 0 if parent is None else parent.revision + 1
        node_id = f"{request.execution_id}-r{revision}"
        bundle = self._current_bundle(state)
        contract = state.contract
        if contract is None:
            raise WorkflowError("lineage records require the active contract")
        outcome = (
            RevisionOutcome.PASSED
            if finding.failure_class is None
            else RevisionOutcome.FAILED
        )
        previous_contract = parent.contract_sha256 if is_revision else None
        contract_sha256 = contract.contract_sha256
        if is_revision and previous_contract == contract_sha256:
            raise WorkflowError(
                "a material adaptation must record a new experiment contract"
            )
        supersedes = state.pending_supersedes
        if is_revision and state.claim_id == parent.claim_id:
            supersedes = None
        if (
            is_revision
            and parent is not None
            and parent.outcome is RevisionOutcome.FAILED
            and outcome is RevisionOutcome.PASSED
            and state.claim_id != parent.claim_id
            and supersedes is None
        ):
            supersedes = parent.claim_id
        rationale = (
            finding.rationale
            if not is_revision
            else (state.pending_rationale or finding.rationale)
        )
        node = RevisionNode(
            node_id=node_id,
            revision=revision,
            run=_run_identity(state.run),
            strategy_id=bundle.strategy_id,
            strategy_version=bundle.version,
            strategy_lineage_version=f"{bundle.strategy_id}-v{revision}",
            experiment_id=request.initial_contract.experiment_id,
            outcome=outcome,
            claim_id=state.claim_id or request.claim_id,
            contract_sha256=contract_sha256,
            rationale=rationale,
            created_at=self._clock(),
            parent_node_id=None if parent is None else parent.node_id,
            parent_node_sha256=None if parent is None else parent.content_sha256,
            previous_contract_sha256=previous_contract,
            failure_class=(
                None if finding.failure_class is None else finding.failure_class.value
            ),
            confidence=finding.confidence,
            supersedes_claim_id=supersedes,
            detail={
                "classificationRule": finding.rule,
                "classificationEvidence": finding.as_dict(),
            },
        )
        self._lineage.append(node)
        cycle = RevisionCycle(
            attempt=len(state.cycles) + 1,
            revision=revision,
            finding=finding,
            run=state.run,
            node_id=node.node_id,
            contract_transition=node.contract_transition,
            proposal_rationale=state.pending_rationale if is_revision else None,
        )
        return replace(
            state,
            cycles=(*state.cycles, cycle),
            pending_rationale=None,
            pending_supersedes=None,
        )

    def _last_node(self) -> RevisionNode | None:
        return self._lineage.nodes[-1] if len(self._lineage) else None

    def _complete(
        self,
        request: RcaRevisionLoopRequest,
        state: RcaRevisionLoopState,
        disposition: RcaLoopDisposition,
        reason: str,
    ) -> RcaRevisionLoopState:
        journal_id = (
            None if self._journal is None else f"workflow-{request.execution_id}"
        )
        bundle = self._current_bundle(state)
        contract = state.contract
        finding = state.finding
        if contract is None or finding is None:
            raise WorkflowError("loop completion requires a classified final run")
        report = build_rca_loop_report(
            workspace_id=request.workspace_id,
            execution_id=request.execution_id,
            claim_id=state.claim_id or request.claim_id,
            objective=request.objective,
            disposition=disposition,
            max_iterations=request.max_iterations,
            iterations_used=state.revisions_used,
            lineage=self._lineage,
            bundle=bundle,
            contract=contract,
            cycles=state.cycles,
            final_finding=finding,
            journal_id=journal_id,
        )
        if self._journal is not None:
            definition = self.definition
            self._journal.accept(
                "workflow.rca_revision_loop.completed",
                {
                    "claimId": report.claim_id,
                    "disposition": disposition.value,
                    "executionId": request.execution_id,
                    "requestedBy": request.requested_by,
                    "triggerMessageId": request.trigger_message_id,
                    "workflow": {
                        "id": definition.workflow_id,
                        "version": definition.version,
                    },
                },
                {"phase": _COMPLETED.value, "report": report.as_dict()},
                workflow_hash=content_hash(
                    {"id": definition.workflow_id, "version": definition.version}
                ),
                model_hash=content_hash({}),
                artifacts=(
                    ArtifactRef.from_bytes(
                        f"reports/{request.execution_id}/rca.json",
                        report.to_bytes(),
                        media_type=_REPORT_MEDIA_TYPE,
                    ),
                ),
                tool_correlation_ids=self._tool_correlation_ids(request, state),
                idempotency_id=journal_id,
            )
        state = replace(state, report=report, journal_id=journal_id)
        state = self._advance(state, _COMPLETED, reason)
        return replace(state, disposition=disposition)

    def _tool_correlation_ids(
        self, request: RcaRevisionLoopRequest, state: RcaRevisionLoopState
    ) -> tuple[str, ...]:
        prefix = f"{_TOOL_CORRELATION_PREFIX}-{request.execution_id}"
        return (
            f"{prefix}-baseline",
            *(f"{prefix}-rev{index}" for index in range(1, state.revisions_used + 1)),
        )

    # -- state helpers -----------------------------------------------------

    def _advance(
        self,
        state: RcaRevisionLoopState,
        target: RcaLoopPhase,
        reason: str,
    ) -> RcaRevisionLoopState:
        transition_phase(state.phase, target)
        transition = PhaseTransition(
            sequence=len(state.transitions) + 1,
            from_phase=state.phase,
            to_phase=target,
            at=self._clock(),
            intent_epoch=state.intent_epoch,
            reason=reason,
        )
        return replace(
            state, phase=target, transitions=(*state.transitions, transition)
        )

    def _pause(
        self, state: RcaRevisionLoopState, fence: _Fenced
    ) -> RcaRevisionLoopState:
        detail = str(fence)
        if state.phase in {_COMPLETED, _FAILED}:
            return replace(state, detail=detail, fenced=True)
        try:
            paused = self._advance(state, _PAUSED, detail)
        except InvalidPhaseTransition:
            return replace(state, detail=detail, fenced=True)
        return replace(paused, detail=detail, fenced=True)

    def _fail(
        self,
        request: RcaRevisionLoopRequest,
        state: RcaRevisionLoopState,
        error: Exception,
    ) -> RcaRevisionLoopState:
        self._finish_execution(
            request,
            WorkflowExecutionState.FAILED,
            ExecutionEventType.WORKFLOW_FAILED,
            payload={"errorMessage": str(error), "state": "failed"},
        )
        failed = self._advance(state, _FAILED, f"workflow failed: {error}")
        return replace(failed, error_message=str(error), detail=str(error))

    def _outcome(self, state: RcaRevisionLoopState) -> RcaRevisionLoopOutcome:
        return RcaRevisionLoopOutcome(
            workspace_id=state.workspace_id,
            execution_id=state.execution_id,
            phase=state.phase,
            fenced=state.fenced,
            transitions=state.transitions,
            disposition=state.disposition,
            cycles=state.cycles,
            revisions_used=state.revisions_used,
            finding=state.finding,
            report=state.report,
            journal_id=state.journal_id,
            detail=state.detail,
        )

    # -- durable boundaries -------------------------------------------------

    def _require_execution(self, request: RcaRevisionLoopRequest) -> WorkflowExecution:
        try:
            execution = self._chat.get_execution(
                request.workspace_id, request.execution_id
            )
        except RecordNotFound as error:
            raise WorkflowError(
                f"execution {request.execution_id!r} does not exist"
            ) from error
        if execution.chat_id != request.chat_id:
            raise WorkflowError("execution does not belong to the request chat")
        if execution.trigger_message_id != request.trigger_message_id:
            raise WorkflowError("trigger message does not match the execution")
        return execution

    def _check_stale_request(self, request: RcaRevisionLoopRequest) -> None:
        execution = self._chat.get_execution(request.workspace_id, request.execution_id)
        if execution.state in _TERMINAL_EXECUTION_STATES:
            raise WorkflowError(f"execution is already {execution.state.value}")
        if execution.intent_epoch != request.intent_epoch:
            raise _Fenced("request predates newer steering")

    def _require_current(
        self, request: RcaRevisionLoopRequest, state: RcaRevisionLoopState
    ) -> None:
        execution = self._chat.get_execution(request.workspace_id, request.execution_id)
        if execution.state in _TERMINAL_EXECUTION_STATES:
            raise _Fenced(f"execution became {execution.state.value}")
        if execution.intent_epoch != state.intent_epoch:
            raise _Fenced("steering advanced the intent epoch")

    def _completion_payload(self, state: RcaRevisionLoopState) -> dict[str, object]:
        payload: dict[str, object] = {
            "state": _COMPLETED.value,
            "revisionsUsed": state.revisions_used,
        }
        if state.disposition is not None:
            payload["disposition"] = state.disposition.value
        report = state.report
        if report is not None:
            payload["report"] = {
                "name": f"reports/{state.execution_id}/rca.json",
                "sha256": report.content_sha256,
                "schema": _REPORT_SCHEMA,
            }
        return payload

    def _finish_execution(
        self,
        request: RcaRevisionLoopRequest,
        final_state: WorkflowExecutionState,
        event_type: ExecutionEventType,
        *,
        payload: Mapping[str, object] | None = None,
    ) -> None:
        try:
            execution = self._chat.get_execution(
                request.workspace_id, request.execution_id
            )
        except RecordNotFound:
            return
        if execution.state in _TERMINAL_EXECUTION_STATES:
            return
        now = self._clock()
        updated = self._chat.persistence.save(
            replace_record(
                execution, state=final_state, finished_at=now, updated_at=now
            )
        )
        self._chat.event_store.append(
            ExecutionEvent(
                execution_id=updated.execution_id,
                workspace_id=updated.workspace_id,
                event_type=event_type,
                correlation_id=updated.execution_id,
                actor_id=_WORKFLOW_ACTOR,
                actor_scope=ActorScope.WORKFLOW,
                occurred_at=now,
                payload=dict(payload or {"state": final_state.value}),
                intent_epoch=updated.intent_epoch,
                event_id=self._id_factory("event"),
            )
        )


__all__ = [
    "CRITERION_OPERATORS",
    "DEFAULT_MAX_ITERATIONS",
    "FAILURE_CLASSES",
    "RCA_LOOP_PHASES",
    "RCA_REVISION_LOOP_WORKFLOW",
    "REVISABLE_CLASSES",
    "AlternativeExplanation",
    "ClassificationError",
    "FailureClass",
    "InvalidPhaseTransition",
    "MetricRule",
    "RcaFinding",
    "RcaIntakeError",
    "RcaLoopDisposition",
    "RcaLoopPhase",
    "RcaLoopReport",
    "RcaRevisionLoopOutcome",
    "RcaRevisionLoopRequest",
    "RcaRevisionLoopState",
    "RcaRevisionLoopWorkflow",
    "RevisionCycle",
    "RevisionProposal",
    "RevisionProposalError",
    "RevisionProposer",
    "RuleEvaluation",
    "RunSignals",
    "WorkflowError",
    "build_rca_loop_report",
    "classify_run",
    "collect_signals",
    "transition_phase",
]
