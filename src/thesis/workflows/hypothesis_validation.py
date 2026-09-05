"""Deterministic hypothesis-validation workflow orchestration.

This module operationalizes user-supplied hypotheses as a Thesis workflow.
It mirrors the strategy-development state machine: the workflow converts a
user claim into a falsifiable specification, selects a point-in-time data
window, runs a harness backtest behind an injected runner port, evaluates the
pre-registered criteria against the recorded metrics, and publishes a labeled
verdict report.

Design rules:

- Workflow transitions are explicit and auditable: every move between phases
  goes through :func:`transition_phase` and is recorded as an immutable
  :class:`PhaseTransition`.
- No wall-clock participates in state decisions.  Time and identifiers are
  injected, the data window is derived from the injected clock only, and
  steering fences are computed from durable intent epochs only.
- Every external effect sits behind an injected port: model invocation through
  :class:`thesis.agent.router.ModelRouter`, data readiness through
  :class:`DataReadinessProbe`, backtests through
  :class:`thesis.workflows.backtests.BacktestRunner`, and durability through
  chat persistence and the command journal.
- The workflow is fully orchestratable without a live model or live market
  data: tests inject a deterministic fake provider, readiness probe, and
  runner.

Verdicts are deliberately conservative.  A verdict of ``supported`` requires
every pre-registered criterion to pass against the recorded metrics;
``refuted`` requires every criterion to fail; anything else — including
missing or non-numeric metrics — is ``inconclusive`` and states that the
evidence is insufficient.  The verdict report reuses the exploratory evidence
report conventions: Phase 1 history is never validated evidence.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol

from thesis.agent.context import assemble_context
from thesis.agent.router import InvocationResult, ModelRouter, Role
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
from thesis.persistence.journal import ArtifactRef, CommandJournal, content_hash
from thesis.strategies.bundles import StrategyBundle, StrategyBundleError
from thesis.workflows.backtests import (
    BacktestRunError,
    BacktestRunner,
    BacktestRunRequest,
    BacktestRunResult,
    HarnessError,
)
from thesis.workflows.evidence import (
    EVIDENCE_GRADE,
    EXPLORATORY_DISCLOSURE,
    HISTORICAL_PHASE_LABEL,
)

_WORKFLOW_ID = "hypothesis-validation"
_WORKFLOW_VERSION = "1.0.0"
_WORKFLOW_TITLE = "Hypothesis validation"
_TOOL_NAME = "harness.backtest"
_TOOL_CORRELATION_PREFIX = "backtest"
_WORKFLOW_ACTOR = "thesis-workflow"
_REPORT_MEDIA_TYPE = "application/json"
_REPORT_SCHEMA = "fca-thesis-hypothesis-verdict-v1"

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
_WORKSPACE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
_SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9._-]{0,15}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")

CLAIM_TYPES: tuple[str, ...] = ("descriptive", "predictive", "causal")
CRITERION_OPERATORS: frozenset[str] = frozenset({"gte", "lte", "gt", "lt"})
DATA_READINESS_CHECKS: tuple[str, ...] = (
    "coverage",
    "calendar",
    "corporateActions",
    "optionMembership",
    "leakage",
)
VERDICT_SUPPORTED = "supported"
VERDICT_REFUTED = "refuted"
VERDICT_INCONCLUSIVE = "inconclusive"
_VERDICTS = frozenset({VERDICT_SUPPORTED, VERDICT_REFUTED, VERDICT_INCONCLUSIVE})

DEFAULT_LOOKBACK = timedelta(days=30)


class WorkflowError(RuntimeError):
    """Base error for invalid hypothesis-validation workflow operations."""


class InvalidPhaseTransition(WorkflowError):
    """A phase cannot move directly to the requested phase."""


class HypothesisIntakeError(WorkflowError):
    """Raised when a hypothesis claim cannot become a valid specification."""


class DataWindowError(WorkflowError):
    """Raised when a point-in-time data window is unusable."""


class _Fenced(Exception):
    """Internal signal that a continuation became stale and must pause.

    Carries the partial state recorded so far so in-flight results are not
    lost when the workflow pauses instead of continuing.
    """

    def __init__(
        self,
        message: str,
        state: HypothesisValidationState | None = None,
    ) -> None:
        super().__init__(message)
        self.state = state


class _PhaseFailure(WorkflowError):
    """Internal failure signal that carries the partial workflow state."""

    def __init__(self, message: str, state: HypothesisValidationState) -> None:
        super().__init__(message)
        self.state = state


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


class HypothesisValidationPhase(StrEnum):
    """The explicit, ordered phases of the hypothesis-validation workflow."""

    REQUESTED = "requested"
    SPECIFYING = "specifying"
    SELECTING = "selecting"
    BACKTESTING = "backtesting"
    RECORDING = "recording"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"


_REQUESTED = HypothesisValidationPhase.REQUESTED
_SPECIFYING = HypothesisValidationPhase.SPECIFYING
_SELECTING = HypothesisValidationPhase.SELECTING
_BACKTESTING = HypothesisValidationPhase.BACKTESTING
_RECORDING = HypothesisValidationPhase.RECORDING
_COMPLETED = HypothesisValidationPhase.COMPLETED
_FAILED = HypothesisValidationPhase.FAILED
_PAUSED = HypothesisValidationPhase.PAUSED

_TRANSITIONS: Mapping[
    HypothesisValidationPhase, frozenset[HypothesisValidationPhase]
] = {
    _REQUESTED: frozenset({_SPECIFYING, _PAUSED, _FAILED}),
    _SPECIFYING: frozenset({_SELECTING, _PAUSED, _FAILED}),
    _SELECTING: frozenset({_BACKTESTING, _PAUSED, _FAILED}),
    _BACKTESTING: frozenset({_RECORDING, _PAUSED, _FAILED}),
    _RECORDING: frozenset({_COMPLETED, _FAILED}),
    _PAUSED: frozenset({_SPECIFYING, _SELECTING, _BACKTESTING, _RECORDING}),
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

HYPOTHESIS_VALIDATION_PHASES: tuple[str, ...] = (
    _REQUESTED.value,
    _SPECIFYING.value,
    _SELECTING.value,
    _BACKTESTING.value,
    _RECORDING.value,
)

HYPOTHESIS_VALIDATION_WORKFLOW = WorkflowDefinition(
    workflow_id=_WORKFLOW_ID,
    version=_WORKFLOW_VERSION,
    title=_WORKFLOW_TITLE,
    phases=HYPOTHESIS_VALIDATION_PHASES,
)


def transition_phase(
    current: HypothesisValidationPhase, target: HypothesisValidationPhase
) -> HypothesisValidationPhase:
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


def _symbols(value: object, label: str) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise WorkflowError(f"{label} must be a sequence of symbol tokens")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise WorkflowError(f"{label} must contain non-empty symbol tokens")
        symbol = item.strip().upper()
        if _SYMBOL.fullmatch(symbol) is None:
            raise WorkflowError(f"{label} contains an unsupported symbol token")
        normalized.append(symbol)
    if not normalized:
        raise WorkflowError(f"{label} must contain at least one symbol")
    return tuple(sorted(dict.fromkeys(normalized)))


@dataclass(frozen=True, slots=True)
class PhaseTransition:
    """One immutable, ordered record of a workflow phase change."""

    sequence: int
    from_phase: HypothesisValidationPhase
    to_phase: HypothesisValidationPhase
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
class HypothesisValidationRequest:
    """The chat request context that triggers one workflow execution."""

    workspace_id: str
    chat_id: str
    execution_id: str
    trigger_message_id: str
    requested_by: str
    claim: str
    intent_epoch: int = 0
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        _workspace_id(self.workspace_id)
        for name in ("chat_id", "execution_id", "trigger_message_id", "requested_by"):
            _identifier(getattr(self, name), name)
        _text(self.claim, "claim", max_length=8_192)
        if self.intent_epoch < 0:
            raise WorkflowError("intent_epoch must not be negative")
        if self.created_at is not None:
            if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
                raise WorkflowError("created_at must be timezone-aware")
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
            "claim": self.claim,
            "createdAt": created_at,
            "executionId": self.execution_id,
            "intentEpoch": self.intent_epoch,
            "requestedBy": self.requested_by,
            "triggerMessageId": self.trigger_message_id,
            "workspaceId": self.workspace_id,
        }


@dataclass(frozen=True, slots=True)
class Criterion:
    """One pre-registered, falsifiable success criterion."""

    metric: str
    operator: str
    threshold: float
    label: str | None = None

    def __post_init__(self) -> None:
        _text(self.metric, "criterion metric", max_length=64)
        object.__setattr__(self, "metric", self.metric.strip())
        if self.operator not in CRITERION_OPERATORS:
            raise HypothesisIntakeError(
                f"criterion operator must be one of {sorted(CRITERION_OPERATORS)}"
            )
        if (
            isinstance(self.threshold, bool)
            or not isinstance(self.threshold, int | float)
            or not math.isfinite(self.threshold)
        ):
            raise HypothesisIntakeError("criterion threshold must be a finite number")
        object.__setattr__(self, "threshold", float(self.threshold))
        if self.label is not None:
            _text(self.label, "criterion label", max_length=128)

    def evaluate(self, observed: float) -> bool:
        """Return whether one observed metric value satisfies the criterion."""
        if self.operator == "gte":
            return observed >= self.threshold
        if self.operator == "lte":
            return observed <= self.threshold
        if self.operator == "gt":
            return observed > self.threshold
        return observed < self.threshold

    def as_dict(self) -> dict[str, object]:
        """Return the portable criterion representation."""
        return {
            "label": self.label,
            "metric": self.metric,
            "operator": self.operator,
            "threshold": self.threshold,
        }


@dataclass(frozen=True, slots=True)
class CriterionEvaluation:
    """The recorded outcome of one criterion against observed metrics."""

    metric: str
    operator: str
    threshold: float
    observed: float
    passed: bool

    def __post_init__(self) -> None:
        Criterion(self.metric, self.operator, self.threshold)
        if (
            isinstance(self.observed, bool)
            or not isinstance(self.observed, int | float)
            or not math.isfinite(self.observed)
        ):
            raise WorkflowError("criterion observed value must be a finite number")
        object.__setattr__(self, "observed", float(self.observed))

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
class HypothesisSpecification:
    """The falsifiable contract converted from one user claim.

    The specification is the nonexploratory record of the inferential fields:
    the claim type distinguishes descriptive, predictive, and causal claims,
    and the variables, hypotheses, estimand, endpoints, data requirements, and
    pre-registered criteria are fixed before any analysis runs.
    """

    claim: str
    claim_type: str
    variables: tuple[str, ...]
    null_hypothesis: str
    alternative_hypothesis: str
    estimand: str
    endpoints: tuple[str, ...]
    data_requirements: Mapping[str, object]
    criteria: tuple[Criterion, ...]
    bundle: StrategyBundle
    limitations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _text(self.claim, "claim", max_length=8_192)
        if self.claim_type not in CLAIM_TYPES:
            raise HypothesisIntakeError(
                f"claim_type must be one of {list(CLAIM_TYPES)}"
            )
        object.__setattr__(
            self,
            "variables",
            _labeled_strings(self.variables, "variables", required=True),
        )
        object.__setattr__(
            self,
            "endpoints",
            _labeled_strings(self.endpoints, "endpoints", required=True),
        )
        _text(self.null_hypothesis, "null_hypothesis", max_length=2_048)
        _text(self.alternative_hypothesis, "alternative_hypothesis", max_length=2_048)
        _text(self.estimand, "estimand", max_length=2_048)
        if not isinstance(self.data_requirements, Mapping):
            raise HypothesisIntakeError("data_requirements must be a mapping")
        object.__setattr__(
            self,
            "data_requirements",
            MappingProxyType(dict(self.data_requirements)),
        )
        try:
            if not self.symbols:
                raise HypothesisIntakeError(
                    "data_requirements must declare at least one symbol"
                )
        except WorkflowError as error:
            if isinstance(error, HypothesisIntakeError):
                raise
            raise HypothesisIntakeError(str(error)) from error
        if not self.criteria:
            raise HypothesisIntakeError(
                "hypothesis specifications require pre-registered criteria"
            )
        if len(set(self.criteria)) != len(self.criteria):
            raise HypothesisIntakeError("criteria must be distinct")
        if not isinstance(self.bundle, StrategyBundle):
            raise HypothesisIntakeError("specification requires a strategy bundle")
        object.__setattr__(
            self,
            "limitations",
            _labeled_strings(self.limitations, "limitations", required=False),
        )

    @property
    def symbols(self) -> tuple[str, ...]:
        """Return the point-in-time symbols required by the claim."""
        return _symbols(self.data_requirements.get("symbols"), "data requirements")

    def as_dict(self) -> dict[str, object]:
        """Return the portable specification representation."""
        return {
            "alternativeHypothesis": self.alternative_hypothesis,
            "bundleManifest": self.bundle.manifest(),
            "claim": self.claim,
            "claimType": self.claim_type,
            "criteria": [criterion.as_dict() for criterion in self.criteria],
            "dataRequirements": dict(self.data_requirements),
            "endpoints": list(self.endpoints),
            "estimand": self.estimand,
            "limitations": list(self.limitations),
            "nullHypothesis": self.null_hypothesis,
            "symbols": list(self.symbols),
            "variables": list(self.variables),
        }


def _labeled_strings(values: object, label: str, *, required: bool) -> tuple[str, ...]:
    if isinstance(values, str) or not isinstance(values, Sequence):
        raise HypothesisIntakeError(f"{label} must be a sequence of strings")
    for item in values:
        if not isinstance(item, str) or not item.strip():
            raise HypothesisIntakeError(f"{label} must contain non-empty strings")
    normalized = tuple(dict.fromkeys(item.strip() for item in values))
    if required and not normalized:
        raise HypothesisIntakeError(f"{label} must contain at least one entry")
    return normalized


class HypothesisComposer(Protocol):
    """Injected boundary that turns model output into a specification."""

    def compose(self, invocation: InvocationResult) -> HypothesisSpecification:
        """Extract the hypothesis specification from one model invocation."""


class JsonHypothesisComposer:
    """Parse the canonical JSON hypothesis envelope from model output.

    The envelope is the deterministic intake contract: the model returns one
    JSON object carrying the claim type, inferential fields, pre-registered
    criteria, data requirements, and the strategy bundle that operationalizes
    the hypothesis.  A live analyst deployment can replace this composer
    through the same protocol.
    """

    def compose(self, invocation: InvocationResult) -> HypothesisSpecification:
        output = invocation.response.output
        if not isinstance(output, str) or not output.strip():
            raise HypothesisIntakeError("model provider returned no inline claim")
        try:
            document = json.loads(output)
        except json.JSONDecodeError as error:
            raise HypothesisIntakeError(
                "model output is not valid JSON hypothesis content"
            ) from error
        if not isinstance(document, dict):
            raise HypothesisIntakeError("hypothesis content must be a JSON object")
        claim_type = document.get("claimType")
        if claim_type not in CLAIM_TYPES:
            raise HypothesisIntakeError(f"claimType must be one of {list(CLAIM_TYPES)}")
        criteria = _criteria(document.get("criteria"))
        return HypothesisSpecification(
            claim=_envelope_text(document.get("claim"), "claim", max_length=8_192),
            claim_type=claim_type,
            variables=_envelope_strings(document.get("variables"), "variables"),
            null_hypothesis=_envelope_text(
                document.get("nullHypothesis"), "nullHypothesis"
            ),
            alternative_hypothesis=_envelope_text(
                document.get("alternativeHypothesis"), "alternativeHypothesis"
            ),
            estimand=_envelope_text(document.get("estimand"), "estimand"),
            endpoints=_envelope_strings(document.get("endpoints"), "endpoints"),
            data_requirements=_envelope_mapping(
                document.get("dataRequirements"), "dataRequirements"
            ),
            criteria=criteria,
            bundle=_bundle(document.get("strategy")),
            limitations=_envelope_strings(
                document.get("limitations"), "limitations", required=False
            ),
        )


def _envelope_text(value: object, label: str, *, max_length: int = 2_048) -> str:
    try:
        return _text(value, label, max_length=max_length)
    except WorkflowError as error:
        raise HypothesisIntakeError(str(error)) from error


def _envelope_strings(
    value: object, label: str, *, required: bool = True
) -> tuple[str, ...]:
    if value is None:
        return _labeled_strings((), label, required=required)
    return _labeled_strings(value, label, required=required)


def _envelope_mapping(value: object, label: str) -> Mapping[str, object]:
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise HypothesisIntakeError(f"{label} must be a JSON object")
    return MappingProxyType(dict(value))


def _criteria(value: object) -> tuple[Criterion, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence) or not value:
        raise HypothesisIntakeError(
            "hypothesis content requires a non-empty criteria list"
        )
    criteria: list[Criterion] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise HypothesisIntakeError("each criterion must be a JSON object")
        label = item.get("label")
        criteria.append(
            Criterion(
                metric=item.get("metric"),
                operator=item.get("operator"),
                threshold=item.get("threshold"),
                label=label if isinstance(label, str) else None,
            )
        )
    return tuple(criteria)


def _bundle(value: object) -> StrategyBundle:
    if not isinstance(value, Mapping):
        raise HypothesisIntakeError(
            "hypothesis content requires a strategy object to operationalize it"
        )
    strategy_id = value.get("strategyId")
    version = value.get("version")
    files = value.get("files")
    tests = value.get("tests")
    if not isinstance(strategy_id, str) or not strategy_id.strip():
        raise HypothesisIntakeError("strategy content requires strategyId")
    if not isinstance(version, str) or not version.strip():
        raise HypothesisIntakeError("strategy content requires a version")
    if not isinstance(files, Mapping) or not files:
        raise HypothesisIntakeError("strategy content requires source files")
    if not isinstance(tests, Mapping) or not tests:
        raise HypothesisIntakeError("strategy content requires tests")
    try:
        return StrategyBundle.create(
            strategy_id=strategy_id,
            version=version,
            source=dict(files),
            tests=dict(tests),
            dependencies=_optional_strings(value.get("dependencies")),
            platform_versions=_optional_mapping(value.get("platformVersions")),
            harness_configuration=_optional_mapping(value.get("harnessConfiguration")),
            risk_declarations=_optional_mapping(value.get("riskDeclarations")),
            capability_declarations=_optional_mapping(
                value.get("capabilityDeclarations")
            ),
            limitations=_optional_strings(value.get("limitations")),
        )
    except StrategyBundleError as error:
        raise HypothesisIntakeError(f"strategy envelope is invalid: {error}") from error


def _optional_mapping(value: object) -> Mapping[str, object]:
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise HypothesisIntakeError("strategy envelope objects must be JSON objects")
    return MappingProxyType(dict(value))


def _optional_strings(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise HypothesisIntakeError("strategy envelope lists must contain strings")
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise HypothesisIntakeError("strategy envelope lists must contain strings")
    return tuple(dict.fromkeys(item.strip() for item in value))


@dataclass(frozen=True, slots=True)
class DataWindow:
    """One deterministic, point-in-time data window.

    ``start`` and ``end`` bound the half-open observation range and ``as_of``
    is the read boundary: reading beyond ``as_of`` would require data that did
    not exist yet, so an ``as_of`` earlier than ``end`` is rejected as a
    lookahead hazard before any analysis runs.
    """

    symbols: Sequence[str]
    start: datetime
    end: datetime
    as_of: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbols", _symbols(self.symbols, "symbols"))
        start = _timestamp(self.start, "start")
        end = _timestamp(self.end, "end")
        as_of = _timestamp(self.as_of, "as_of")
        if end <= start:
            raise DataWindowError("window end must be after window start")
        if as_of < end:
            raise DataWindowError(
                "point-in-time boundary precedes the window end; "
                "reading would require future data (lookahead hazard)"
            )
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)
        object.__setattr__(self, "as_of", as_of)

    def as_dict(self) -> dict[str, object]:
        """Return the portable window representation."""
        return {
            "asOf": _iso(self.as_of),
            "end": _iso(self.end),
            "start": _iso(self.start),
            "symbols": list(self.symbols),
        }


def _timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise DataWindowError(f"{label} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


class DataReadinessProbe(Protocol):
    """Injected boundary that audits one window before analysis."""

    def probe(self, window: DataWindow) -> Mapping[str, object]:
        """Return the named readiness checks for the window."""


class StaticReadinessProbe:
    """Deterministic probe with injected check outcomes.

    Production deployments replace this port with live coverage, calendar,
    corporate-action, option-membership, and leakage audits; the workflow core
    only sees the recorded check results.
    """

    def __init__(self, checks: Mapping[str, object] | None = None) -> None:
        resolved: dict[str, bool] = {name: True for name in DATA_READINESS_CHECKS}
        for key, value in dict(checks or {}).items():
            if not isinstance(value, bool):
                raise DataWindowError("readiness check values must be booleans")
            resolved[key] = value
        self._checks = MappingProxyType(resolved)

    def probe(self, window: DataWindow) -> Mapping[str, object]:
        return dict(self._checks)


@dataclass(frozen=True, slots=True)
class DataReadinessRecord:
    """The recorded outcome of the pre-analysis data readiness gate."""

    checks: Mapping[str, bool]

    def __post_init__(self) -> None:
        if not isinstance(self.checks, Mapping):
            raise WorkflowError("readiness checks must be a mapping")
        for name in DATA_READINESS_CHECKS:
            if name not in self.checks:
                raise WorkflowError(f"readiness check {name!r} is required")
        for key, value in self.checks.items():
            if not isinstance(key, str) or not key.strip():
                raise WorkflowError("readiness check names must be non-empty")
            if not isinstance(value, bool):
                raise WorkflowError("readiness check values must be booleans")
        object.__setattr__(self, "checks", MappingProxyType(dict(self.checks)))

    @property
    def blocking(self) -> tuple[str, ...]:
        """Return the sorted names of the checks that failed."""
        return tuple(sorted(name for name, passed in self.checks.items() if not passed))

    @property
    def passed(self) -> bool:
        """Return whether every recorded check passed."""
        return not self.blocking

    def as_dict(self) -> dict[str, object]:
        """Return the portable readiness representation."""
        return {
            "blocking": list(self.blocking),
            "checks": dict(sorted(self.checks.items())),
            "passed": self.passed,
        }


@dataclass(frozen=True, slots=True)
class HypothesisVerdict:
    """The conservative verdict for one hypothesis against its criteria."""

    outcome: str
    reason: str
    evaluations: tuple[CriterionEvaluation, ...] = ()

    def __post_init__(self) -> None:
        if self.outcome not in _VERDICTS:
            raise WorkflowError(f"verdict outcome must be one of {sorted(_VERDICTS)}")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise WorkflowError("verdict reason must be non-empty")
        for evaluation in self.evaluations:
            if not isinstance(evaluation, CriterionEvaluation):
                raise WorkflowError("verdict evaluations must be recorded outcomes")

    @property
    def insufficient(self) -> bool:
        """Return whether the evidence was insufficient to decide."""
        return self.outcome == VERDICT_INCONCLUSIVE

    def as_dict(self) -> dict[str, object]:
        """Return the portable verdict representation."""
        return {
            "evaluations": [item.as_dict() for item in self.evaluations],
            "outcome": self.outcome,
            "reason": self.reason,
        }


def evaluate_hypothesis(
    criteria: Sequence[Criterion],
    metrics: object | None,
) -> HypothesisVerdict:
    """Evaluate pre-registered criteria against recorded backtest metrics.

    The rule is deterministic: ``supported`` requires every criterion to pass,
    ``refuted`` requires every criterion to fail, and any missing, non-numeric,
    or mixed outcome is ``inconclusive`` because the evidence is insufficient.
    """
    if not criteria:
        raise WorkflowError("hypothesis evaluation requires pre-registered criteria")
    if metrics is None or not isinstance(metrics, Mapping):
        return HypothesisVerdict(
            outcome=VERDICT_INCONCLUSIVE,
            reason="no backtest metrics were recorded",
        )
    evaluations: list[CriterionEvaluation] = []
    for criterion in criteria:
        observed = metrics.get(criterion.metric)
        if (
            isinstance(observed, bool)
            or not isinstance(observed, int | float)
            or not math.isfinite(observed)
        ):
            return HypothesisVerdict(
                outcome=VERDICT_INCONCLUSIVE,
                reason=(
                    f"criterion metric {criterion.metric!r} is missing or "
                    "non-numeric in the recorded metrics"
                ),
            )
        evaluations.append(
            CriterionEvaluation(
                metric=criterion.metric,
                operator=criterion.operator,
                threshold=criterion.threshold,
                observed=float(observed),
                passed=criterion.evaluate(float(observed)),
            )
        )
    if all(evaluation.passed for evaluation in evaluations):
        return HypothesisVerdict(
            outcome=VERDICT_SUPPORTED,
            reason="all pre-registered criteria passed",
            evaluations=tuple(evaluations),
        )
    if not any(evaluation.passed for evaluation in evaluations):
        return HypothesisVerdict(
            outcome=VERDICT_REFUTED,
            reason="all pre-registered criteria failed",
            evaluations=tuple(evaluations),
        )
    return HypothesisVerdict(
        outcome=VERDICT_INCONCLUSIVE,
        reason=(
            "pre-registered criteria disagree; the evidence is insufficient "
            "to support or refute the claim"
        ),
        evaluations=tuple(evaluations),
    )


@dataclass(frozen=True, slots=True)
class HypothesisEvidenceReport:
    """The complete, labeled verdict record for one workflow execution.

    The report reuses the exploratory evidence-report conventions: Phase 1
    history is never validated evidence, the pre-registered criteria are
    preserved unmodified beside their evaluations, and the report links the
    claim, tests, and metrics to reproducible artifacts while stating when
    the evidence is insufficient.
    """

    workspace_id: str
    execution_id: str
    claim: str
    claim_type: str
    variables: tuple[str, ...]
    null_hypothesis: str
    alternative_hypothesis: str
    estimand: str
    endpoints: tuple[str, ...]
    data_requirements: Mapping[str, object]
    criteria: tuple[Criterion, ...]
    verdict_outcome: str
    verdict_reason: str
    verdict_evaluations: tuple[CriterionEvaluation, ...]
    window: Mapping[str, object]
    readiness: Mapping[str, object]
    strategy_id: str
    strategy_version: str
    bundle_sha256: str
    evidence_grade: str = EVIDENCE_GRADE
    historical: bool = True
    phase_label: str = HISTORICAL_PHASE_LABEL
    schema: str = _REPORT_SCHEMA
    backtest_job_id: str | None = None
    backtest_state: str | None = None
    harness_manifest_reference: str | None = None
    metrics: object | None = None
    artifact_references: tuple[Mapping[str, object], ...] = ()
    journal_id: str | None = None
    limitations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "workspace_id", _workspace_id(self.workspace_id))
        object.__setattr__(
            self, "execution_id", _identifier(self.execution_id, "execution_id")
        )
        _text(self.claim, "claim", max_length=8_192)
        if self.claim_type not in CLAIM_TYPES:
            raise WorkflowError(f"claim_type must be one of {list(CLAIM_TYPES)}")
        object.__setattr__(
            self, "strategy_id", _identifier(self.strategy_id, "strategy_id")
        )
        _text(self.strategy_version, "strategy version", max_length=64)
        if (
            not isinstance(self.bundle_sha256, str)
            or _DIGEST.fullmatch(self.bundle_sha256) is None
        ):
            raise WorkflowError("bundle_sha256 must be a lowercase SHA-256 digest")
        if self.evidence_grade != EVIDENCE_GRADE:
            raise WorkflowError(
                f"Phase 1 evidence must carry the {EVIDENCE_GRADE!r} grade"
            )
        if self.historical is not True or self.phase_label != HISTORICAL_PHASE_LABEL:
            raise WorkflowError("Phase 1 evidence must be labeled as history")
        if not self.criteria:
            raise WorkflowError("verdict reports require pre-registered criteria")
        if self.verdict_outcome not in _VERDICTS:
            raise WorkflowError(f"verdict outcome must be one of {sorted(_VERDICTS)}")
        _text(self.verdict_reason, "verdict reason", max_length=2_048)
        if not isinstance(self.window, Mapping) or not isinstance(
            self.readiness, Mapping
        ):
            raise WorkflowError("window and readiness records must be mappings")
        object.__setattr__(self, "window", MappingProxyType(dict(self.window)))
        object.__setattr__(self, "readiness", MappingProxyType(dict(self.readiness)))
        if not isinstance(self.data_requirements, Mapping):
            raise WorkflowError("data_requirements must be a mapping")
        object.__setattr__(
            self,
            "data_requirements",
            MappingProxyType(dict(self.data_requirements)),
        )
        if self.metrics is not None and not isinstance(self.metrics, Mapping):
            raise WorkflowError("metrics must be a JSON-shaped mapping")
        object.__setattr__(
            self,
            "artifact_references",
            tuple(
                MappingProxyType(dict(reference))
                for reference in self.artifact_references
            ),
        )
        object.__setattr__(self, "limitations", _report_limitations(self.limitations))

    def as_dict(self) -> dict[str, object]:
        """Return the portable report representation for transport and Git."""
        return {
            "alternativeHypothesis": self.alternative_hypothesis,
            "artifactReferences": [
                dict(reference) for reference in self.artifact_references
            ],
            "backtestJobId": self.backtest_job_id,
            "backtestState": self.backtest_state,
            "bundleSha256": self.bundle_sha256,
            "claim": self.claim,
            "claimType": self.claim_type,
            "criteria": [criterion.as_dict() for criterion in self.criteria],
            "dataRequirements": dict(self.data_requirements),
            "endpoints": list(self.endpoints),
            "estimand": self.estimand,
            "evidenceGrade": self.evidence_grade,
            "harnessManifestReference": self.harness_manifest_reference,
            "historical": self.historical,
            "journalId": self.journal_id,
            "limitations": list(self.limitations),
            "metrics": self.metrics,
            "nullHypothesis": self.null_hypothesis,
            "phaseLabel": self.phase_label,
            "readiness": dict(self.readiness),
            "schema": self.schema,
            "strategyId": self.strategy_id,
            "strategyVersion": self.strategy_version,
            "variables": list(self.variables),
            "verdict": {
                "evaluations": [
                    evaluation.as_dict() for evaluation in self.verdict_evaluations
                ],
                "outcome": self.verdict_outcome,
                "reason": self.verdict_reason,
            },
            "window": dict(self.window),
            "workspaceId": self.workspace_id,
        }

    def to_bytes(self) -> bytes:
        """Return deterministic report bytes suitable for Git publication."""
        document = self.as_dict()
        return (
            json.dumps(
                document,
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


def _report_limitations(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, str):
        raise WorkflowError("limitations must be a sequence of strings")
    normalized: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise WorkflowError("limitations must contain non-empty strings")
        normalized.append(value.strip())
    if EXPLORATORY_DISCLOSURE not in normalized:
        normalized.append(EXPLORATORY_DISCLOSURE)
    return tuple(sorted(dict.fromkeys(normalized)))


def build_hypothesis_evidence_report(
    *,
    workspace_id: str,
    execution_id: str,
    specification: HypothesisSpecification,
    window: DataWindow,
    readiness: DataReadinessRecord,
    verdict: HypothesisVerdict,
    backtest: BacktestRunResult | None = None,
    journal_id: str | None = None,
) -> HypothesisEvidenceReport:
    """Assemble the labeled verdict report from recorded phase outputs."""
    job_id: str | None = None
    state: str | None = None
    metrics: object | None = None
    manifest_reference: str | None = None
    artifact_references: tuple[Mapping[str, object], ...] = ()
    if backtest is not None:
        job_id = backtest.job_id
        state = backtest.state
        metrics = backtest.metrics
        manifest_reference = _manifest_reference(backtest.manifest)
        if isinstance(backtest.result_artifacts, Sequence) and not isinstance(
            backtest.result_artifacts, str
        ):
            artifact_references = tuple(
                reference
                for reference in backtest.result_artifacts
                if isinstance(reference, Mapping)
            )
    return HypothesisEvidenceReport(
        workspace_id=workspace_id,
        execution_id=execution_id,
        claim=specification.claim,
        claim_type=specification.claim_type,
        variables=specification.variables,
        null_hypothesis=specification.null_hypothesis,
        alternative_hypothesis=specification.alternative_hypothesis,
        estimand=specification.estimand,
        endpoints=specification.endpoints,
        data_requirements=specification.data_requirements,
        criteria=specification.criteria,
        verdict_outcome=verdict.outcome,
        verdict_reason=verdict.reason,
        verdict_evaluations=verdict.evaluations,
        window=window.as_dict(),
        readiness=readiness.as_dict(),
        strategy_id=specification.bundle.strategy_id,
        strategy_version=specification.bundle.version,
        bundle_sha256=specification.bundle.bundle_sha256,
        backtest_job_id=job_id,
        backtest_state=state,
        harness_manifest_reference=manifest_reference,
        metrics=metrics,
        artifact_references=artifact_references,
        journal_id=journal_id,
        limitations=specification.limitations + tuple(specification.bundle.limitations),
    )


def _manifest_reference(manifest: object | None) -> str | None:
    if not isinstance(manifest, Mapping):
        return None
    identifier = manifest.get("manifestId")
    digest = manifest.get("sha256")
    if isinstance(identifier, str) and isinstance(digest, str):
        return f"{identifier}@sha256:{digest}"
    return None


@dataclass(frozen=True, slots=True)
class HypothesisValidationState:
    """The immutable state snapshot after each workflow transition."""

    workspace_id: str
    execution_id: str
    phase: HypothesisValidationPhase
    intent_epoch: int
    transitions: tuple[PhaseTransition, ...] = ()
    specification: HypothesisSpecification | None = None
    window: DataWindow | None = None
    readiness: DataReadinessRecord | None = None
    backtest: BacktestRunResult | None = None
    verdict: HypothesisVerdict | None = None
    report: HypothesisEvidenceReport | None = None
    journal_id: str | None = None
    model_provenance: Mapping[str, object] | None = None
    error_message: str | None = None
    detail: str | None = None
    fenced: bool = False


@dataclass(frozen=True, slots=True)
class HypothesisValidationOutcome:
    """The detached result of one workflow run, safe for UI transport."""

    workspace_id: str
    execution_id: str
    phase: HypothesisValidationPhase
    fenced: bool
    transitions: tuple[PhaseTransition, ...]
    specification: HypothesisSpecification | None = None
    window: DataWindow | None = None
    readiness: DataReadinessRecord | None = None
    backtest: BacktestRunResult | None = None
    verdict: HypothesisVerdict | None = None
    report: HypothesisEvidenceReport | None = None
    journal_id: str | None = None
    detail: str | None = None

    def as_dict(self) -> dict[str, object]:
        """Return the transport representation of the outcome."""
        return {
            "backtest": None if self.backtest is None else self.backtest.as_dict(),
            "detail": self.detail,
            "executionId": self.execution_id,
            "fenced": self.fenced,
            "journalId": self.journal_id,
            "phase": self.phase.value,
            "readiness": None if self.readiness is None else self.readiness.as_dict(),
            "report": None if self.report is None else self.report.as_dict(),
            "specification": None
            if self.specification is None
            else self.specification.as_dict(),
            "transitions": [item.as_dict() for item in self.transitions],
            "verdict": None if self.verdict is None else self.verdict.as_dict(),
            "window": None if self.window is None else self.window.as_dict(),
            "workspaceId": self.workspace_id,
        }


class HypothesisValidationWorkflow:
    """Orchestrate the documented chat-to-verdict hypothesis workflow."""

    def __init__(
        self,
        *,
        chat: ChatService,
        router: ModelRouter,
        runner: BacktestRunner,
        composer: HypothesisComposer | None = None,
        probe: DataReadinessProbe | None = None,
        journal: CommandJournal | None = None,
        clock: Callable[[], datetime] = _utc_now,
        id_factory: Callable[[str], str] = _new_id,
        role: Role = "research",
        experiment_id: str | None = None,
        lookback: timedelta = DEFAULT_LOOKBACK,
    ) -> None:
        self._chat = chat
        self._router = router
        self._runner = runner
        self._composer = JsonHypothesisComposer() if composer is None else composer
        self._probe = StaticReadinessProbe() if probe is None else probe
        self._journal = journal
        self._clock = clock
        self._id_factory = id_factory
        self._role = role
        self._experiment_id = experiment_id
        if lookback <= timedelta(0):
            raise WorkflowError("lookback must be a positive duration")
        self._lookback = lookback

    @property
    def definition(self) -> WorkflowDefinition:
        """Return the versioned workflow definition registered in chat."""
        return HYPOTHESIS_VALIDATION_WORKFLOW

    def launch(
        self,
        workspace_id: str,
        chat_id: str,
        *,
        actor_id: str,
        claim: str,
        investigation_id: str | None = None,
    ) -> HypothesisValidationRequest:
        """Persist the chat trigger and return the runnable request."""
        execution = self._chat.launch_workflow(
            workspace_id,
            chat_id,
            workflow=self.definition,
            actor_id=actor_id,
            body=claim,
            investigation_id=investigation_id,
        )
        return HypothesisValidationRequest(
            workspace_id=workspace_id,
            chat_id=chat_id,
            execution_id=execution.execution_id,
            trigger_message_id=execution.trigger_message_id,
            requested_by=actor_id,
            claim=claim,
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

    def run(self, request: HypothesisValidationRequest) -> HypothesisValidationOutcome:
        """Execute the documented phases once for this request snapshot."""
        state = HypothesisValidationState(
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
            state = self._advance(state, _SPECIFYING, "chat request accepted")
            specification, provenance = self._specify(request)
            state = replace(
                state,
                specification=specification,
                model_provenance=MappingProxyType(dict(provenance)),
            )
            state = self._advance(
                state, _SELECTING, "hypothesis specification registered"
            )
            state = self._select_window(request, state)
            state = self._advance(
                state, _BACKTESTING, "point-in-time data window accepted"
            )
            state = self._backtest(request, state)
            self._require_current(request, state)
            state = self._advance(state, _RECORDING, "harness backtest completed")
            verdict, report, journal_id = self._record(request, state)
            state = replace(
                state, verdict=verdict, report=report, journal_id=journal_id
            )
            self._finish_execution(
                request,
                WorkflowExecutionState.COMPLETED,
                ExecutionEventType.WORKFLOW_COMPLETED,
                payload=self._completion_payload(request, report),
            )
            state = self._advance(state, _COMPLETED, "outcome recorded")
        except _Fenced as fence:
            state = self._pause(
                fence.state if fence.state is not None else state, fence
            )
        except _PhaseFailure as failure:
            state = self._fail(request, failure.state, failure)
        except (
            BacktestRunError,
            DataWindowError,
            HarnessError,
            HypothesisIntakeError,
            WorkflowError,
        ) as error:
            state = self._fail(request, state, error)
        return self._outcome(state)

    # -- phases ------------------------------------------------------------

    def _specify(
        self, request: HypothesisValidationRequest
    ) -> tuple[HypothesisSpecification, dict[str, object]]:
        context = assemble_context(
            workspace_scope=request.workspace_id,
            task=request.claim,
            role=f"Thesis {self._role} hypothesis analyst",
            context_id=request.execution_id,
        )
        invocation = self._router.invoke(self._role, context)
        specification = self._composer.compose(invocation)
        return specification, invocation.provenance.as_dict()

    def _select_window(
        self,
        request: HypothesisValidationRequest,
        state: HypothesisValidationState,
    ) -> HypothesisValidationState:
        specification = state.specification
        if specification is None:
            raise WorkflowError("window selection requires a registered specification")
        self._require_current(request, state)
        now = self._clock()
        window = DataWindow(
            symbols=specification.symbols,
            start=now - self._lookback,
            end=now,
            as_of=now,
        )
        checks = self._probe.probe(window)
        readiness = DataReadinessRecord(checks=_boolean_checks(checks))
        state = replace(state, window=window, readiness=readiness)
        if not readiness.passed:
            raise _PhaseFailure(
                f"data readiness gate failed: {', '.join(readiness.blocking)}",
                state,
            )
        return state

    def _backtest(
        self,
        request: HypothesisValidationRequest,
        state: HypothesisValidationState,
    ) -> HypothesisValidationState:
        specification = state.specification
        if specification is None:
            raise WorkflowError("backtest requires a registered specification")
        self._require_current(request, state)
        correlation_id = f"{_TOOL_CORRELATION_PREFIX}-{request.execution_id}"
        experiment_id = self._experiment_id or f"experiment-{request.execution_id}"
        bundle = specification.bundle
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
            error_message=None if result.succeeded else f"state {result.state!r}",
            intent_epoch=state.intent_epoch,
        )
        state = replace(state, backtest=result)
        try:
            self._require_current(request, state)
        except _Fenced as fence:
            raise _Fenced(str(fence), state) from fence
        if not result.succeeded:
            raise _PhaseFailure(
                f"harness backtest {result.job_id!r} ended in state {result.state!r}",
                state,
            )
        return state

    def _record(
        self,
        request: HypothesisValidationRequest,
        state: HypothesisValidationState,
    ) -> tuple[HypothesisVerdict, HypothesisEvidenceReport, str | None]:
        specification = state.specification
        backtest = state.backtest
        window = state.window
        readiness = state.readiness
        if (
            specification is None
            or window is None
            or readiness is None
            or backtest is None
        ):
            raise WorkflowError("recording requires the completed prior phases")
        verdict = evaluate_hypothesis(specification.criteria, backtest.metrics)
        report = build_hypothesis_evidence_report(
            workspace_id=request.workspace_id,
            execution_id=request.execution_id,
            specification=specification,
            window=window,
            readiness=readiness,
            verdict=verdict,
            backtest=backtest,
            journal_id=(
                None if self._journal is None else f"workflow-{request.execution_id}"
            ),
        )
        journal_id = self._journal_entry(request, state, report)
        return verdict, report, journal_id

    def _journal_entry(
        self,
        request: HypothesisValidationRequest,
        state: HypothesisValidationState,
        report: HypothesisEvidenceReport,
    ) -> str | None:
        if self._journal is None:
            return None
        specification = state.specification
        if specification is None:
            raise WorkflowError("journal recording requires a registered specification")
        definition = self.definition
        journal_id = f"workflow-{request.execution_id}"
        self._journal.accept(
            "workflow.hypothesis_validation.completed",
            {
                "executionId": request.execution_id,
                "requestedBy": request.requested_by,
                "strategyIds": [specification.bundle.strategy_id],
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
            model_hash=content_hash(dict(state.model_provenance or {})),
            artifacts=(
                ArtifactRef.from_bytes(
                    f"reports/{request.execution_id}/hypothesis-verdict.json",
                    report.to_bytes(),
                    media_type=_REPORT_MEDIA_TYPE,
                ),
            ),
            tool_correlation_ids=(
                f"{_TOOL_CORRELATION_PREFIX}-{request.execution_id}",
            ),
            idempotency_id=journal_id,
        )
        return journal_id

    # -- state helpers -----------------------------------------------------

    def _advance(
        self,
        state: HypothesisValidationState,
        target: HypothesisValidationPhase,
        reason: str,
    ) -> HypothesisValidationState:
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
        self, state: HypothesisValidationState, fence: _Fenced
    ) -> HypothesisValidationState:
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
        request: HypothesisValidationRequest,
        state: HypothesisValidationState,
        error: Exception,
    ) -> HypothesisValidationState:
        self._finish_execution(
            request,
            WorkflowExecutionState.FAILED,
            ExecutionEventType.WORKFLOW_FAILED,
            payload={"errorMessage": str(error), "state": "failed"},
        )
        failed = self._advance(state, _FAILED, f"workflow failed: {error}")
        return replace(failed, error_message=str(error), detail=str(error))

    def _outcome(self, state: HypothesisValidationState) -> HypothesisValidationOutcome:
        return HypothesisValidationOutcome(
            workspace_id=state.workspace_id,
            execution_id=state.execution_id,
            phase=state.phase,
            fenced=state.fenced,
            transitions=state.transitions,
            specification=state.specification,
            window=state.window,
            readiness=state.readiness,
            backtest=state.backtest,
            verdict=state.verdict,
            report=state.report,
            journal_id=state.journal_id,
            detail=state.detail,
        )

    # -- durable boundaries -------------------------------------------------

    def _require_execution(
        self, request: HypothesisValidationRequest
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

    def _check_stale_request(self, request: HypothesisValidationRequest) -> None:
        execution = self._chat.get_execution(request.workspace_id, request.execution_id)
        if execution.state in _TERMINAL_EXECUTION_STATES:
            raise WorkflowError(f"execution is already {execution.state.value}")
        if execution.intent_epoch != request.intent_epoch:
            raise _Fenced("request predates newer steering")

    def _require_current(
        self,
        request: HypothesisValidationRequest,
        state: HypothesisValidationState,
    ) -> None:
        execution = self._chat.get_execution(request.workspace_id, request.execution_id)
        if execution.state in _TERMINAL_EXECUTION_STATES:
            raise _Fenced(f"execution became {execution.state.value}")
        if execution.intent_epoch != state.intent_epoch:
            raise _Fenced("steering advanced the intent epoch")

    def _finish_execution(
        self,
        request: HypothesisValidationRequest,
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

    def _completion_payload(
        self,
        request: HypothesisValidationRequest,
        report: HypothesisEvidenceReport | None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {"state": _COMPLETED.value}
        if report is not None:
            payload["report"] = {
                "name": f"reports/{request.execution_id}/hypothesis-verdict.json",
                "sha256": report.content_sha256,
                "schema": _REPORT_SCHEMA,
            }
        return payload


def _boolean_checks(value: Mapping[str, object]) -> dict[str, bool]:
    checks: dict[str, bool] = {}
    for key, outcome in dict(value).items():
        if not isinstance(key, str) or not key.strip():
            raise DataWindowError("readiness check names must be non-empty")
        if not isinstance(outcome, bool):
            raise DataWindowError("readiness check values must be booleans")
        checks[key] = outcome
    return checks


__all__ = [
    "CLAIM_TYPES",
    "CRITERION_OPERATORS",
    "DATA_READINESS_CHECKS",
    "DataReadinessProbe",
    "DataReadinessRecord",
    "DataWindow",
    "DataWindowError",
    "DEFAULT_LOOKBACK",
    "HYPOTHESIS_VALIDATION_PHASES",
    "HYPOTHESIS_VALIDATION_WORKFLOW",
    "HypothesisComposer",
    "HypothesisEvidenceReport",
    "HypothesisIntakeError",
    "HypothesisSpecification",
    "HypothesisValidationOutcome",
    "HypothesisValidationPhase",
    "HypothesisValidationRequest",
    "HypothesisValidationState",
    "HypothesisValidationWorkflow",
    "HypothesisVerdict",
    "InvalidPhaseTransition",
    "JsonHypothesisComposer",
    "PhaseTransition",
    "StaticReadinessProbe",
    "VERDICT_INCONCLUSIVE",
    "VERDICT_REFUTED",
    "VERDICT_SUPPORTED",
    "WorkflowError",
    "build_hypothesis_evidence_report",
    "evaluate_hypothesis",
    "transition_phase",
]
