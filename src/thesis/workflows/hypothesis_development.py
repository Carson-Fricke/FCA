"""Falsification-first hypothesis-development workflow.

This workflow captures the research pipeline the product describes: a user
brings an informal theory or trade idea, the agent forms a *falsifiable*
hypothesis that states explicitly why it disagrees with current market pricing,
makes a real effort to destroy the hypothesis by planning and running
falsification trials and negative controls, diagnoses any trial that fails
through fresh knowledge-cutoff subagents, and only then designs and optimizes a
strategy.  A strategy is a falsification instrument: its declared exit criteria
are falsifier re-checks, and PnL is only a guardrail.

Design rules mirror the sibling workflows:

- Every transition goes through the explicit :func:`transition_phase` table and
  is recorded as an immutable :class:`PhaseTransition`.
- Every gate is enforced by the state machine; a failed gate *pauses* the
  workflow with a reason and never silently advances.
- No wall-clock participates in state decisions and every external effect sits
  behind an injected port (model router, backtest runner, sandbox validator,
  hypothesis store, attempt ledger, subagent factory, command journal).
- The workflow is fully orchestratable with a fake model and a fake backtest
  runner.

Phases (ordered)::

    requested -> ideation -> hypothesis -> falsification_plan
        -> falsification_trials <-> rca -> verdict -> strategy_design
        -> optimization -> completed
    plus paused / failed

Gates:

* ``hypothesis``: falsifiability.  A non-empty disagreement with market pricing
  and at least one falsifier with ``{observable, condition, threshold, test}``.
  No edge or no falsifier cannot advance: if the market prices the claim
  fairly, there is no trade.
* ``falsification_plan``: at least one falsifier test *and* at least one
  negative/placebo control.
* ``falsification_trials -> verdict``: every planned falsifier has bound
  evidence (a recorded run digest and metric).
* ``verdict``: only a not-falsified outcome (``supported`` or ``inconclusive``)
  may advance to ``strategy_design``; ``refuted`` loops back to ``hypothesis``.
* ``strategy_design -> optimization``: a sandbox-valid strategy whose declared
  exit criteria are phrased as falsifier re-checks.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol

from thesis.agent.context import assemble_context
from thesis.agent.router import InvocationResult, ModelRouter, Role
from thesis.agent.subagents import SubagentContextFactory
from thesis.chat.contracts import (
    MessageRole,
    RecordNotFound,
    TranscriptItemKind,
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
from thesis.execution.sub_agents import (
    STATUS_COMPLETED,
    STATUS_FAILED,
    SubAgentContextSpec,
    append_gate_event,
    append_sub_agent_event,
)
from thesis.experiments.ledger import AttemptKind, AttemptLedger, AttemptRecord
from thesis.persistence.journal import ArtifactRef, CommandJournal, content_hash
from thesis.research.hypothesis import (
    FALSIFIER_CONDITIONS,
    HypothesisDocument,
    HypothesisFalsifier,
    HypothesisFrontMatter,
    HypothesisLinks,
    HypothesisStatus,
    HypothesisStore,
    InMemoryHypothesisStore,
)
from thesis.strategies.bundles import StrategyBundle, StrategyBundleError
from thesis.strategies.validation import (
    StrategySandboxValidator,
    StrategyValidationError,
    validate_strategy_bundle,
)
from thesis.workflows.backtests import (
    BacktestRunError,
    BacktestRunner,
    BacktestRunRequest,
    BacktestRunResult,
    HarnessError,
)
from thesis.workflows.hypothesis_validation import (
    VERDICT_INCONCLUSIVE,
    VERDICT_REFUTED,
    VERDICT_SUPPORTED,
)

_WORKFLOW_ID = "hypothesis-development"
_WORKFLOW_VERSION = "1.0.0"
_WORKFLOW_TITLE = "Hypothesis development"
_TOOL_NAME = "harness.backtest"
_WORKFLOW_ACTOR = "thesis-workflow"
_HYPOTHESIS_MEDIA_TYPE = "text/markdown"

_VERDICTS = frozenset({VERDICT_SUPPORTED, VERDICT_REFUTED, VERDICT_INCONCLUSIVE})

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
_WORKSPACE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")

DEFAULT_MAX_RCA_CYCLES = 1


class WorkflowError(RuntimeError):
    """Base error for invalid hypothesis-development workflow operations."""


class InvalidPhaseTransition(WorkflowError):
    """A phase cannot move directly to the requested phase."""


class HypothesisDevelopmentIntakeError(WorkflowError):
    """Raised when a hypothesis-development request is not valid intake."""


class FalsificationComposeError(WorkflowError):
    """Raised when model output cannot become falsification content."""


class _Fenced(Exception):
    """Internal signal that a gate or steering event must pause the workflow."""

    def __init__(
        self,
        message: str,
        state: HypothesisDevelopmentState | None = None,
    ) -> None:
        super().__init__(message)
        self.state = state


class _PhaseFailure(WorkflowError):
    """Internal failure signal that carries the partial workflow state."""

    def __init__(self, message: str, state: HypothesisDevelopmentState) -> None:
        super().__init__(message)
        self.state = state


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise WorkflowError(f"{label} must be a safe identifier")
    return value


def _workspace_id(value: str) -> str:
    if not isinstance(value, str) or _WORKSPACE_ID.fullmatch(value) is None:
        raise WorkflowError("workspace_id must be a safe workspace identifier")
    return value


def _text(value: object, label: str, *, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkflowError(f"{label} must be non-empty")
    if len(value) > max_length or any(ord(character) < 32 for character in value):
        raise WorkflowError(f"{label} contains unsupported characters")
    return value


class HypothesisDevelopmentPhase(StrEnum):
    """The explicit, ordered phases of the hypothesis-development workflow."""

    REQUESTED = "requested"
    IDEATION = "ideation"
    HYPOTHESIS = "hypothesis"
    FALSIFICATION_PLAN = "falsification_plan"
    FALSIFICATION_TRIALS = "falsification_trials"
    RCA = "rca"
    VERDICT = "verdict"
    STRATEGY_DESIGN = "strategy_design"
    OPTIMIZATION = "optimization"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"


_REQUESTED = HypothesisDevelopmentPhase.REQUESTED
_IDEATION = HypothesisDevelopmentPhase.IDEATION
_HYPOTHESIS = HypothesisDevelopmentPhase.HYPOTHESIS
_FALSIFICATION_PLAN = HypothesisDevelopmentPhase.FALSIFICATION_PLAN
_FALSIFICATION_TRIALS = HypothesisDevelopmentPhase.FALSIFICATION_TRIALS
_RCA = HypothesisDevelopmentPhase.RCA
_VERDICT = HypothesisDevelopmentPhase.VERDICT
_STRATEGY_DESIGN = HypothesisDevelopmentPhase.STRATEGY_DESIGN
_OPTIMIZATION = HypothesisDevelopmentPhase.OPTIMIZATION
_COMPLETED = HypothesisDevelopmentPhase.COMPLETED
_FAILED = HypothesisDevelopmentPhase.FAILED
_PAUSED = HypothesisDevelopmentPhase.PAUSED

_ACTIVE_PHASES = (
    _IDEATION,
    _HYPOTHESIS,
    _FALSIFICATION_PLAN,
    _FALSIFICATION_TRIALS,
    _RCA,
    _VERDICT,
    _STRATEGY_DESIGN,
    _OPTIMIZATION,
)

#: Role family and human label for each phase's top-level orchestrator.
_PHASE_ORCHESTRATORS: Mapping[HypothesisDevelopmentPhase, tuple[str, str]] = {
    _IDEATION: ("research", "Thesis research ideation orchestrator"),
    _HYPOTHESIS: ("research", "Thesis research hypothesis orchestrator"),
    _FALSIFICATION_PLAN: (
        "research",
        "Thesis research falsification planner orchestrator",
    ),
    _FALSIFICATION_TRIALS: (
        "research",
        "Thesis research falsification trials orchestrator",
    ),
    _RCA: ("critique", "Thesis critique root-cause orchestrator"),
    _VERDICT: ("research", "Thesis research verdict orchestrator"),
    _STRATEGY_DESIGN: ("coding", "Thesis coding strategy-design orchestrator"),
    _OPTIMIZATION: ("coding", "Thesis coding optimization orchestrator"),
}


@dataclass(frozen=True, slots=True)
class _ActiveSubAgent:
    """The currently open top-level orchestrator for one execution."""

    sub_agent_id: str
    phase: HypothesisDevelopmentPhase
    role: str
    context: SubAgentContextSpec


_TRANSITIONS: Mapping[
    HypothesisDevelopmentPhase, frozenset[HypothesisDevelopmentPhase]
] = {
    _REQUESTED: frozenset({_IDEATION, _PAUSED, _FAILED}),
    _IDEATION: frozenset({_HYPOTHESIS, _PAUSED, _FAILED}),
    _HYPOTHESIS: frozenset({_FALSIFICATION_PLAN, _PAUSED, _FAILED}),
    _FALSIFICATION_PLAN: frozenset({_FALSIFICATION_TRIALS, _PAUSED, _FAILED}),
    _FALSIFICATION_TRIALS: frozenset({_RCA, _VERDICT, _PAUSED, _FAILED}),
    _RCA: frozenset({_FALSIFICATION_TRIALS, _HYPOTHESIS, _PAUSED, _FAILED}),
    _VERDICT: frozenset({_STRATEGY_DESIGN, _HYPOTHESIS, _PAUSED, _FAILED}),
    _STRATEGY_DESIGN: frozenset({_OPTIMIZATION, _PAUSED, _FAILED}),
    _OPTIMIZATION: frozenset({_COMPLETED, _PAUSED, _FAILED}),
    _PAUSED: frozenset({*_ACTIVE_PHASES}),
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

HYPOTHESIS_DEVELOPMENT_PHASES: tuple[str, ...] = (
    _REQUESTED.value,
    _IDEATION.value,
    _HYPOTHESIS.value,
    _FALSIFICATION_PLAN.value,
    _FALSIFICATION_TRIALS.value,
    _RCA.value,
    _VERDICT.value,
    _STRATEGY_DESIGN.value,
    _OPTIMIZATION.value,
)

HYPOTHESIS_DEVELOPMENT_WORKFLOW = WorkflowDefinition(
    workflow_id=_WORKFLOW_ID,
    version=_WORKFLOW_VERSION,
    title=_WORKFLOW_TITLE,
    phases=HYPOTHESIS_DEVELOPMENT_PHASES,
)


def transition_phase(
    current: HypothesisDevelopmentPhase, target: HypothesisDevelopmentPhase
) -> HypothesisDevelopmentPhase:
    """Move between phases through the explicit transition table."""
    if target not in _TRANSITIONS[current]:
        raise InvalidPhaseTransition(
            f"workflow phase cannot move from {current.value} to {target.value}"
        )
    return target


@dataclass(frozen=True, slots=True)
class PhaseTransition:
    """One immutable, ordered record of a workflow phase change."""

    sequence: int
    from_phase: HypothesisDevelopmentPhase
    to_phase: HypothesisDevelopmentPhase
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
            "at": _iso(self.at),
            "fromPhase": self.from_phase.value,
            "intentEpoch": self.intent_epoch,
            "reason": self.reason,
            "sequence": self.sequence,
            "toPhase": self.to_phase.value,
        }


@dataclass(frozen=True, slots=True)
class HypothesisDevelopmentRequest:
    """The chat request context that triggers one workflow execution."""

    workspace_id: str
    chat_id: str
    execution_id: str
    trigger_message_id: str
    requested_by: str
    ideas: str
    intent_epoch: int = 0
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        _workspace_id(self.workspace_id)
        for name in ("chat_id", "execution_id", "trigger_message_id", "requested_by"):
            _identifier(getattr(self, name), name)
        try:
            _text(self.ideas, "ideas", max_length=8_192)
        except WorkflowError as error:
            raise HypothesisDevelopmentIntakeError(str(error)) from error
        if self.intent_epoch < 0:
            raise HypothesisDevelopmentIntakeError("intent_epoch must not be negative")
        if self.created_at is not None:
            if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
                raise HypothesisDevelopmentIntakeError(
                    "created_at must be timezone-aware"
                )
            object.__setattr__(self, "created_at", self.created_at.astimezone(UTC))

    def as_dict(self) -> dict[str, object]:
        """Return the detached request representation for transport."""
        return {
            "chatId": self.chat_id,
            "createdAt": None if self.created_at is None else _iso(self.created_at),
            "executionId": self.execution_id,
            "ideas": self.ideas,
            "intentEpoch": self.intent_epoch,
            "requestedBy": self.requested_by,
            "triggerMessageId": self.trigger_message_id,
            "workspaceId": self.workspace_id,
        }


@dataclass(frozen=True, slots=True)
class StrategyContent:
    """Content-addressed strategy content shared by probes and designs."""

    strategy_id: str
    version: str
    source: Mapping[str, str]
    tests: Mapping[str, str]
    harness_configuration: Mapping[str, object] = field(default_factory=dict)
    dependencies: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    harness_manifest_reference: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.strategy_id, "strategy_id")
        _text(self.version, "strategy version", max_length=64)
        for name in ("source", "tests"):
            files = getattr(self, name)
            if not isinstance(files, Mapping) or not files:
                raise FalsificationComposeError(f"{name} must be a non-empty mapping")
            for path, content in files.items():
                if not isinstance(path, str) or not path.strip():
                    raise FalsificationComposeError(f"{name} paths must be non-empty")
                if not isinstance(content, str):
                    raise FalsificationComposeError(
                        f"{name} values must be path-to-text mappings"
                    )
        object.__setattr__(self, "source", MappingProxyType(dict(self.source)))
        object.__setattr__(self, "tests", MappingProxyType(dict(self.tests)))
        if not isinstance(self.harness_configuration, Mapping):
            raise FalsificationComposeError("harness_configuration must be a mapping")
        object.__setattr__(
            self,
            "harness_configuration",
            MappingProxyType(dict(self.harness_configuration)),
        )

    def bundle(self) -> StrategyBundle:
        """Return the sandbox-ready immutable bundle for this content."""
        return StrategyBundle.create(
            strategy_id=self.strategy_id,
            version=self.version,
            source=dict(self.source),
            tests=dict(self.tests),
            dependencies=self.dependencies,
            harness_configuration=dict(self.harness_configuration),
            limitations=self.limitations,
            harness_manifest_reference=self.harness_manifest_reference,
        )


@dataclass(frozen=True, slots=True)
class HypothesisDraft:
    """The model-authored falsifiable hypothesis and its probe instrument."""

    claim: str
    disagreement: str
    risk_factor: str
    horizon: str
    universe: tuple[str, ...]
    falsifiers: tuple[HypothesisFalsifier, ...]
    probe: StrategyContent
    sizing: str | None = None
    body: str = ""
    hypothesis_id: str | None = None

    def __post_init__(self) -> None:
        _text(self.claim, "claim", max_length=8_192)
        _text(self.disagreement, "disagreement", max_length=8_192)
        _text(self.risk_factor, "risk_factor", max_length=256)
        _text(self.horizon, "horizon", max_length=256)
        if isinstance(self.universe, str) or not isinstance(self.universe, Sequence):
            raise FalsificationComposeError("universe must be a sequence")
        universe = tuple(_identifier(item, "universe") for item in self.universe)
        if not universe:
            raise FalsificationComposeError("universe must contain at least one symbol")
        object.__setattr__(self, "universe", universe)
        if any(not isinstance(item, HypothesisFalsifier) for item in self.falsifiers):
            raise FalsificationComposeError(
                "hypothesis draft requires HypothesisFalsifier values"
            )
        if not isinstance(self.probe, StrategyContent):
            raise FalsificationComposeError(
                "hypothesis draft requires a probe strategy"
            )
        if self.sizing is not None:
            _text(self.sizing, "sizing", max_length=256)
        if self.hypothesis_id is not None:
            _identifier(self.hypothesis_id, "hypothesis_id")


@dataclass(frozen=True, slots=True)
class PlannedTest:
    """One planned falsifier test before it is bound to a declared falsifier."""

    test_id: str
    falsifier_id: str
    description: str

    def __post_init__(self) -> None:
        _identifier(self.test_id, "test_id")
        _identifier(self.falsifier_id, "falsifier_id")
        _text(self.description, "test description", max_length=2_048)


@dataclass(frozen=True, slots=True)
class PlaceboControl:
    """One negative/placebo control that should not show the claimed edge."""

    control_id: str
    description: str
    expectation: str

    def __post_init__(self) -> None:
        _identifier(self.control_id, "control_id")
        _text(self.description, "control description", max_length=2_048)
        _text(self.expectation, "control expectation", max_length=2_048)


@dataclass(frozen=True, slots=True)
class FalsificationPlanDraft:
    """The model-authored plan before falsifier binding."""

    tests: tuple[PlannedTest, ...]
    controls: tuple[PlaceboControl, ...]


@dataclass(frozen=True, slots=True)
class FalsifierTest:
    """One planned falsifier test bound to its observable and threshold."""

    test_id: str
    falsifier_id: str
    observable: str
    condition: str
    threshold: float
    description: str

    def __post_init__(self) -> None:
        _identifier(self.test_id, "test_id")
        _identifier(self.falsifier_id, "falsifier_id")
        _identifier(self.observable, "observable")
        if self.condition not in FALSIFIER_CONDITIONS:
            raise WorkflowError(
                f"falsifier condition must be one of {sorted(FALSIFIER_CONDITIONS)}"
            )
        if isinstance(self.threshold, bool) or not isinstance(
            self.threshold, int | float
        ):
            raise WorkflowError("falsifier threshold must be a finite number")
        object.__setattr__(self, "threshold", float(self.threshold))
        _text(self.description, "test description", max_length=2_048)


@dataclass(frozen=True, slots=True)
class FalsificationPlan:
    """The concrete falsification plan: tests plus negative controls."""

    tests: tuple[FalsifierTest, ...]
    controls: tuple[PlaceboControl, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.tests, tuple) or not isinstance(self.controls, tuple):
            raise WorkflowError("falsification plan tests and controls must be tuples")


@dataclass(frozen=True, slots=True)
class FalsificationTrial:
    """One recorded falsifier trial with bound run evidence."""

    test_id: str
    falsifier_id: str
    run_id: str
    digest: str
    run_state: str
    observed: float | None
    falsified: bool

    def __post_init__(self) -> None:
        _identifier(self.test_id, "test_id")
        _identifier(self.falsifier_id, "falsifier_id")
        _text(self.run_id, "run_id", max_length=256)
        if not isinstance(self.digest, str) or _DIGEST.fullmatch(self.digest) is None:
            raise WorkflowError("trial digest must be a lowercase SHA-256 digest")
        _text(self.run_state, "run_state", max_length=64)
        if self.observed is not None and (
            isinstance(self.observed, bool)
            or not isinstance(self.observed, int | float)
        ):
            raise WorkflowError("trial observation must be numeric when present")

    def as_dict(self) -> dict[str, object]:
        """Return the portable trial representation."""
        return {
            "digest": self.digest,
            "falsified": self.falsified,
            "falsifierId": self.falsifier_id,
            "observed": self.observed,
            "runId": self.run_id,
            "runState": self.run_state,
            "testId": self.test_id,
        }


@dataclass(frozen=True, slots=True)
class RcaDiagnosis:
    """One fresh-subagent root-cause diagnosis of a failed trial."""

    falsifier_id: str
    failure_class: str
    rationale: str
    confidence: float
    follow_ups: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _identifier(self.falsifier_id, "falsifier_id")
        _text(self.failure_class, "failure_class", max_length=64)
        _text(self.rationale, "rca rationale", max_length=4_096)
        if not 0.0 <= self.confidence <= 1.0:
            raise WorkflowError("rca confidence must fall within [0, 1]")

    def as_dict(self) -> dict[str, object]:
        """Return the portable diagnosis representation."""
        return {
            "confidence": self.confidence,
            "failureClass": self.failure_class,
            "falsifierId": self.falsifier_id,
            "followUps": list(self.follow_ups),
            "rationale": self.rationale,
        }


@dataclass(frozen=True, slots=True)
class ExitCriterion:
    """A strategy exit phrased as a falsifier re-check."""

    observable: str
    condition: str
    threshold: float
    description: str
    falsifier_id: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.observable, "exit observable")
        if self.condition not in FALSIFIER_CONDITIONS:
            raise WorkflowError(
                f"exit condition must be one of {sorted(FALSIFIER_CONDITIONS)}"
            )
        if isinstance(self.threshold, bool) or not isinstance(
            self.threshold, int | float
        ):
            raise WorkflowError("exit threshold must be a finite number")
        object.__setattr__(self, "threshold", float(self.threshold))
        _text(self.description, "exit description", max_length=2_048)
        if self.falsifier_id is not None:
            _identifier(self.falsifier_id, "exit falsifier_id")

    def as_dict(self) -> dict[str, object]:
        """Return the portable exit-criterion representation."""
        return {
            "condition": self.condition,
            "description": self.description,
            "falsifierId": self.falsifier_id,
            "observable": self.observable,
            "threshold": self.threshold,
        }


@dataclass(frozen=True, slots=True)
class StrategyDesignDraft:
    """The model-authored strategy with falsifier-recheck exit criteria."""

    content: StrategyContent
    exit_criteria: tuple[ExitCriterion, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.content, StrategyContent):
            raise FalsificationComposeError("strategy design requires content")
        if not isinstance(self.exit_criteria, tuple):
            raise FalsificationComposeError(
                "strategy design exit criteria must be a tuple"
            )


@dataclass(frozen=True, slots=True)
class FalsificationVerdict:
    """The conservative falsification verdict for one hypothesis."""

    outcome: str
    reason: str
    trials: tuple[FalsificationTrial, ...] = ()

    def __post_init__(self) -> None:
        if self.outcome not in _VERDICTS:
            raise WorkflowError(f"verdict outcome must be one of {sorted(_VERDICTS)}")
        _text(self.reason, "verdict reason", max_length=2_048)

    @property
    def falsified(self) -> bool:
        """Return whether the verdict refutes the hypothesis."""
        return self.outcome == VERDICT_REFUTED

    def as_dict(self) -> dict[str, object]:
        """Return the portable verdict representation."""
        return {
            "outcome": self.outcome,
            "reason": self.reason,
            "trials": [trial.as_dict() for trial in self.trials],
        }


# -- gate checks ------------------------------------------------------------


def check_falsifiability(front_matter: HypothesisFrontMatter) -> str | None:
    """Return the failure reason when a hypothesis is not falsifiable."""
    if not front_matter.claim.strip():
        return "hypothesis requires a non-empty claim"
    if not front_matter.disagreement.strip():
        return (
            "hypothesis requires a non-empty disagreement with market pricing: "
            "if the market prices the claim fairly, there is no trade"
        )
    if not front_matter.falsifiers:
        return "hypothesis requires at least one falsifier"
    for falsifier in front_matter.falsifiers:
        if not falsifier.observable.strip():
            return "every falsifier requires a non-empty observable"
        if falsifier.condition not in FALSIFIER_CONDITIONS:
            return "every falsifier requires a supported condition"
        if not falsifier.test.strip():
            return "every falsifier requires a non-empty test"
    return None


def check_falsification_plan(
    plan: FalsificationPlan, front_matter: HypothesisFrontMatter
) -> str | None:
    """Return the failure reason when a plan cannot falsify the hypothesis."""
    if not plan.tests:
        return "falsification plan requires at least one falsifier test"
    if not plan.controls:
        return "falsification plan requires at least one negative/placebo control"
    declared = {
        falsifier.falsifier_id
        for falsifier in front_matter.falsifiers
        if falsifier.falsifier_id is not None
    }
    for test in plan.tests:
        if test.falsifier_id not in declared:
            return (
                f"falsification test {test.test_id!r} references undeclared "
                f"falsifier {test.falsifier_id!r}"
            )
    return None


def check_evidence_bound(
    plan: FalsificationPlan | None, trials: Sequence[FalsificationTrial]
) -> str | None:
    """Return the failure reason when a planned falsifier lacks evidence."""
    if plan is None:
        return "no falsification plan was recorded"
    by_falsifier = {trial.falsifier_id: trial for trial in trials}
    for test in plan.tests:
        trial = by_falsifier.get(test.falsifier_id)
        if trial is None:
            return f"falsifier {test.falsifier_id!r} has no recorded trial"
        if trial.observed is None:
            return (
                f"falsifier {test.falsifier_id!r} has no numeric observation bound "
                "to a run digest"
            )
        if _DIGEST.fullmatch(trial.digest) is None:
            return f"falsifier {test.falsifier_id!r} has no run digest"
    return None


def check_verdict_advances(verdict: FalsificationVerdict) -> str | None:
    """Return the failure reason when a verdict may not reach strategy design."""
    if verdict.outcome == VERDICT_REFUTED:
        return (
            "the hypothesis was refuted; a refuted premise cannot advance to "
            "strategy design and must return to hypothesis"
        )
    return None


def check_strategy_design(
    bundle: StrategyBundle | None,
    exit_criteria: Sequence[ExitCriterion],
    front_matter: HypothesisFrontMatter,
) -> str | None:
    """Return the failure reason when a strategy is not a falsification tool."""
    if bundle is None:
        return "strategy design requires a validated strategy bundle"
    if not exit_criteria:
        return "strategy design requires declared exit criteria"
    falsifiers = front_matter.falsifiers
    for criterion in exit_criteria:
        if not any(
            _exit_rechecks_falsifier(criterion, falsifier) for falsifier in falsifiers
        ):
            return (
                "every exit criterion must re-check a declared falsifier "
                f"(observable={criterion.observable!r}, "
                f"condition={criterion.condition!r})"
            )
    return None


def _exit_rechecks_falsifier(
    criterion: ExitCriterion, falsifier: HypothesisFalsifier
) -> bool:
    if criterion.falsifier_id is not None:
        return criterion.falsifier_id == falsifier.falsifier_id
    return (
        criterion.observable == falsifier.observable
        and criterion.condition == falsifier.condition
        and criterion.threshold == falsifier.threshold
    )


# -- composers --------------------------------------------------------------


class HypothesisComposer(Protocol):
    """Injected boundary that turns model output into a hypothesis draft."""

    def compose(self, invocation: InvocationResult) -> HypothesisDraft: ...


class FalsificationPlanner(Protocol):
    """Injected boundary that turns model output into a falsification plan."""

    def compose(self, invocation: InvocationResult) -> FalsificationPlanDraft: ...


class RcaComposer(Protocol):
    """Injected boundary that turns model output into an RCA diagnosis."""

    def compose(self, invocation: InvocationResult) -> RcaDiagnosis: ...


class StrategyDesigner(Protocol):
    """Injected boundary that turns model output into a strategy design."""

    def compose(self, invocation: InvocationResult) -> StrategyDesignDraft: ...


def _json_document(invocation: InvocationResult, label: str) -> dict[str, object]:
    output = invocation.response.output
    if not isinstance(output, str) or not output.strip():
        raise FalsificationComposeError(f"model provider returned no {label} content")
    try:
        document = json.loads(output)
    except json.JSONDecodeError as error:
        raise FalsificationComposeError(
            f"model output is not valid JSON {label} content"
        ) from error
    if not isinstance(document, dict):
        raise FalsificationComposeError(f"{label} content must be a JSON object")
    return document


def _compose_text(document: Mapping[str, object], key: str, label: str) -> str:
    try:
        return _text(document.get(key), label, max_length=8_192)
    except WorkflowError as error:
        raise FalsificationComposeError(str(error)) from error


class JsonDevelopmentHypothesisComposer:
    """Parse the canonical JSON hypothesis envelope from model output."""

    def compose(self, invocation: InvocationResult) -> HypothesisDraft:
        document = _json_document(invocation, "hypothesis")
        universe = _compose_symbols(document.get("universe"), "universe")
        falsifiers = _compose_falsifiers(document.get("falsifiers"))
        probe = _compose_strategy(document.get("probe"), "probe")
        sizing = document.get("sizing")
        if sizing is not None and not (isinstance(sizing, str) and sizing.strip()):
            raise FalsificationComposeError("sizing must be non-empty text when set")
        body = document.get("body", "")
        if not isinstance(body, str):
            raise FalsificationComposeError("hypothesis body must be text")
        hypothesis_id = document.get("hypothesisId")
        if hypothesis_id is not None:
            try:
                _identifier(hypothesis_id, "hypothesisId")
            except WorkflowError as error:
                raise FalsificationComposeError(str(error)) from error
        return HypothesisDraft(
            claim=_compose_text(document, "claim", "claim"),
            disagreement=_compose_text(document, "disagreement", "disagreement"),
            risk_factor=_compose_text(document, "riskFactor", "riskFactor"),
            horizon=_compose_text(document, "horizon", "horizon"),
            universe=universe,
            falsifiers=falsifiers,
            probe=probe,
            sizing=None if sizing is None else sizing.strip(),
            body=body,
            hypothesis_id=None if hypothesis_id is None else str(hypothesis_id),
        )


def _compose_symbols(value: object, label: str) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise FalsificationComposeError(f"{label} must be a sequence of symbols")
    symbols = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise FalsificationComposeError(f"{label} must contain non-empty symbols")
        symbols.append(item.strip())
    if not symbols:
        raise FalsificationComposeError(f"{label} must contain at least one symbol")
    return tuple(dict.fromkeys(symbols))


def _compose_falsifiers(value: object) -> tuple[HypothesisFalsifier, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise FalsificationComposeError("falsifiers must be a sequence")
    falsifiers: list[HypothesisFalsifier] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise FalsificationComposeError("each falsifier must be a JSON object")
        normalized = dict(item)
        if "falsifierId" in normalized and "falsifier_id" not in normalized:
            normalized["falsifier_id"] = normalized.pop("falsifierId")
        try:
            falsifiers.append(HypothesisFalsifier.model_validate(normalized))
        except ValueError as error:
            raise FalsificationComposeError(f"invalid falsifier: {error}") from error
    return tuple(falsifiers)


class JsonFalsificationPlanner:
    """Parse the canonical JSON falsification-plan envelope."""

    def compose(self, invocation: InvocationResult) -> FalsificationPlanDraft:
        document = _json_document(invocation, "falsification plan")
        tests = _compose_tests(document.get("tests"))
        controls = _compose_controls(document.get("controls"))
        return FalsificationPlanDraft(tests=tests, controls=controls)


def _compose_tests(value: object) -> tuple[PlannedTest, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise FalsificationComposeError("plan tests must be a sequence")
    tests: list[PlannedTest] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise FalsificationComposeError("each plan test must be a JSON object")
        try:
            tests.append(
                PlannedTest(
                    test_id=item.get("testId"),
                    falsifier_id=item.get("falsifierId"),
                    description=item.get("description"),
                )
            )
        except WorkflowError as error:
            raise FalsificationComposeError(str(error)) from error
    return tuple(tests)


def _compose_controls(value: object) -> tuple[PlaceboControl, ...]:
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise FalsificationComposeError("plan controls must be a sequence")
    controls: list[PlaceboControl] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise FalsificationComposeError("each control must be a JSON object")
        try:
            controls.append(
                PlaceboControl(
                    control_id=item.get("controlId"),
                    description=item.get("description"),
                    expectation=item.get("expectation"),
                )
            )
        except WorkflowError as error:
            raise FalsificationComposeError(str(error)) from error
    return tuple(controls)


class JsonRcaComposer:
    """Parse the canonical JSON RCA-diagnosis envelope."""

    def compose(self, invocation: InvocationResult) -> RcaDiagnosis:
        document = _json_document(invocation, "rca")
        confidence = document.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, int | float):
            raise FalsificationComposeError("rca confidence must be numeric")
        follow_ups = document.get("followUps", [])
        if isinstance(follow_ups, str) or not isinstance(follow_ups, Sequence):
            raise FalsificationComposeError("rca followUps must be a sequence")
        for item in follow_ups:
            if not isinstance(item, str) or not item.strip():
                raise FalsificationComposeError(
                    "rca followUps must contain non-empty strings"
                )
        return RcaDiagnosis(
            falsifier_id=_compose_text(document, "falsifierId", "falsifierId"),
            failure_class=_compose_text(document, "failureClass", "failureClass"),
            rationale=_compose_text(document, "rationale", "rationale"),
            confidence=float(confidence),
            follow_ups=tuple(item.strip() for item in follow_ups),
        )


class JsonStrategyDesigner:
    """Parse the canonical JSON strategy-design envelope."""

    def compose(self, invocation: InvocationResult) -> StrategyDesignDraft:
        document = _json_document(invocation, "strategy design")
        content = _compose_strategy(document, "strategy")
        criteria = _compose_exit_criteria(document.get("exitCriteria"))
        return StrategyDesignDraft(content=content, exit_criteria=criteria)


def _compose_strategy(value: object, label: str) -> StrategyContent:
    if not isinstance(value, Mapping):
        raise FalsificationComposeError(f"{label} must be a JSON object")
    files = value.get("files")
    tests = value.get("tests")
    strategy_id = value.get("strategyId")
    version = value.get("version")
    if not isinstance(files, Mapping) or not files:
        raise FalsificationComposeError(f"{label} requires source files")
    if not isinstance(tests, Mapping) or not tests:
        raise FalsificationComposeError(f"{label} requires tests")
    if not isinstance(strategy_id, str) or not strategy_id.strip():
        raise FalsificationComposeError(f"{label} requires strategyId")
    if not isinstance(version, str) or not version.strip():
        raise FalsificationComposeError(f"{label} requires version")
    dependencies = value.get("dependencies", [])
    limitations = value.get("limitations", [])
    for sequence, name in (
        (dependencies, "dependencies"),
        (limitations, "limitations"),
    ):
        if isinstance(sequence, str) or not isinstance(sequence, Sequence):
            raise FalsificationComposeError(f"{label} {name} must be a sequence")
        for item in sequence:
            if not isinstance(item, str) or not item.strip():
                raise FalsificationComposeError(
                    f"{label} {name} must contain non-empty strings"
                )
    harness_configuration = value.get("harnessConfiguration", {})
    if not isinstance(harness_configuration, Mapping):
        raise FalsificationComposeError(
            f"{label} harnessConfiguration must be a JSON object"
        )
    try:
        return StrategyContent(
            strategy_id=strategy_id,
            version=version,
            source=dict(files),
            tests=dict(tests),
            harness_configuration=dict(harness_configuration),
            dependencies=tuple(dict.fromkeys(item.strip() for item in dependencies)),
            limitations=tuple(dict.fromkeys(item.strip() for item in limitations)),
        )
    except (WorkflowError, StrategyBundleError) as error:
        raise FalsificationComposeError(str(error)) from error


def _compose_exit_criteria(value: object) -> tuple[ExitCriterion, ...]:
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise FalsificationComposeError("exitCriteria must be a sequence")
    criteria: list[ExitCriterion] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise FalsificationComposeError("each exit criterion must be an object")
        try:
            criteria.append(
                ExitCriterion(
                    observable=item.get("observable"),
                    condition=item.get("condition"),
                    threshold=item.get("threshold"),
                    description=item.get("description"),
                    falsifier_id=item.get("falsifierId"),
                )
            )
        except WorkflowError as error:
            raise FalsificationComposeError(str(error)) from error
    return tuple(criteria)


# -- workflow ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HypothesisDevelopmentState:
    """The immutable state snapshot after each workflow transition."""

    workspace_id: str
    execution_id: str
    phase: HypothesisDevelopmentPhase
    intent_epoch: int
    transitions: tuple[PhaseTransition, ...] = ()
    hypothesis: HypothesisDocument | None = None
    draft: HypothesisDraft | None = None
    plan: FalsificationPlan | None = None
    trials: tuple[FalsificationTrial, ...] = ()
    rca: tuple[RcaDiagnosis, ...] = ()
    rca_cycles: int = 0
    verdict: FalsificationVerdict | None = None
    strategy_bundle: StrategyBundle | None = None
    exit_criteria: tuple[ExitCriterion, ...] = ()
    optimization_backtest: BacktestRunResult | None = None
    model_provenance: Mapping[str, object] | None = None
    journal_id: str | None = None
    error_message: str | None = None
    detail: str | None = None
    fenced: bool = False


@dataclass(frozen=True, slots=True)
class HypothesisDevelopmentOutcome:
    """The detached result of one workflow run, safe for UI transport."""

    workspace_id: str
    execution_id: str
    phase: HypothesisDevelopmentPhase
    fenced: bool
    transitions: tuple[PhaseTransition, ...]
    hypothesis: HypothesisDocument | None = None
    plan: FalsificationPlan | None = None
    trials: tuple[FalsificationTrial, ...] = ()
    rca: tuple[RcaDiagnosis, ...] = ()
    verdict: FalsificationVerdict | None = None
    strategy_bundle: StrategyBundle | None = None
    exit_criteria: tuple[ExitCriterion, ...] = ()
    optimization_backtest: BacktestRunResult | None = None
    journal_id: str | None = None
    detail: str | None = None

    def as_dict(self) -> dict[str, object]:
        """Return the transport representation of the outcome."""
        return {
            "executionId": self.execution_id,
            "exitCriteria": [item.as_dict() for item in self.exit_criteria],
            "fenced": self.fenced,
            "hypothesis": (
                None
                if self.hypothesis is None
                else {
                    "frontMatter": self.hypothesis.front_matter.model_dump(mode="json"),
                    "body": self.hypothesis.body,
                    "contentSha256": self.hypothesis.content_sha256,
                    "path": self.hypothesis.relative_path,
                }
            ),
            "journalId": self.journal_id,
            "optimizationBacktest": (
                None
                if self.optimization_backtest is None
                else self.optimization_backtest.as_dict()
            ),
            "phase": self.phase.value,
            "plan": (
                None
                if self.plan is None
                else {
                    "tests": [
                        {
                            "condition": test.condition,
                            "description": test.description,
                            "falsifierId": test.falsifier_id,
                            "observable": test.observable,
                            "testId": test.test_id,
                            "threshold": test.threshold,
                        }
                        for test in self.plan.tests
                    ],
                    "controls": [
                        {
                            "controlId": control.control_id,
                            "description": control.description,
                            "expectation": control.expectation,
                        }
                        for control in self.plan.controls
                    ],
                }
            ),
            "rca": [item.as_dict() for item in self.rca],
            "strategyManifest": (
                None
                if self.strategy_bundle is None
                else self.strategy_bundle.manifest()
            ),
            "transitions": [item.as_dict() for item in self.transitions],
            "trials": [item.as_dict() for item in self.trials],
            "verdict": None if self.verdict is None else self.verdict.as_dict(),
            "workspaceId": self.workspace_id,
            "detail": self.detail,
        }


class HypothesisDevelopmentWorkflow:
    """Orchestrate the falsification-first chat-to-strategy pipeline."""

    def __init__(
        self,
        *,
        chat: ChatService,
        router: ModelRouter,
        runner: BacktestRunner,
        validator: StrategySandboxValidator,
        hypothesis_store: HypothesisStore | None = None,
        composer: HypothesisComposer | None = None,
        planner: FalsificationPlanner | None = None,
        rca_composer: RcaComposer | None = None,
        designer: StrategyDesigner | None = None,
        ledger: AttemptLedger | None = None,
        subagent_factory: SubagentContextFactory | None = None,
        journal: CommandJournal | None = None,
        clock: Callable[[], datetime] = _utc_now,
        id_factory: Callable[[str], str] = _new_id,
        research_role: Role = "research",
        critique_role: Role = "critique",
        coding_role: Role = "coding",
        experiment_id: str | None = None,
        max_rca_cycles: int = DEFAULT_MAX_RCA_CYCLES,
        knowledge_cutoff: datetime | None = None,
        subagent_mcp_tools: Mapping[str, tuple[str, ...]] | None = None,
    ) -> None:
        self._chat = chat
        self._router = router
        self._runner = runner
        self._validator = validator
        self._store = (
            InMemoryHypothesisStore() if hypothesis_store is None else hypothesis_store
        )
        self._composer = (
            JsonDevelopmentHypothesisComposer() if composer is None else composer
        )
        self._planner = JsonFalsificationPlanner() if planner is None else planner
        self._rca_composer = JsonRcaComposer() if rca_composer is None else rca_composer
        self._designer = JsonStrategyDesigner() if designer is None else designer
        self._ledger = ledger
        self._subagents = (
            SubagentContextFactory(clock=clock, id_factory=lambda: id_factory("ctx"))
            if subagent_factory is None
            else subagent_factory
        )
        self._journal = journal
        self._clock = clock
        self._id_factory = id_factory
        self._research_role = research_role
        self._critique_role = critique_role
        self._coding_role = coding_role
        self._experiment_id = experiment_id
        if not isinstance(max_rca_cycles, int) or max_rca_cycles < 0:
            raise WorkflowError("max_rca_cycles must be a non-negative integer")
        self._max_rca_cycles = max_rca_cycles
        if knowledge_cutoff is not None and (
            knowledge_cutoff.tzinfo is None or knowledge_cutoff.utcoffset() is None
        ):
            raise WorkflowError("knowledge_cutoff must be timezone-aware")
        self._knowledge_cutoff = knowledge_cutoff
        self._subagent_mcp_tools: dict[str, tuple[str, ...]] = {
            family: tuple(tools)
            for family, tools in dict(subagent_mcp_tools or {}).items()
        }
        self._active_orchestrators: dict[str, _ActiveSubAgent] = {}

    @property
    def definition(self) -> WorkflowDefinition:
        """Return the versioned workflow definition registered in chat."""
        return HYPOTHESIS_DEVELOPMENT_WORKFLOW

    @property
    def store(self) -> HypothesisStore:
        """Return the hypothesis-artifact store this workflow projects onto."""
        return self._store

    def launch(
        self,
        workspace_id: str,
        chat_id: str,
        *,
        actor_id: str,
        ideas: str,
        investigation_id: str | None = None,
    ) -> HypothesisDevelopmentRequest:
        """Persist the chat trigger and return the runnable request."""
        execution = self._chat.launch_workflow(
            workspace_id,
            chat_id,
            workflow=self.definition,
            actor_id=actor_id,
            body=ideas,
            investigation_id=investigation_id,
        )
        self._chat.create_message(
            workspace_id,
            chat_id,
            actor_id=actor_id,
            body=self.definition.title,
            role=MessageRole.SYSTEM,
            kind=TranscriptItemKind.WORKFLOW,
            data={
                "currentPhase": execution.phase,
                "executionId": execution.execution_id,
                "status": execution.state.value,
                "title": self.definition.title,
                "version": execution.workflow_version,
                "workflowId": execution.workflow_id,
            },
            execution_id=execution.execution_id,
        )
        return HypothesisDevelopmentRequest(
            workspace_id=workspace_id,
            chat_id=chat_id,
            execution_id=execution.execution_id,
            trigger_message_id=execution.trigger_message_id,
            requested_by=actor_id,
            ideas=ideas,
            intent_epoch=execution.intent_epoch,
            created_at=execution.created_at,
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

    def run(
        self, request: HypothesisDevelopmentRequest
    ) -> HypothesisDevelopmentOutcome:
        """Execute the documented phases once for this request snapshot."""
        state = HypothesisDevelopmentState(
            workspace_id=request.workspace_id,
            execution_id=request.execution_id,
            phase=_REQUESTED,
            intent_epoch=request.intent_epoch,
        )
        try:
            self._require_execution(request)
            self._check_stale_request(request)
            self._chat.start_workflow(
                request.workspace_id,
                request.execution_id,
                actor_id=request.requested_by,
            )
            state = self._transition(request, state, _IDEATION, "chat request accepted")
            draft, provenance = self._ideate(request)
            state = replace(
                state,
                draft=draft,
                model_provenance=MappingProxyType(dict(provenance)),
            )
            document = self._build_hypothesis(request, draft)
            self._store.save(document)
            state = replace(state, hypothesis=document)
            self._append_attempt(
                request,
                state,
                AttemptKind.HYPOTHESIS,
                "falsifiable hypothesis authored",
                {
                    "hypothesisId": document.hypothesis_id,
                    "disagreement": document.front_matter.disagreement,
                    "falsifiers": [
                        falsifier.model_dump(mode="json")
                        for falsifier in document.front_matter.falsifiers
                    ],
                },
            )
            state = self._transition(
                request, state, _HYPOTHESIS, "falsifiable hypothesis drafted"
            )
            state = self._transition_gated(
                request,
                state,
                _FALSIFICATION_PLAN,
                "falsifiability gate satisfied",
                lambda: check_falsifiability(document.front_matter),
                gate="falsifiability",
            )
            plan = self._plan(request, state)
            state = replace(state, plan=plan)
            self._append_attempt(
                request,
                state,
                AttemptKind.PARAMETER,
                "falsification plan recorded",
                {
                    "tests": [test.test_id for test in plan.tests],
                    "controls": [control.control_id for control in plan.controls],
                },
            )
            state = self._transition_gated(
                request,
                state,
                _FALSIFICATION_TRIALS,
                "falsification plan gate satisfied",
                lambda: check_falsification_plan(plan, document.front_matter),
                gate="falsification_plan",
            )
            state = self._run_trials(request, state)
            if self._needs_rca(state):
                state = self._transition(
                    request,
                    state,
                    _RCA,
                    "falsification trial failures require diagnosis",
                )
                state = self._diagnose(request, state)
                state = self._transition(
                    request,
                    state,
                    _FALSIFICATION_TRIALS,
                    "rca diagnoses recorded; retrying falsification trials",
                )
                state = self._run_trials(request, state)
            state = self._transition_gated(
                request,
                state,
                _VERDICT,
                "evidence bound to every planned falsifier",
                lambda: check_evidence_bound(state.plan, state.trials),
                gate="evidence_bound",
            )
            verdict = self._evaluate_verdict(state)
            state = self._project_hypothesis(request, state, status=verdict.outcome)
            state = replace(state, verdict=verdict)
            self._append_attempt(
                request,
                state,
                AttemptKind.METRIC,
                "falsification verdict recorded",
                verdict.as_dict(),
            )
            gate = check_verdict_advances(verdict)
            if gate is not None:
                self._emit_gate_blocked(
                    request, _VERDICT, _STRATEGY_DESIGN, "verdict", gate
                )
                state = self._transition(
                    request,
                    state,
                    _HYPOTHESIS,
                    "premise refuted; returning to hypothesis",
                )
                raise _Fenced(f"verdict gate: {gate}", state)
            state = self._transition(
                request,
                state,
                _STRATEGY_DESIGN,
                "hypothesis survived falsification",
            )
            state = self._design(request, state)
            state = self._transition_gated(
                request,
                state,
                _OPTIMIZATION,
                "strategy validated with falsifier exit criteria",
                lambda: check_strategy_design(
                    state.strategy_bundle, state.exit_criteria, document.front_matter
                ),
                gate="strategy_design",
            )
            state = self._optimize(request, state)
            journal_id = self._journal_entry(request, state)
            state = replace(state, journal_id=journal_id)
            self._finish_execution(
                request,
                WorkflowExecutionState.COMPLETED,
                ExecutionEventType.WORKFLOW_COMPLETED,
                payload=self._completion_payload(request, state),
            )
            state = self._transition(request, state, _COMPLETED, "outcome recorded")
        except _Fenced as fence:
            state = self._pause(
                request, fence.state if fence.state is not None else state, fence
            )
            self._emit_phase(request, state.phase, {"state": state.phase.value})
        except _PhaseFailure as failure:
            state = self._fail(request, failure.state, failure)
            self._emit_phase(request, state.phase, {"state": state.phase.value})
        except (
            BacktestRunError,
            FalsificationComposeError,
            HarnessError,
            StrategyBundleError,
            StrategyValidationError,
            WorkflowError,
        ) as error:
            state = self._fail(request, state, error)
            self._emit_phase(request, state.phase, {"state": state.phase.value})
        return self._outcome(state)

    # -- phases ------------------------------------------------------------

    def _ideate(
        self, request: HypothesisDevelopmentRequest
    ) -> tuple[HypothesisDraft, dict[str, object]]:
        context = assemble_context(
            workspace_scope=request.workspace_id,
            task=request.ideas,
            role=f"Thesis {self._research_role} falsification analyst",
            context_id=request.execution_id,
        )
        invocation = self._router.invoke(self._research_role, context)
        draft = self._composer.compose(invocation)
        return draft, invocation.provenance.as_dict()

    def _build_hypothesis(
        self, request: HypothesisDevelopmentRequest, draft: HypothesisDraft
    ) -> HypothesisDocument:
        hypothesis_id = draft.hypothesis_id or f"h-{request.execution_id}"
        front_matter = HypothesisFrontMatter(
            hypothesis_id=hypothesis_id,
            status=HypothesisStatus.ACTIVE,
            claim=draft.claim,
            disagreement=draft.disagreement,
            risk_factor=draft.risk_factor,
            horizon=draft.horizon,
            universe=draft.universe,
            falsifiers=draft.falsifiers,
            sizing=draft.sizing,
            links=HypothesisLinks(),
        )
        return HypothesisDocument(front_matter=front_matter, body=draft.body)

    def _plan(
        self, request: HypothesisDevelopmentRequest, state: HypothesisDevelopmentState
    ) -> FalsificationPlan:
        document = state.hypothesis
        if document is None:
            raise WorkflowError("falsification planning requires a hypothesis")
        context = assemble_context(
            workspace_scope=request.workspace_id,
            task=(
                "Design falsification trials and negative controls for the "
                f"hypothesis: {document.front_matter.claim}"
            ),
            role=f"Thesis {self._research_role} falsification planner",
            context_id=f"{request.execution_id}-plan",
        )
        invocation = self._router.invoke(self._research_role, context)
        draft = self._planner.compose(invocation)
        falsifiers = {
            falsifier.falsifier_id: falsifier
            for falsifier in document.front_matter.falsifiers
            if falsifier.falsifier_id is not None
        }
        tests: list[FalsifierTest] = []
        for planned in draft.tests:
            falsifier = falsifiers.get(planned.falsifier_id)
            if falsifier is None:
                raise FalsificationComposeError(
                    f"plan test {planned.test_id!r} references an undeclared "
                    f"falsifier {planned.falsifier_id!r}"
                )
            tests.append(
                FalsifierTest(
                    test_id=planned.test_id,
                    falsifier_id=planned.falsifier_id,
                    observable=falsifier.observable,
                    condition=falsifier.condition,
                    threshold=falsifier.threshold,
                    description=planned.description,
                )
            )
        return FalsificationPlan(tests=tuple(tests), controls=draft.controls)

    def _run_trials(
        self, request: HypothesisDevelopmentRequest, state: HypothesisDevelopmentState
    ) -> HypothesisDevelopmentState:
        document = state.hypothesis
        plan = state.plan
        if document is None or plan is None or state.draft is None:
            raise WorkflowError("trials require a hypothesis and a falsification plan")
        bundle = state.draft.probe.bundle()
        falsifiers = {
            falsifier.falsifier_id: falsifier
            for falsifier in document.front_matter.falsifiers
            if falsifier.falsifier_id is not None
        }
        retained = {
            trial.test_id: trial for trial in state.trials if trial.observed is not None
        }
        trials: list[FalsificationTrial] = []
        for test in plan.tests:
            existing = retained.get(test.test_id)
            if existing is not None:
                trials.append(existing)
                continue
            result = self._backtest(
                request,
                state,
                bundle=bundle,
                correlation_id=f"falsify-{request.execution_id}-{test.test_id}",
                label=test.test_id,
            )
            observed = _metric(result.metrics, test.observable)
            falsifier = falsifiers[test.falsifier_id]
            trials.append(
                FalsificationTrial(
                    test_id=test.test_id,
                    falsifier_id=test.falsifier_id,
                    run_id=result.job_id,
                    digest=_digest(result.as_dict()),
                    run_state=result.state,
                    observed=observed,
                    falsified=observed is not None and falsifier.falsifies(observed),
                )
            )
            self._append_attempt(
                request,
                state,
                AttemptKind.METRIC,
                f"falsification trial {test.test_id} recorded",
                {
                    "falsifierId": test.falsifier_id,
                    "observed": observed,
                    "runId": result.job_id,
                    "runState": result.state,
                },
            )
        state = replace(state, trials=tuple(trials))
        return self._project_hypothesis(request, state)

    def _needs_rca(self, state: HypothesisDevelopmentState) -> bool:
        if state.rca_cycles >= self._max_rca_cycles:
            return False
        return any(trial.observed is None for trial in state.trials)

    def _diagnose(
        self, request: HypothesisDevelopmentRequest, state: HypothesisDevelopmentState
    ) -> HypothesisDevelopmentState:
        document = state.hypothesis
        if document is None:
            raise WorkflowError("rca requires a hypothesis")
        problem_trials = [trial for trial in state.trials if trial.observed is None]
        falsifiers = {
            falsifier.falsifier_id: falsifier
            for falsifier in document.front_matter.falsifiers
        }
        diagnoses = list(state.rca)
        for trial in problem_trials:
            falsifier = falsifiers.get(trial.falsifier_id)
            parent = self._active_orchestrators.get(request.execution_id)
            parent_sub_agent_id = None if parent is None else parent.sub_agent_id
            agent_role = f"Thesis {self._critique_role} falsification RCA"
            task = (
                "Diagnose the failed falsifier trial "
                f"{trial.test_id!r} for observable "
                f"{(None if falsifier is None else falsifier.observable)!r} "
                f"on run {trial.run_id!r} (state {trial.run_state!r})."
            )
            context = self._subagents.create(
                role=agent_role,
                workspace_scope=request.workspace_id,
                task=task,
            )
            agent_context = SubAgentContextSpec(knowledge_cutoff=self._knowledge_cutoff)
            self._append_sub_agent(
                request,
                ExecutionEventType.SUB_AGENT_SPAWNED,
                sub_agent_id=context.context_id,
                parent_sub_agent_id=parent_sub_agent_id,
                phase=_RCA,
                role=agent_role,
                context=agent_context,
            )
            self._append_sub_agent(
                request,
                ExecutionEventType.SUB_AGENT_PROGRESS,
                sub_agent_id=context.context_id,
                parent_sub_agent_id=parent_sub_agent_id,
                phase=_RCA,
                role=agent_role,
                context=agent_context,
                extra={"kind": "thinking", "text": task},
            )
            invocation = self._router.invoke(self._critique_role, context.prompt)
            diagnosis = self._rca_composer.compose(invocation)
            if diagnosis.falsifier_id != trial.falsifier_id:
                self._append_sub_agent(
                    request,
                    ExecutionEventType.SUB_AGENT_FAILED,
                    sub_agent_id=context.context_id,
                    parent_sub_agent_id=parent_sub_agent_id,
                    phase=_RCA,
                    role=agent_role,
                    context=agent_context,
                    extra={"status": STATUS_FAILED},
                )
                raise FalsificationComposeError(
                    "rca diagnosis falsifier id does not match the trial"
                )
            self._append_sub_agent(
                request,
                ExecutionEventType.SUB_AGENT_OUTPUT,
                sub_agent_id=context.context_id,
                parent_sub_agent_id=parent_sub_agent_id,
                phase=_RCA,
                role=agent_role,
                context=agent_context,
                extra={
                    "artifact": {
                        "content": diagnosis.as_dict(),
                        "type": "rca_diagnosis",
                    },
                    "kind": "artifact",
                },
            )
            self._append_sub_agent(
                request,
                ExecutionEventType.SUB_AGENT_COMPLETED,
                sub_agent_id=context.context_id,
                parent_sub_agent_id=parent_sub_agent_id,
                phase=_RCA,
                role=agent_role,
                context=agent_context,
                extra={"frozen": True, "status": STATUS_COMPLETED},
            )
            diagnoses.append(diagnosis)
            self._append_attempt(
                request,
                state,
                AttemptKind.ABORTED_JOB,
                f"rca diagnosis recorded for {trial.test_id}",
                diagnosis.as_dict(),
            )
        return replace(state, rca=tuple(diagnoses), rca_cycles=state.rca_cycles + 1)

    def _evaluate_verdict(
        self, state: HypothesisDevelopmentState
    ) -> FalsificationVerdict:
        document = state.hypothesis
        if document is None:
            raise WorkflowError("verdict requires a hypothesis")
        by_falsifier = {trial.falsifier_id: trial for trial in state.trials}
        missing: list[str] = []
        for falsifier in document.front_matter.falsifiers:
            key = falsifier.falsifier_id
            trial = None if key is None else by_falsifier.get(key)
            if trial is None or trial.observed is None:
                missing.append(key or falsifier.observable)
        if missing:
            return FalsificationVerdict(
                outcome=VERDICT_INCONCLUSIVE,
                reason=(
                    "insufficient evidence: no numeric observation is bound for "
                    f"{sorted(missing)}"
                ),
                trials=state.trials,
            )
        hits = [trial for trial in state.trials if trial.falsified]
        if hits:
            return FalsificationVerdict(
                outcome=VERDICT_REFUTED,
                reason=(
                    "at least one falsifier was observed: "
                    + ", ".join(sorted(trial.falsifier_id for trial in hits))
                ),
                trials=state.trials,
            )
        return FalsificationVerdict(
            outcome=VERDICT_SUPPORTED,
            reason=(
                "no falsifier was observed across the planned trials; the "
                "hypothesis survived falsification"
            ),
            trials=state.trials,
        )

    def _design(
        self, request: HypothesisDevelopmentRequest, state: HypothesisDevelopmentState
    ) -> HypothesisDevelopmentState:
        document = state.hypothesis
        if document is None:
            raise WorkflowError("strategy design requires a hypothesis")
        context = assemble_context(
            workspace_scope=request.workspace_id,
            task=(
                "Implement a strategy for the surviving hypothesis whose exit "
                f"criteria are falsifier re-checks: {document.front_matter.claim}"
            ),
            role=f"Thesis {self._coding_role} strategy author",
            context_id=f"{request.execution_id}-design",
        )
        invocation = self._router.invoke(self._coding_role, context)
        draft = self._designer.compose(invocation)
        bundle = draft.content.bundle()
        validate_strategy_bundle(bundle, self._validator)
        self._append_attempt(
            request,
            state,
            AttemptKind.CANDIDATE,
            "sandbox-validated strategy designed",
            {
                "strategyId": bundle.strategy_id,
                "version": bundle.version,
                "bundleSha256": bundle.bundle_sha256,
                "exitCriteria": [
                    criterion.as_dict() for criterion in draft.exit_criteria
                ],
            },
        )
        return replace(state, strategy_bundle=bundle, exit_criteria=draft.exit_criteria)

    def _optimize(
        self, request: HypothesisDevelopmentRequest, state: HypothesisDevelopmentState
    ) -> HypothesisDevelopmentState:
        bundle = state.strategy_bundle
        if bundle is None:
            raise WorkflowError("optimization requires a validated strategy bundle")
        result = self._backtest(
            request,
            state,
            bundle=bundle,
            correlation_id=f"optimize-{request.execution_id}",
            label="optimization",
        )
        if not result.succeeded:
            raise _PhaseFailure(
                f"optimization backtest {result.job_id!r} ended in state "
                f"{result.state!r}",
                replace(state, optimization_backtest=result),
            )
        state = replace(state, optimization_backtest=result)
        self._append_attempt(
            request,
            state,
            AttemptKind.VARIANT,
            "strategy optimization run recorded",
            {"runId": result.job_id, "state": result.state},
        )
        return state

    def _backtest(
        self,
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
        *,
        bundle: StrategyBundle,
        correlation_id: str,
        label: str,
    ) -> BacktestRunResult:
        experiment_id = self._experiment_id or f"experiment-{request.execution_id}"
        job = self._chat.submit_tool_job(
            request.workspace_id,
            request.execution_id,
            tool_name=_TOOL_NAME,
            actor_id=_WORKFLOW_ACTOR,
            correlation_id=correlation_id,
            strategy_ids=(bundle.strategy_id,),
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
            error_message=None
            if result.succeeded
            else f"state {result.state!r} ({label})",
            intent_epoch=state.intent_epoch,
        )
        try:
            self._require_current(request, state)
        except _Fenced as fence:
            raise _Fenced(str(fence), replace(state)) from fence
        return result

    # -- projection and durability -----------------------------------------

    def _project_hypothesis(
        self,
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
        *,
        status: str | None = None,
    ) -> HypothesisDevelopmentState:
        document = state.hypothesis
        if document is None:
            return state
        projected_status = (
            document.status if status is None else HypothesisStatus(status)
        )
        links = HypothesisLinks(
            runs=tuple(trial.run_id for trial in state.trials),
            evidence=tuple(trial.digest for trial in state.trials),
        )
        projected = document.projected(status=projected_status, links=links)
        self._store.save(projected)
        return replace(state, hypothesis=projected)

    def _append_attempt(
        self,
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
        kind: AttemptKind,
        description: str,
        payload: Mapping[str, object],
    ) -> None:
        if self._ledger is None:
            return
        contract_sha256 = (
            state.hypothesis.content_sha256
            if state.hypothesis is not None
            else content_hash({"id": _WORKFLOW_ID, "version": _WORKFLOW_VERSION})
        )
        sequence = len(self._ledger.entries()) + 1
        self._ledger.append(
            AttemptRecord(
                attempt_id=f"attempt-{request.execution_id}-{sequence}",
                contract_sha256=contract_sha256,
                kind=kind,
                description=description,
                actor=_WORKFLOW_ACTOR,
                observed_at=_iso(self._clock()),
                payload=dict(payload),
            )
        )

    def _journal_entry(
        self,
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
    ) -> str | None:
        if self._journal is None:
            return None
        definition = self.definition
        journal_id = f"workflow-{request.execution_id}"
        artifacts: tuple[ArtifactRef, ...] = ()
        if state.hypothesis is not None:
            artifacts = (
                ArtifactRef.from_bytes(
                    state.hypothesis.relative_path,
                    state.hypothesis.to_bytes(),
                    media_type=_HYPOTHESIS_MEDIA_TYPE,
                ),
            )
        self._journal.accept(
            "workflow.hypothesis_development.completed",
            {
                "executionId": request.execution_id,
                "requestedBy": request.requested_by,
                "strategyIds": []
                if state.strategy_bundle is None
                else [state.strategy_bundle.strategy_id],
                "triggerMessageId": request.trigger_message_id,
                "workflow": {
                    "id": definition.workflow_id,
                    "version": definition.version,
                },
            },
            {
                "phase": _COMPLETED.value,
                "verdict": None if state.verdict is None else state.verdict.as_dict(),
            },
            workflow_hash=content_hash(
                {"id": definition.workflow_id, "version": definition.version}
            ),
            model_hash=content_hash(dict(state.model_provenance or {})),
            artifacts=artifacts,
            idempotency_id=journal_id,
        )
        return journal_id

    def _completion_payload(
        self,
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
    ) -> dict[str, object]:
        payload: dict[str, object] = {"state": _COMPLETED.value}
        if state.hypothesis is not None:
            payload["hypothesis"] = {
                "name": state.hypothesis.relative_path,
                "sha256": state.hypothesis.content_sha256,
            }
        if state.verdict is not None:
            payload["verdict"] = state.verdict.outcome
        return payload

    # -- sub-agent orchestration -------------------------------------------

    def _resolve_role(self, family: str) -> Role:
        if family == "critique":
            return self._critique_role
        if family == "coding":
            return self._coding_role
        return self._research_role

    def _append_sub_agent(
        self,
        request: HypothesisDevelopmentRequest,
        event_type: ExecutionEventType,
        *,
        sub_agent_id: str,
        parent_sub_agent_id: str | None,
        phase: HypothesisDevelopmentPhase | str,
        role: str,
        context: SubAgentContextSpec,
        accepts_input: bool = False,
        extra: Mapping[str, object] | None = None,
    ) -> None:
        append_sub_agent_event(
            self._chat.event_store,
            execution_id=request.execution_id,
            workspace_id=request.workspace_id,
            event_type=event_type,
            occurred_at=self._clock(),
            sub_agent_id=sub_agent_id,
            parent_sub_agent_id=parent_sub_agent_id,
            phase=(
                phase.value if isinstance(phase, HypothesisDevelopmentPhase) else phase
            ),
            role=role,
            context=context,
            accepts_input=accepts_input,
            intent_epoch=request.intent_epoch,
            extra=extra,
        )

    def _spawn_orchestrator(
        self,
        request: HypothesisDevelopmentRequest,
        phase: HypothesisDevelopmentPhase,
    ) -> None:
        family, label = _PHASE_ORCHESTRATORS[phase]
        tools = self._subagent_mcp_tools.get(family, ())
        context = SubAgentContextSpec(
            knowledge_cutoff=None, mcp_enabled=bool(tools), mcp_tools=tools
        )
        sub_agent_id = self._id_factory("subagent")
        self._append_sub_agent(
            request,
            ExecutionEventType.SUB_AGENT_SPAWNED,
            sub_agent_id=sub_agent_id,
            parent_sub_agent_id=None,
            phase=phase,
            role=label,
            context=context,
        )
        self._active_orchestrators[request.execution_id] = _ActiveSubAgent(
            sub_agent_id=sub_agent_id,
            phase=phase,
            role=label,
            context=context,
        )

    def _finish_orchestrator(
        self,
        request: HypothesisDevelopmentRequest,
        *,
        status: str,
        detail: str | None = None,
    ) -> None:
        active = self._active_orchestrators.pop(request.execution_id, None)
        if active is None:
            return
        extra: dict[str, object] = {"frozen": True, "status": status}
        if detail is not None:
            extra["detail"] = detail
        event_type = (
            ExecutionEventType.SUB_AGENT_COMPLETED
            if status == STATUS_COMPLETED
            else ExecutionEventType.SUB_AGENT_FAILED
        )
        self._append_sub_agent(
            request,
            event_type,
            sub_agent_id=active.sub_agent_id,
            parent_sub_agent_id=None,
            phase=active.phase,
            role=active.role,
            context=active.context,
            accepts_input=False,
            extra=extra,
        )

    def _on_phase_entered(
        self,
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
    ) -> None:
        self._finish_orchestrator(request, status=STATUS_COMPLETED)
        if state.phase in _ACTIVE_PHASES:
            self._spawn_orchestrator(request, state.phase)

    def _emit_gate_blocked(
        self,
        request: HypothesisDevelopmentRequest,
        from_phase: HypothesisDevelopmentPhase,
        to_phase: HypothesisDevelopmentPhase,
        gate: str,
        reason: str,
    ) -> None:
        active = self._active_orchestrators.get(request.execution_id)
        append_gate_event(
            self._chat.event_store,
            execution_id=request.execution_id,
            workspace_id=request.workspace_id,
            event_type=ExecutionEventType.WORKFLOW_GATE_BLOCKED,
            occurred_at=self._clock(),
            from_phase=from_phase.value,
            to_phase=to_phase.value,
            gate=gate,
            reason=reason,
            sub_agent_id=None if active is None else active.sub_agent_id,
            intent_epoch=request.intent_epoch,
        )
        if active is not None:
            self._append_sub_agent(
                request,
                ExecutionEventType.SUB_AGENT_NEEDS_INPUT,
                sub_agent_id=active.sub_agent_id,
                parent_sub_agent_id=None,
                phase=active.phase,
                role=active.role,
                context=active.context,
                accepts_input=True,
                extra={"gate": gate, "question": reason},
            )

    def _emit_gate_resolved(
        self,
        request: HypothesisDevelopmentRequest,
        from_phase: HypothesisDevelopmentPhase,
        to_phase: HypothesisDevelopmentPhase,
        gate: str,
        reason: str,
    ) -> None:
        active = self._active_orchestrators.get(request.execution_id)
        append_gate_event(
            self._chat.event_store,
            execution_id=request.execution_id,
            workspace_id=request.workspace_id,
            event_type=ExecutionEventType.WORKFLOW_GATE_RESOLVED,
            occurred_at=self._clock(),
            from_phase=from_phase.value,
            to_phase=to_phase.value,
            gate=gate,
            reason=reason,
            sub_agent_id=None if active is None else active.sub_agent_id,
            intent_epoch=request.intent_epoch,
        )

    # -- state helpers ------------------------------------------------------

    def _transition(
        self,
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
        target: HypothesisDevelopmentPhase,
        reason: str,
    ) -> HypothesisDevelopmentState:
        advanced = self._advance(state, target, reason)
        self._emit_phase(request, target, {"reason": reason})
        self._on_phase_entered(request, advanced)
        return advanced

    def _transition_gated(
        self,
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
        target: HypothesisDevelopmentPhase,
        reason: str,
        check: Callable[[], str | None],
        *,
        gate: str | None = None,
    ) -> HypothesisDevelopmentState:
        failure = check()
        gate_name = gate or f"{state.phase.value}->{target.value}"
        if failure is not None:
            self._emit_gate_blocked(request, state.phase, target, gate_name, failure)
            raise _Fenced(
                f"gate blocked {state.phase.value} -> {target.value}: {failure}",
                state,
            )
        self._emit_gate_resolved(request, state.phase, target, gate_name, reason)
        return self._transition(request, state, target, reason)

    def _advance(
        self,
        state: HypothesisDevelopmentState,
        target: HypothesisDevelopmentPhase,
        reason: str,
    ) -> HypothesisDevelopmentState:
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

    def _emit_phase(
        self,
        request: HypothesisDevelopmentRequest,
        phase: HypothesisDevelopmentPhase,
        payload: Mapping[str, object],
    ) -> None:
        self._chat.event_store.append(
            ExecutionEvent(
                execution_id=request.execution_id,
                workspace_id=request.workspace_id,
                event_type=ExecutionEventType.WORKFLOW_PHASE,
                correlation_id=request.execution_id,
                actor_id=_WORKFLOW_ACTOR,
                actor_scope=ActorScope.WORKFLOW,
                occurred_at=self._clock(),
                payload={"phase": phase.value, **dict(payload)},
                intent_epoch=request.intent_epoch,
                event_id=self._id_factory("event"),
            )
        )

    def _pause(
        self,
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
        fence: _Fenced,
    ) -> HypothesisDevelopmentState:
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
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
        error: Exception,
    ) -> HypothesisDevelopmentState:
        self._finish_orchestrator(request, status=STATUS_FAILED, detail=str(error))
        self._finish_execution(
            request,
            WorkflowExecutionState.FAILED,
            ExecutionEventType.WORKFLOW_FAILED,
            payload={"errorMessage": str(error), "state": "failed"},
        )
        failed = self._advance(state, _FAILED, f"workflow failed: {error}")
        return replace(failed, error_message=str(error), detail=str(error))

    def _outcome(
        self, state: HypothesisDevelopmentState
    ) -> HypothesisDevelopmentOutcome:
        return HypothesisDevelopmentOutcome(
            workspace_id=state.workspace_id,
            execution_id=state.execution_id,
            phase=state.phase,
            fenced=state.fenced,
            transitions=state.transitions,
            hypothesis=state.hypothesis,
            plan=state.plan,
            trials=state.trials,
            rca=state.rca,
            verdict=state.verdict,
            strategy_bundle=state.strategy_bundle,
            exit_criteria=state.exit_criteria,
            optimization_backtest=state.optimization_backtest,
            journal_id=state.journal_id,
            detail=state.detail,
        )

    # -- durable boundaries -------------------------------------------------

    def _require_execution(
        self, request: HypothesisDevelopmentRequest
    ) -> WorkflowExecution:
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

    def _check_stale_request(self, request: HypothesisDevelopmentRequest) -> None:
        execution = self._chat.get_execution(request.workspace_id, request.execution_id)
        if execution.state in _TERMINAL_EXECUTION_STATES:
            raise WorkflowError(f"execution is already {execution.state.value}")
        if execution.intent_epoch != request.intent_epoch:
            raise _Fenced("request predates newer steering")

    def _require_current(
        self,
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
    ) -> None:
        execution = self._chat.get_execution(request.workspace_id, request.execution_id)
        if execution.state in _TERMINAL_EXECUTION_STATES:
            raise _Fenced(f"execution became {execution.state.value}")
        if execution.intent_epoch != state.intent_epoch:
            raise _Fenced("steering advanced the intent epoch")

    def _finish_execution(
        self,
        request: HypothesisDevelopmentRequest,
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


def _metric(metrics: object | None, observable: str) -> float | None:
    if not isinstance(metrics, Mapping):
        return None
    value = metrics.get(observable)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _digest(payload: Mapping[str, object]) -> str:
    document = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(document).hexdigest()


__all__ = [
    "DEFAULT_MAX_RCA_CYCLES",
    "ExitCriterion",
    "FalsificationComposeError",
    "FalsificationPlan",
    "FalsificationPlanDraft",
    "FalsificationPlanner",
    "FalsificationTrial",
    "FalsificationVerdict",
    "FalsifierTest",
    "HYPOTHESIS_DEVELOPMENT_PHASES",
    "HYPOTHESIS_DEVELOPMENT_WORKFLOW",
    "HypothesisComposer",
    "HypothesisDevelopmentIntakeError",
    "HypothesisDevelopmentOutcome",
    "HypothesisDevelopmentPhase",
    "HypothesisDevelopmentRequest",
    "HypothesisDevelopmentState",
    "HypothesisDevelopmentWorkflow",
    "HypothesisDraft",
    "InvalidPhaseTransition",
    "JsonFalsificationPlanner",
    "JsonDevelopmentHypothesisComposer",
    "JsonRcaComposer",
    "JsonStrategyDesigner",
    "PhaseTransition",
    "PlaceboControl",
    "PlannedTest",
    "RcaComposer",
    "RcaDiagnosis",
    "StrategyContent",
    "StrategyDesignDraft",
    "StrategyDesigner",
    "WorkflowError",
    "check_evidence_bound",
    "check_falsifiability",
    "check_falsification_plan",
    "check_strategy_design",
    "check_verdict_advances",
    "transition_phase",
]
