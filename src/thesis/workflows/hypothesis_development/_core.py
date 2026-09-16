"""Falsification-first hypothesis-development workflow.

This workflow captures the research pipeline the product describes: a user
brings an intuition about a trading opportunity, the agent turns it into a
*statistical model* of the assets involved and asks whether a regularity
matching the intuition exists in historical data, makes a real effort to
destroy the hypothesis by planning and running falsification trials and
negative controls, evaluates the typed trial results in agent-authored prose,
loops a refuted premise back through hypothesis revision, and only then
designs, tests, diagnoses, and optimizes a strategy.

There is no separate falsification RCA sub-agent: once the trials return, the
research sub-agent reads the typed evidence (including every harness or data
failure) and explains in prose what survived, what was refuted, and what went
wrong. Operator-facing text is never a canned workflow string; when the
workflow needs operator direction it asks the model to author the request from
the concrete reason it paused.

The model serves two falsification layers:

* **Layer A** (this pipeline, on history): the model cannot be fit, the fitted
  model shows no regularity that beats a no-structure null, or the regularity
  exists but does not match the intuition (wrong direction, far too weak).
  Any of these refutes the hypothesis, which is adjusted and tried again.
* **Layer B** (live, while a position is open): the strategy recomputes the
  same statistics on a trailing window and exits when they leave the range the
  model showed historically.  A strategy is a falsification instrument: its
  declared exit criteria are falsifier re-checks, and PnL is only a guardrail.

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

    requested -> hypothesis_creation <-> falsification_trials -> verdict
        -> strategy_design <-> strategy_backtesting <-> strategy_rca
        -> optimization -> completed
    plus paused / failed

``hypothesis_creation`` is a durable conversation: the agent proposes the
statistical model itself (assets, estimator, sampling frequency, windows,
sample period), states what the intuition predicts the estimates will look
like versus a no-structure null, defines the Layer A falsifiers and controls,
and recommends advancing.  It infers rather than interrogates: sizing, costs
and execution belong to strategy design. Each conversational turn returns a
structured ``{message, advance}`` judgment; when the model judges the proposal
ready, the structured hypothesis document and falsification plan are composed.

Gates:

* ``hypothesis_creation``: falsifiability.  A non-empty disagreement (what the
  no-structure null predicts instead) and at least one falsifier with
  ``{observable, condition, threshold, test}``, plus at least one falsifier
  trial and one negative/placebo control.  No departure from the null or no
  falsifier cannot advance: without a regularity there is nothing to trade.
* ``falsification_trials -> verdict``: every planned falsifier has bound
  evidence (a recorded run digest and metric).
* ``verdict``: only a not-falsified outcome (``supported`` or ``inconclusive``)
  may advance to ``strategy_design``; ``refuted`` loops back to
  ``hypothesis_creation``.
* ``strategy_design -> strategy_backtesting``: a sandbox-valid strategy whose
  declared exit criteria are phrased as falsifier re-checks (Layer B).
* ``strategy_backtesting`` and ``optimization``: failed runs enter strategy RCA
  and loop back to strategy design, with at most five strategy attempts.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType
from typing import NoReturn, Protocol, TypeVar

from thesis.agent.context import PromptContext, assemble_context
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
from thesis.research.indicators import (
    IndicatorArtifactError,
    IndicatorScope,
    WorkspaceIndicator,
)
from thesis.research.models import (
    FittedModelRef,
    IndicatorBinding,
    ResearchModel,
    ResearchModelError,
    ResearchModelRef,
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
from thesis.workflows.research_models import (
    BacktestResearchModelRunner,
    ResearchModelRunner,
    ResearchModelRunRequest,
    ResearchModelRunResult,
)

#: Canonical falsification verdict outcomes shared by the thesis pipelines.
VERDICT_SUPPORTED = "supported"
VERDICT_REFUTED = "refuted"
VERDICT_INCONCLUSIVE = "inconclusive"

_WORKFLOW_ID = "hypothesis-development"
_WORKFLOW_VERSION = "1.0.0"
_WORKFLOW_TITLE = "Hypothesis development"
_TOOL_NAME = "harness.backtest"
_MODEL_TOOL_NAME = "harness.research_model"
_WORKFLOW_ACTOR = "thesis-workflow"
_HYPOTHESIS_MEDIA_TYPE = "text/markdown"

#: Appended to every composer task so the model emits one bare JSON object.
_JSON_ONLY_INSTRUCTION = (
    "Respond with a single JSON object and nothing else: no prose, no "
    "explanations, and no markdown code fences."
)
_CREATION_TURN_SCHEMA = '{"message": string, "advance": boolean}'

_VERDICT_CHECK_OFF_MARKERS = (
    "check off",
    "approve",
    "approved",
    "accept",
    "accepted",
    "confirm",
    "confirmed",
    "proceed",
    "looks good",
    "continue",
    "yes",
)
_VERDICT_CIRCLE_BACK_MARKERS = (
    "circle back",
    "revise",
    "revision",
    "revisit",
    "go back",
    "return",
    "disagree",
    "not ready",
)

_VERDICTS = frozenset({VERDICT_SUPPORTED, VERDICT_REFUTED, VERDICT_INCONCLUSIVE})

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
_WORKSPACE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")

#: Composed value returned by a repair-aware composer call.
_ComposedT = TypeVar("_ComposedT")

DEFAULT_MAX_HYPOTHESIS_CYCLES = 5
DEFAULT_MAX_RCA_CYCLES = 5
DEFAULT_MAX_COMPOSE_REPAIRS = 2

#: Minimum fields a good-faith model reply must carry; everything else is
#: defaulted so tolerant extraction never turns a partial answer into a failure.
_DEFAULT_HORIZON = "unspecified"
_DEFAULT_RISK_FACTOR = "unspecified"
_DEFAULT_UNIVERSE: tuple[str, ...] = ("UNSPECIFIED",)

#: Concise JSON shapes echoed back to the model during a compose repair.
_HYPOTHESIS_SCHEMA = (
    '{"claim": string, "disagreement": string, "riskFactor": string, '
    '"horizon": string, "universe": [string], "falsifiers": [{"falsifierId": '
    'string, "observable": string, "condition": "lt"|"lte"|"gt"|"gte", '
    '"threshold": number, "test": string}], "sizing": string, "body": string, '
    '"model": {"modelId": string, "version": string, "description": string, '
    '"source": string, "fitEntrypoint": string, "observeEntrypoint": string, '
    '"outputs": [string], "tests": '
    '{path: string}, "harnessConfiguration": {"symbols": [string]}, "indicators": [{"alias": '
    'string, "scope": "global"|"workspace", "name": string, "symbol": string, '
    '"parameters": {}, "contentSha256": string|null, "definition": object|null}]}}'
)
_FALSIFICATION_PLAN_SCHEMA = (
    '{"tests": [{"testId": string, "falsifierId": string, "description": '
    'string}], "controls": [{"controlId": string, "description": string, '
    '"expectation": string}]}'
)
_RCA_SCHEMA = (
    '{"falsifierId": string, '
    '"failureClass": "harness"|"data"|"implementation"|"strategy"|"market_regime", '
    '"rationale": string, "confidence": number, "followUps": [string]}'
)
_STRATEGY_DESIGN_SCHEMA = (
    '{"strategyId": string, "version": string, "entrypoint": string, '
    '"files": {path: string}, '
    '"tests": {path: string}, "harnessConfiguration": {}, "exitCriteria": '
    '[{"falsifierId": string, "observable": string, "condition": '
    '"lt"|"lte"|"gt"|"gte", "threshold": number, "description": string}], '
    '"limitations": [string]}'
)


class WorkflowError(RuntimeError):
    """Base error for invalid hypothesis-development workflow operations."""


class InvalidPhaseTransition(WorkflowError):
    """A phase cannot move directly to the requested phase."""


class HypothesisDevelopmentIntakeError(WorkflowError):
    """Raised when a hypothesis-development request is not valid intake."""


class FalsificationComposeError(WorkflowError):
    """Raised when model output cannot become falsification content."""


@dataclass(frozen=True, slots=True)
class CreationTurn:
    """Structured conversational judgment for the hypothesis-creation phase."""

    message: str
    advance: bool

    def __post_init__(self) -> None:
        if not isinstance(self.message, str) or not self.message.strip():
            raise FalsificationComposeError("creation turn message must be non-empty")
        if not isinstance(self.advance, bool):
            raise FalsificationComposeError("creation turn advance must be boolean")
        object.__setattr__(self, "message", self.message.strip())


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
    sanitized = value.replace("\r\n", "\n").replace("\r", "\n")
    if len(sanitized) > max_length:
        raise WorkflowError(
            f"{label} exceeds maximum length of {max_length} characters"
        )
    if any(ord(character) < 32 and character not in "\n\t" for character in sanitized):
        raise WorkflowError(f"{label} contains unsupported control characters")
    return sanitized


def _verdict_review_action(body: str) -> str | None:
    """Classify explicit operator input for the verdict checkpoint."""
    lowered = body.strip().lower()
    if not lowered:
        return None
    if any(marker in lowered for marker in _VERDICT_CIRCLE_BACK_MARKERS):
        return "circle_back"
    if any(marker in lowered for marker in _VERDICT_CHECK_OFF_MARKERS):
        return "check_off"
    return None


class HypothesisDevelopmentPhase(StrEnum):
    """The explicit, ordered phases of the hypothesis-development workflow."""

    REQUESTED = "requested"
    HYPOTHESIS_CREATION = "hypothesis_creation"
    FALSIFICATION_TRIALS = "falsification_trials"
    # Retained to decode historical execution events. New runs use STRATEGY_RCA.
    RCA = "rca"
    VERDICT = "verdict"
    STRATEGY_DESIGN = "strategy_design"
    STRATEGY_BACKTESTING = "strategy_backtesting"
    STRATEGY_RCA = "strategy_rca"
    OPTIMIZATION = "optimization"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"


_REQUESTED = HypothesisDevelopmentPhase.REQUESTED
_HYPOTHESIS_CREATION = HypothesisDevelopmentPhase.HYPOTHESIS_CREATION
_FALSIFICATION_TRIALS = HypothesisDevelopmentPhase.FALSIFICATION_TRIALS
_RCA = HypothesisDevelopmentPhase.RCA
_VERDICT = HypothesisDevelopmentPhase.VERDICT
_STRATEGY_DESIGN = HypothesisDevelopmentPhase.STRATEGY_DESIGN
_STRATEGY_BACKTESTING = HypothesisDevelopmentPhase.STRATEGY_BACKTESTING
_STRATEGY_RCA = HypothesisDevelopmentPhase.STRATEGY_RCA
_OPTIMIZATION = HypothesisDevelopmentPhase.OPTIMIZATION
_COMPLETED = HypothesisDevelopmentPhase.COMPLETED
_FAILED = HypothesisDevelopmentPhase.FAILED
_PAUSED = HypothesisDevelopmentPhase.PAUSED

_ACTIVE_PHASES = (
    _HYPOTHESIS_CREATION,
    _FALSIFICATION_TRIALS,
    _VERDICT,
    _STRATEGY_DESIGN,
    _STRATEGY_BACKTESTING,
    _STRATEGY_RCA,
    _OPTIMIZATION,
)

#: Role family and human label for each phase's top-level orchestrator.
_PHASE_ORCHESTRATORS: Mapping[HypothesisDevelopmentPhase, tuple[str, str]] = {
    _HYPOTHESIS_CREATION: (
        "research",
        "Thesis hypothesis-creation orchestrator",
    ),
    _FALSIFICATION_TRIALS: (
        "research",
        "Thesis research falsification trials orchestrator",
    ),
    _VERDICT: ("research", "Thesis research verdict orchestrator"),
    _STRATEGY_DESIGN: ("coding", "Thesis coding strategy-design orchestrator"),
    _STRATEGY_BACKTESTING: (
        "coding",
        "Thesis strategy-backtesting orchestrator",
    ),
    _STRATEGY_RCA: ("critique", "Thesis strategy root-cause orchestrator"),
    _OPTIMIZATION: ("coding", "Thesis coding optimization orchestrator"),
}

#: What each phase orchestrator is prompted to do, recorded as visible thinking
#: so the operator can see the phase's intent even when it makes no model call.
_PHASE_TASKS: Mapping[HypothesisDevelopmentPhase, str] = {
    _HYPOTHESIS_CREATION: (
        "Turn the operator's intuition into one statistical model of the assets "
        "involved, estimated on historical data: the regularity it predicts, the "
        "no-structure null it must beat, the Layer A falsifiers that would refute "
        "it, and the negative controls."
    ),
    _FALSIFICATION_TRIALS: (
        "Call the harness research-model MCP tool for the planned falsification "
        "trials, inspect its typed result, and explain the empirical findings."
    ),
    _VERDICT: "Record the falsification verdict from the trial results.",
    _STRATEGY_DESIGN: (
        "Implement a sandbox-valid strategy whose exits re-check the "
        "hypothesis falsifiers."
    ),
    _STRATEGY_BACKTESTING: (
        "Run the designed strategy through the harness and record its test result."
    ),
    _STRATEGY_RCA: (
        "Diagnose the failed strategy backtest and prescribe a concrete revision."
    ),
    _OPTIMIZATION: "Run the optimization backtest for the designed strategy.",
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
    _REQUESTED: frozenset({_HYPOTHESIS_CREATION, _PAUSED, _FAILED}),
    _HYPOTHESIS_CREATION: frozenset({_FALSIFICATION_TRIALS, _PAUSED, _FAILED}),
    _FALSIFICATION_TRIALS: frozenset({_VERDICT, _PAUSED, _FAILED}),
    _RCA: frozenset({_FALSIFICATION_TRIALS, _HYPOTHESIS_CREATION, _PAUSED, _FAILED}),
    _VERDICT: frozenset({_STRATEGY_DESIGN, _HYPOTHESIS_CREATION, _PAUSED, _FAILED}),
    _STRATEGY_DESIGN: frozenset({_STRATEGY_BACKTESTING, _PAUSED, _FAILED}),
    _STRATEGY_BACKTESTING: frozenset({_OPTIMIZATION, _STRATEGY_RCA, _PAUSED, _FAILED}),
    _STRATEGY_RCA: frozenset({_STRATEGY_DESIGN, _PAUSED, _FAILED}),
    _OPTIMIZATION: frozenset({_COMPLETED, _STRATEGY_RCA, _PAUSED, _FAILED}),
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
    _HYPOTHESIS_CREATION.value,
    _FALSIFICATION_TRIALS.value,
    _VERDICT.value,
    _STRATEGY_DESIGN.value,
    _STRATEGY_BACKTESTING.value,
    _STRATEGY_RCA.value,
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

    @classmethod
    def from_execution(
        cls, chat: ChatService, execution: WorkflowExecution
    ) -> HypothesisDevelopmentRequest:
        """Rebuild the runnable request for a durable execution.

        Used when an operator message resumes the hypothesis-creation
        conversation: the original idea is recovered from the trigger message
        and everything else from the execution record.
        """
        try:
            trigger = chat.get_message(
                execution.workspace_id, execution.trigger_message_id
            )
            ideas = _ideas_from_trigger(trigger.body)
        except (LookupError, ValueError):
            ideas = "Continue the hypothesis-creation conversation."
        return cls(
            workspace_id=execution.workspace_id,
            chat_id=execution.chat_id,
            execution_id=execution.execution_id,
            trigger_message_id=execution.trigger_message_id,
            requested_by=execution.requested_by,
            ideas=ideas,
            intent_epoch=execution.intent_epoch,
            created_at=execution.created_at,
        )


def _ideas_from_trigger(body: str) -> str:
    """Recover the operator's original idea from the workflow trigger message."""
    text = body.strip()
    if text.startswith("{"):
        try:
            document = json.loads(text)
        except json.JSONDecodeError:
            return text or "Continue the hypothesis-creation conversation."
        if isinstance(document, Mapping):
            ideas = document.get("ideas")
            if isinstance(ideas, str) and ideas.strip():
                return ideas.strip()
    return text or "Continue the hypothesis-creation conversation."


@dataclass(frozen=True, slots=True)
class StrategyContent:
    """Content-addressed strategy content authored after Layer A survives."""

    strategy_id: str
    version: str
    source: Mapping[str, str]
    tests: Mapping[str, str]
    entrypoint: str = "Strategy"
    harness_configuration: Mapping[str, object] = field(default_factory=dict)
    dependencies: tuple[str, ...] = ()
    limitations: tuple[str, ...] = ()
    harness_manifest_reference: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.strategy_id, "strategy_id")
        _text(self.version, "strategy version", max_length=64)
        if not isinstance(self.entrypoint, str) or not self.entrypoint.isidentifier():
            raise FalsificationComposeError(
                "strategy entrypoint must be a Python class identifier"
            )
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
    """The falsifiable hypothesis and its separate statistical model."""

    claim: str
    disagreement: str
    risk_factor: str
    horizon: str
    universe: tuple[str, ...]
    falsifiers: tuple[HypothesisFalsifier, ...]
    model: ResearchModel
    sizing: str | None = None
    body: str = ""
    hypothesis_id: str | None = None

    def __post_init__(self) -> None:
        _text(self.claim, "claim", max_length=8_192)
        _text(self.disagreement, "disagreement", max_length=8_192)
        _text(self.risk_factor, "risk_factor", max_length=4_096)
        _text(self.horizon, "horizon", max_length=4_096)
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
        if not isinstance(self.model, ResearchModel):
            raise FalsificationComposeError(
                "hypothesis draft requires a research model"
            )
        if self.sizing is not None:
            _text(self.sizing, "sizing", max_length=4_096)
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
    detail: str | None = None

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
        if self.detail is not None:
            _text(self.detail, "trial detail", max_length=4_096)

    def as_dict(self) -> dict[str, object]:
        """Return the portable trial representation."""
        return {
            "detail": self.detail,
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
            "hypothesis requires a non-empty disagreement: state what the "
            "no-structure null predicts and why the intuition departs from it; "
            "without a departure there is no regularity to trade"
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


def check_research_model(
    model: ResearchModel, front_matter: HypothesisFrontMatter
) -> str | None:
    """Return the failure reason when Layer A cannot observe its falsifiers."""
    declared_outputs = set(model.outputs)
    missing = sorted(
        falsifier.observable
        for falsifier in front_matter.falsifiers
        if falsifier.observable not in declared_outputs
    )
    if missing:
        return (
            "research model must publish every falsifier observable; missing model "
            f"outputs: {missing}"
        )
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


def _event_output_text(payload: Mapping[str, object]) -> str | None:
    """Return the visible text carried by one sub-agent output payload."""
    text = payload.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    artifact = payload.get("artifact")
    if isinstance(artifact, Mapping):
        content = artifact.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
    return None


def _conversation_lines(
    ideas: str, conversation: Sequence[tuple[str, str]]
) -> list[str]:
    lines = [f"Operator's original idea:\n{ideas}", "", "Conversation so far:"]
    for role, text in conversation:
        lines.append(f"{role}: {text}")
    return lines


#: Shared framing for both hypothesis-creation prompts: the phase builds a
#: statistical model of the operator's intuition and tests it on history.
_CREATION_MISSION = (
    "You are the hypothesis-creation agent of a falsification-first research "
    "pipeline. The operator brings an intuition about a trading opportunity. "
    "Your job is to turn that intuition into ONE statistical model of the "
    "assets involved and to decide, from HISTORICAL data, whether a regularity "
    "matching the intuition actually exists. You are not designing, sizing, or "
    "costing a trade yet, and you never propose a forward-looking experiment: "
    "the model is estimated and falsified on history that is available today."
)

#: The two falsification layers the model serves.
_CREATION_LAYERS = (
    "Layer A (this pipeline, on history): the model cannot be fit, the fitted "
    "model shows no regularity that beats a no-structure null, or the "
    "regularity exists but points the wrong way or is far weaker than the "
    "intuition claims. Any of these refutes the hypothesis, which then gets "
    "adjusted and tried again.",
    "Markets are noisy, so Layer A asks whether any stable regularity survives "
    "reasonable attempts to falsify it; it does not demand strategy-grade "
    "performance. Use uncertainty-aware, conservative rejection thresholds. A "
    "modest regularity that has the predicted sign, is distinguishable from the "
    "no-structure null, and survives controls is a successful research result. "
    "Do not refute it merely because it misses an ambitious effect size, R-squared, "
    "hit rate, Sharpe, or conventional significance cutoff. Treat borderline "
    "evidence as inconclusive, not refuted. Economic strength, costs, and trade "
    "quality are evaluated later in strategy design.",
    "Layer B (later, while a position is open): the same statistics are "
    "recomputed on a trailing window; if they leave the range the model showed "
    "historically, the idea behind the entry has been refuted and the position "
    "exits. The model you build here is the instrument for both layers.",
)

#: Model families the agent may reach for; the list is illustrative, not a
#: menu the operator must choose from.
_CREATION_MODEL_FAMILIES = (
    "- Two or more assets ('A tracks B but underperforms it', 'A and B look "
    "cointegrated, can we trade mean reversion?'): regress A's returns on B's "
    "(alpha, beta, R^2) over the full sample and rolling windows; the "
    "log-price spread A - beta*B with a stationarity/cointegration test and a "
    "mean-reversion half-life; the drift and hit rate of the relative return.",
    "- Structure of one asset's price action ('bitcoin is trendy', 'A cycles "
    "between peaks and troughs'): return autocorrelation at several lags, "
    "variance-ratio tests, a Hurst exponent, the hit rate of a simple "
    "trend-following rule; for periodicity, the periodogram or spectral peak "
    "of the detrended series and its autocorrelation at the claimed period, "
    "against a random-walk baseline.",
    "- Options and volatility ('vol is rich/cheap when ...'): implied minus "
    "subsequently realized volatility conditional on the regime, the variance "
    "risk premium, skew or term-structure slope versus realized outcomes, "
    "delta-hedged option PnL as the estimator.",
)

_CREATION_TURN_INSTRUCTIONS = (
    "Work like this:",
    "1. Restate the intuition as the regularity it implies, in statistical "
    "terms, and decide which assets belong in the model, adding a benchmark or "
    "control series when one is natural. Infer the assets from the idea; ask "
    "only when a required asset is genuinely ambiguous, and propose a default "
    "even then.",
    "2. Propose the model yourself. Choose the estimator, the sampling "
    "frequency, the lookback/rolling windows and the sample period, and give a "
    "one-clause reason for each choice. Do not ask the operator which "
    "statistics, tests, windows or thresholds to use: that is your job. Be "
    "concrete and creative; if several models fit, name the primary one and "
    "the alternative. Model families you may reach for:",
    *_CREATION_MODEL_FAMILIES,
    "3. State what the intuition PREDICTS the estimates will look like "
    "(direction and rough magnitude) and what a no-structure null "
    "(independence, random walk, fairly priced volatility) would produce "
    "instead.",
    "4. Define the Layer A falsifiers: numeric conditions on the estimated "
    "model that, if observed in history, refute the intuition. Each names an "
    "observable (a statistic the model produces), a comparison (lt, lte, gt or "
    "gte), a threshold and the exact computation. Cover the failure modes: the "
    "model cannot be fit or the sample is too short; the fit does not beat the "
    "null; the regularity points the wrong way or is too weak. Add at least "
    "one negative control (shuffled or permuted returns, an unrelated asset "
    "pair, a placebo period) that should show no regularity. Calibrate thresholds "
    "to sampling noise with confidence intervals, bootstrap/permutation baselines, "
    "or similarly robust uncertainty estimates. Falsify only when evidence supports "
    "no stable regularity or the wrong direction; do not use tight aspirational "
    "trading-performance thresholds as Layer A falsifiers.",
    "5. Say which observables double as Layer B monitors and on what trailing "
    "window. Sizing, costs, borrow, financing, execution and portfolio "
    "construction belong to strategy design; do not ask about them now.",
    "Return a single JSON object with exactly {message, advance}. The message "
    "is concise operator-facing prose: lead with the proposed model, not with "
    "questions. Format the message in clear Markdown with bullet-point lists "
    "under three distinct sections: '### Model' (regularity predicted vs null, "
    "estimator specification, equations), '### Data' (assets/universe, sampling "
    "frequency, sample window, minimum session counts), and '### Falsifiers & "
    "Controls' (Layer A falsifiers list with observable/threshold, negative controls, "
    "and Layer B live monitors). The boolean advance is your judgment: set it true "
    "only when the model, expected estimates, at least one falsifier, and at least one "
    "control are sufficiently specified for operator review; set it false when "
    "another research turn is needed. On the first agent turn this value reports "
    "readiness only and the workflow always waits for operator input. On later "
    "turns, judge whether the operator's latest prose confirms the proposal: set "
    "advance true for clear acceptance in any wording, or false when the operator "
    "provides feedback, asks a question, or remains ambiguous. The exact response "
    "schema is:",
    _CREATION_TURN_SCHEMA,
)

_CREATION_FINALIZE_INSTRUCTIONS = (
    "Return one JSON object with these fields:",
    "- claim: one single-line sentence stating the regularity the intuition "
    "predicts, in model terms.",
    "- disagreement: one single-line sentence stating what the no-structure "
    "null predicts instead and why the intuition departs from it. Without a "
    "departure there is no regularity to trade.",
    "- riskFactor: the main reason the regularity could be spurious or regime-bound.",
    "- horizon: the sampling frequency and the estimation window(s) of the model.",
    "- universe: the ticker symbols in the model, including any benchmark or "
    "control series.",
    "- falsifiers: the Layer A falsifiers. Each has falsifierId, observable (a "
    "snake_case identifier for the statistic the model produces), condition "
    "(lt, lte, gt or gte), threshold (a number) and test (the exact "
    "computation: data, frequency, sample period, method). A falsifier fires "
    "when the observed value satisfies condition/threshold, and a fired "
    "falsifier refutes the claim. Include at least one that catches 'the model "
    "cannot be fit or shows no regularity' (e.g. data_ok < 1.0 or sample_valid < 1.0) and one that catches 'the "
    "regularity points the wrong way or is too weak'. State in the test text "
    "which falsifiers double as Layer B monitors and on what trailing window.",
    "- body: Markdown with the full model specification: variables and "
    "transformations, the estimator, the expected estimates versus the null, "
    "the negative controls, the Layer B monitoring rule and known limitations.",
    "- model (required): a statistical research model, NOT a strategy. Include "
    "modelId, semantic version, description, source defining "
    "fit(context) -> dict[str, number] and observe(fit, context) -> "
    "dict[str, number], fitEntrypoint='fit', observeEntrypoint='observe', "
    "outputs "
    "listing every falsifier observable, tests, and harnessConfiguration with "
    "symbols listing every required symbol. The plain context contains quotes, "
    "trades, trade_prices and returns by symbol; it exposes no orders, positions or "
    "sizing. The model source may import only math, statistics and numpy. If "
    "sample data is missing, unidentifiable, or inadequate, fit and observe must "
    "return explicit numeric failure indicators (such as data_ok=0.0 or sample_valid=0.0) "
    "rather than crashing with unhandled exceptions, so that falsifiers can cleanly observe "
    "and record the empirical failure.",
    "- model.indicators (optional): explicitly scoped reusable inputs. A global "
    "binding names a built-in indicator (scope, name, alias, symbol, parameters). "
    "A workspace binding additionally carries either contentSha256 for an "
    "already-published workspace indicator or a definition with name, version, "
    "description, source defining calculate(context, parameters), entrypoint, "
    "parameterSchema and tests. Never encode an indicator as a strategy.",
    "Omit sizing; it belongs to strategy design. Do not include operator-facing prose.",
)


def _creation_turn_prompt(
    ideas: str, conversation: Sequence[tuple[str, str]], *, first_turn: bool
) -> str:
    lines = [
        _CREATION_MISSION,
        "",
        *_CREATION_LAYERS,
        "",
        *_conversation_lines(ideas, conversation),
    ]
    if first_turn:
        lines.append("(no agent turn yet)")
    lines.extend(["", *_CREATION_TURN_INSTRUCTIONS])
    return "\n".join(lines)


def _creation_finalize_prompt(
    ideas: str, conversation: Sequence[tuple[str, str]]
) -> str:
    lines = [
        "Produce the FINAL hypothesis agreed in this conversation as a "
        "statistical model that is estimated and falsified on historical data. "
        "Treat the operator's idea and every turn below as the source of truth; "
        "fill any remaining gap with the model you proposed, never with a "
        "placeholder.",
        "",
        *_CREATION_LAYERS,
        "",
        *_conversation_lines(ideas, conversation),
        "",
        *_CREATION_FINALIZE_INSTRUCTIONS,
    ]
    return "\n".join(lines)


def _falsification_turn_prompt(
    state: HypothesisDevelopmentState,
    metrics: Mapping[str, object],
) -> str:
    """Task prompt asking the falsification sub-agent to author its empirical analysis."""
    del metrics
    claim = state.hypothesis.front_matter.claim if state.hypothesis else ""
    disagreement = (
        state.hypothesis.front_matter.disagreement if state.hypothesis else ""
    )
    universe = (
        ", ".join(state.hypothesis.front_matter.universe) if state.hypothesis else ""
    )
    horizon = state.hypothesis.front_matter.horizon if state.hypothesis else ""

    lines = [
        "You are the Thesis research falsification trials orchestrator sub-agent.",
        "The empirical falsification evaluation has executed against historical data.",
        "You are the agent that evaluates these trials: there is no separate "
        "diagnosis step. Read the typed results below, including any run state or "
        "failure message, and explain in prose exactly what survived, what was "
        "refuted, and what went wrong.",
        "If a trial produced no observation because the harness or the data failed, "
        "say so plainly, name the observable and the failure text, and state whether "
        "the operator's direction is needed before the verdict can be trusted. Do not "
        "invent observations, and do not treat a failed run as a refutation.",
        "",
        f"Hypothesis Claim: {claim}",
        f"Null / Disagreement: {disagreement}",
        f"Universe: {universe} | Horizon: {horizon}",
        "",
        "Empirical Trial Observables & Results:",
    ]
    tests_by_id = {t.test_id: t for t in (state.plan.tests if state.plan else ())}
    for t in state.trials:
        planned = tests_by_id.get(t.test_id)
        cond = planned.condition if planned else ""
        thresh = _format_observed(planned.threshold if planned else 0.0)
        obs = _format_observed(t.observed)
        if t.observed is None:
            status = f"NO OBSERVATION (run state {t.run_state})"
        elif t.falsified:
            status = "REFUTED"
        else:
            status = "SURVIVED (not refuted)"
        desc = (
            planned.description if planned and planned.description else t.falsifier_id
        )
        lines.append(
            f"- Test {t.test_id} ({desc}): Observed {obs} vs threshold {cond} {thresh} -> {status}"
        )
        if t.detail:
            lines.append(f"  Failure detail: {t.detail}")

    if state.plan and state.plan.controls:
        lines.append("")
        lines.append("Negative & Placebo Controls:")
        for c in state.plan.controls:
            lines.append(
                f"- {c.control_id}: {c.description} (Expected: {c.expectation})"
            )

    lines.extend(
        [
            "",
            "Format your response in clean Markdown with appropriate headers and bullet points.",
            "Lead directly with your empirical takeaways; do not output raw JSON or code blocks.",
        ]
    )
    return "\n".join(lines)


def _hypothesis_context_lines(
    document: HypothesisDocument,
    model_reference: str | None = None,
    fitted_reference: str | None = None,
) -> list[str]:
    """Render the finalized model so later phases work from it, not the claim.

    The declared falsifier ids are listed explicitly: the falsification planner
    must bind every test to one of them, and the strategy author must phrase
    every exit criterion as a re-check of one of them (Layer B).
    """
    front = document.front_matter
    lines = [
        f"Claim: {front.claim}",
        f"Null / disagreement: {front.disagreement}",
        f"Horizon: {front.horizon}",
        f"Universe: {', '.join(front.universe)}",
    ]
    if model_reference is not None:
        lines.append(f"Workspace research model: {model_reference}")
    if fitted_reference is not None:
        lines.append(f"Frozen Layer A fit: {fitted_reference}")
    lines.append("Declared falsifiers:")
    for falsifier in front.falsifiers:
        lines.append(
            f"- {falsifier.falsifier_id or falsifier.observable}: "
            f"{falsifier.observable} {falsifier.condition} "
            f"{falsifier.threshold:g} :: {falsifier.test}"
        )
    if document.body:
        lines.extend(["Model specification:", document.body])
    return lines


class StrategyDesigner(Protocol):
    """Injected boundary that turns model output into a strategy design."""

    def compose(self, invocation: InvocationResult) -> StrategyDesignDraft: ...


def _balanced_objects(text: str) -> Iterator[str]:
    """Yield every balanced top-level ``{...}`` slice, in source order.

    The scan respects JSON string quoting and escapes so braces inside string
    literals never affect the nesting depth.
    """
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    yield text[start : index + 1]
                    break
        start = text.find("{", start + 1)


_FENCED_JSON = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def _extract_json_document(output: str) -> object | None:
    """Best-effort extraction of a JSON document from tolerant model output.

    Tried in order: the raw output as-is; every fenced code block (``json`` or
    bare); then each balanced top-level object.  Returns ``None`` when no
    candidate parses as JSON.
    """
    candidates: list[str] = [output]
    candidates.extend(match.group(1) for match in _FENCED_JSON.finditer(output))
    candidates.extend(_balanced_objects(output))
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def _json_document(invocation: InvocationResult, label: str) -> dict[str, object]:
    output = invocation.response.output
    if not isinstance(output, str) or not output.strip():
        raise FalsificationComposeError(f"model provider returned no {label} content")
    document = _extract_json_document(output)
    if document is None:
        raise FalsificationComposeError(
            f"model output is not valid JSON {label} content"
        )
    if not isinstance(document, dict):
        raise FalsificationComposeError(f"{label} content must be a JSON object")
    return document


def _compose_text(document: Mapping[str, object], key: str, label: str) -> str:
    try:
        return _text(document.get(key), label, max_length=8_192)
    except WorkflowError as error:
        raise FalsificationComposeError(str(error)) from error


def _optional_compose_text(
    document: Mapping[str, object],
    key: str,
    default: str,
    *,
    max_length: int,
) -> str:
    """Return a trimmed optional field or ``default`` when it is absent/blank."""
    value = document.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    if isinstance(value, str) and len(value) > max_length:
        value = value[:max_length].strip()
    try:
        return _text(value, key, max_length=max_length)
    except WorkflowError as error:
        raise FalsificationComposeError(str(error)) from error


class JsonCreationTurnComposer:
    """Parse the model's conversational message and advancement judgment."""

    def compose(self, invocation: InvocationResult) -> CreationTurn:
        document = _json_document(invocation, "creation turn")
        message = _compose_text(document, "message", "message")
        advance = document.get("advance")
        if not isinstance(advance, bool):
            raise FalsificationComposeError("creation turn advance must be boolean")
        return CreationTurn(message=message, advance=advance)


class JsonDevelopmentHypothesisComposer:
    """Parse the canonical JSON hypothesis envelope from model output.

    Composition is deliberately lenient: only ``claim`` and ``disagreement``
    plus the falsifier core fields and a research ``model`` are required.
    Prose metadata remains tolerant, but Layer A cannot silently substitute a
    placeholder strategy for a missing statistical model.
    """

    def compose(self, invocation: InvocationResult) -> HypothesisDraft:
        document = _json_document(invocation, "hypothesis")
        claim = _compose_text(document, "claim", "claim")
        disagreement = _compose_text(document, "disagreement", "disagreement")
        model = _compose_research_model(document.get("model"))
        universe = _compose_universe(document.get("universe"), model)
        falsifiers = _compose_falsifiers(document.get("falsifiers"))
        sizing = document.get("sizing")
        if not (isinstance(sizing, str) and sizing.strip()):
            sizing = None
        body = document.get("body", "")
        if body is None:
            body = ""
        if not isinstance(body, str):
            raise FalsificationComposeError("hypothesis body must be text")
        hypothesis_id = document.get("hypothesisId")
        if hypothesis_id is not None and not (
            isinstance(hypothesis_id, str) and hypothesis_id.strip()
        ):
            hypothesis_id = None
        try:
            return HypothesisDraft(
                claim=claim,
                disagreement=disagreement,
                risk_factor=_optional_compose_text(
                    document, "riskFactor", _DEFAULT_RISK_FACTOR, max_length=4_096
                ),
                horizon=_optional_compose_text(
                    document, "horizon", _DEFAULT_HORIZON, max_length=4_096
                ),
                universe=universe,
                falsifiers=falsifiers,
                model=model,
                sizing=None if sizing is None else sizing.strip(),
                body=body,
                hypothesis_id=None if hypothesis_id is None else hypothesis_id.strip(),
            )
        except WorkflowError as error:
            raise FalsificationComposeError(str(error)) from error


def _compose_research_model(value: object) -> ResearchModel:
    if not isinstance(value, Mapping):
        raise FalsificationComposeError("hypothesis requires a research model object")
    indicators = value.get("indicators", [])
    outputs = value.get("outputs")
    tests = value.get("tests")
    configuration = value.get("harnessConfiguration", {})
    if isinstance(indicators, str) or not isinstance(indicators, Sequence):
        raise FalsificationComposeError("model indicators must be a sequence")
    if isinstance(outputs, str) or not isinstance(outputs, Sequence):
        raise FalsificationComposeError("model outputs must be a sequence")
    if not isinstance(tests, Mapping):
        raise FalsificationComposeError("model tests must be a mapping")
    if not isinstance(configuration, Mapping):
        raise FalsificationComposeError("model harnessConfiguration must be an object")
    try:
        return ResearchModel(
            model_id=_model_text(value.get("modelId"), "modelId"),
            version=_model_text(value.get("version"), "model version"),
            description=_model_text(value.get("description"), "model description"),
            source=_model_text(value.get("source"), "model source"),
            fit_entrypoint=_model_text(
                value.get("fitEntrypoint", "fit"), "model fit entrypoint"
            ),
            observe_entrypoint=_model_text(
                value.get("observeEntrypoint", "observe"),
                "model observe entrypoint",
            ),
            outputs=tuple(outputs),
            indicators=tuple(_compose_indicator_binding(item) for item in indicators),
            tests=dict(tests),
            harness_configuration=dict(configuration),
        )
    except (IndicatorArtifactError, ResearchModelError, TypeError) as error:
        raise FalsificationComposeError(str(error)) from error


def _compose_indicator_binding(value: object) -> IndicatorBinding:
    if not isinstance(value, Mapping):
        raise FalsificationComposeError("each model indicator must be an object")
    try:
        scope = IndicatorScope(value.get("scope"))
    except ValueError as error:
        raise FalsificationComposeError(
            "model indicator scope must be global or workspace"
        ) from error
    parameters = value.get("parameters", {})
    if not isinstance(parameters, Mapping):
        raise FalsificationComposeError("model indicator parameters must be an object")
    definition_value = value.get("definition")
    definition: WorkspaceIndicator | None = None
    if definition_value is not None:
        if not isinstance(definition_value, Mapping):
            raise FalsificationComposeError(
                "workspace indicator definition must be an object"
            )
        tests = definition_value.get("tests", {})
        schema = definition_value.get("parameterSchema", {})
        if not isinstance(tests, Mapping) or not isinstance(schema, Mapping):
            raise FalsificationComposeError(
                "workspace indicator tests and parameterSchema must be objects"
            )
        try:
            definition = WorkspaceIndicator(
                name=definition_value.get("name", value.get("name")),
                version=definition_value.get("version"),
                description=definition_value.get("description"),
                source=definition_value.get("source"),
                entrypoint=definition_value.get("entrypoint", "calculate"),
                parameter_schema=dict(schema),
                tests=dict(tests),
            )
        except (IndicatorArtifactError, TypeError) as error:
            raise FalsificationComposeError(str(error)) from error
    try:
        return IndicatorBinding(
            alias=_model_text(value.get("alias"), "indicator alias"),
            scope=scope,
            name=_model_text(value.get("name"), "indicator name"),
            symbol=_model_text(value.get("symbol"), "indicator symbol"),
            parameters=dict(parameters),
            content_sha256=value.get("contentSha256"),
            definition=definition,
        )
    except (IndicatorArtifactError, ResearchModelError, TypeError) as error:
        raise FalsificationComposeError(str(error)) from error


def _model_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FalsificationComposeError(f"{label} must be non-empty text")
    return value.strip()


def _compose_universe(value: object, model: ResearchModel) -> tuple[str, ...]:
    """Return the declared universe, inferring it from the model when absent."""
    if value is not None:
        return _compose_symbols(value, "universe")
    symbols = model.harness_configuration.get("symbols")
    data = model.harness_configuration.get("data")
    if not isinstance(symbols, Sequence) or isinstance(symbols, str):
        symbols = data.get("symbols") if isinstance(data, Mapping) else None
    if isinstance(symbols, Sequence) and not isinstance(symbols, str):
        inferred = [item.strip() for item in symbols if isinstance(item, str)]
        inferred = [item for item in inferred if item]
        if inferred:
            return tuple(dict.fromkeys(inferred))
    return _DEFAULT_UNIVERSE


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
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise FalsificationComposeError("falsifiers must be a sequence")
    falsifiers: list[HypothesisFalsifier] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise FalsificationComposeError("each falsifier must be a JSON object")
        normalized = dict(item)
        if "falsifierId" in normalized and "falsifier_id" not in normalized:
            normalized["falsifier_id"] = normalized.pop("falsifierId")
        if not normalized.get("falsifier_id"):
            normalized["falsifier_id"] = f"f{index + 1}"
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
        raw_class = document.get("failureClass")
        if isinstance(raw_class, str):
            lowered = raw_class.lower()
            if "harness" in lowered:
                failure_class = "harness"
            elif "data" in lowered:
                failure_class = "data"
            elif (
                "implementation" in lowered
                or "syntax" in lowered
                or "import" in lowered
            ):
                failure_class = "implementation"
            elif "market" in lowered or "regime" in lowered:
                failure_class = "market_regime"
            elif "strategy" in lowered:
                failure_class = "strategy"
            else:
                failure_class = _compose_text(document, "failureClass", "failureClass")[
                    :64
                ]
        else:
            failure_class = _compose_text(document, "failureClass", "failureClass")[:64]
        try:
            return RcaDiagnosis(
                falsifier_id=_compose_text(document, "falsifierId", "falsifierId"),
                failure_class=failure_class,
                rationale=_compose_text(document, "rationale", "rationale"),
                confidence=float(confidence),
                follow_ups=tuple(item.strip() for item in follow_ups),
            )
        except WorkflowError as error:
            raise FalsificationComposeError(str(error)) from error


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
            entrypoint=_model_text(
                value.get("entrypoint", value.get("className", "Strategy")),
                f"{label} entrypoint",
            ),
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
    research_model_reference: ResearchModelRef | None = None
    research_model_artifact: str | None = None
    fitted_model_reference: FittedModelRef | None = None
    fitted_parameters: Mapping[str, float] | None = None
    research_model_runtime_configuration: Mapping[str, object] | None = None
    plan: FalsificationPlan | None = None
    trials: tuple[FalsificationTrial, ...] = ()
    falsification_prose: str | None = None
    hypothesis_cycles: int = 0
    rca: tuple[RcaDiagnosis, ...] = ()
    verdict: FalsificationVerdict | None = None
    strategy_bundle: StrategyBundle | None = None
    exit_criteria: tuple[ExitCriterion, ...] = ()
    strategy_cycles: int = 0
    strategy_backtest: BacktestRunResult | None = None
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
    research_model_reference: ResearchModelRef | None = None
    research_model_artifact: str | None = None
    fitted_model_reference: FittedModelRef | None = None
    fitted_parameters: Mapping[str, float] | None = None
    research_model_runtime_configuration: Mapping[str, object] | None = None
    plan: FalsificationPlan | None = None
    trials: tuple[FalsificationTrial, ...] = ()
    rca: tuple[RcaDiagnosis, ...] = ()
    verdict: FalsificationVerdict | None = None
    strategy_bundle: StrategyBundle | None = None
    exit_criteria: tuple[ExitCriterion, ...] = ()
    hypothesis_cycles: int = 0
    strategy_cycles: int = 0
    strategy_backtest: BacktestRunResult | None = None
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
            "hypothesisCycles": self.hypothesis_cycles,
            "researchModel": (
                None
                if self.research_model_reference is None
                else {
                    "contentSha256": self.research_model_reference.content_sha256,
                    "evidenceArtifact": self.research_model_artifact,
                    "fittedReference": (
                        None
                        if self.fitted_model_reference is None
                        else self.fitted_model_reference.key
                    ),
                    "fittedParameters": (
                        None
                        if self.fitted_parameters is None
                        else dict(self.fitted_parameters)
                    ),
                    "runtimeConfiguration": (
                        None
                        if self.research_model_runtime_configuration is None
                        else dict(self.research_model_runtime_configuration)
                    ),
                    "modelId": self.research_model_reference.model_id,
                    "reference": self.research_model_reference.key,
                }
            ),
            "optimizationBacktest": (
                None
                if self.optimization_backtest is None
                else self.optimization_backtest.as_dict()
            ),
            "strategyBacktest": (
                None
                if self.strategy_backtest is None
                else self.strategy_backtest.as_dict()
            ),
            "strategyCycles": self.strategy_cycles,
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


def _format_observed(val: float | None) -> str:
    if val is None or not math.isfinite(val):
        return "—"
    if abs(val) < 0.0001 and val != 0:
        return f"{val:.2e}"
    if abs(val) < 1:
        return f"{val:.4f}"
    return f"{val:.2f}"


class HypothesisDevelopmentWorkflow:
    """Orchestrate the falsification-first chat-to-strategy pipeline."""

    def __init__(
        self,
        *,
        chat: ChatService,
        router: ModelRouter,
        runner: BacktestRunner,
        research_runner: ResearchModelRunner | None = None,
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
        max_compose_repairs: int = DEFAULT_MAX_COMPOSE_REPAIRS,
        knowledge_cutoff: datetime | None = None,
        subagent_mcp_tools: Mapping[str, tuple[str, ...]] | None = None,
    ) -> None:
        self._chat = chat
        self._router = router
        self._runner = runner
        self._research_runner = (
            BacktestResearchModelRunner(runner)
            if research_runner is None
            else research_runner
        )
        if not callable(getattr(self._research_runner, "run", None)):
            raise WorkflowError("research_runner must implement ResearchModelRunner")
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
        self._max_hypothesis_cycles = DEFAULT_MAX_HYPOTHESIS_CYCLES
        if not isinstance(max_rca_cycles, int) or max_rca_cycles < 0:
            raise WorkflowError("max_rca_cycles must be a non-negative integer")
        self._max_rca_cycles = max_rca_cycles
        if not isinstance(max_compose_repairs, int) or max_compose_repairs < 0:
            raise WorkflowError("max_compose_repairs must be a non-negative integer")
        self._max_compose_repairs = max_compose_repairs
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
        self._paused_states: dict[str, HypothesisDevelopmentState] = {}

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
        trigger_message_id: str | None = None,
    ) -> HypothesisDevelopmentRequest:
        """Persist the chat trigger and return the runnable request.

        When ``trigger_message_id`` is supplied the execution binds to the
        operator's existing message instead of creating a duplicate trigger.
        """
        try:
            _text(ideas, "ideas", max_length=8_192)
        except WorkflowError as error:
            raise HypothesisDevelopmentIntakeError(str(error)) from error
        if trigger_message_id is None:
            execution = self._chat.launch_workflow(
                workspace_id,
                chat_id,
                workflow=self.definition,
                actor_id=actor_id,
                body=ideas,
                investigation_id=investigation_id,
            )
        else:
            execution = self._chat.launch_workflow(
                workspace_id,
                chat_id,
                workflow=self.definition,
                actor_id=actor_id,
                message_id=trigger_message_id,
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
                "phases": list(self.definition.phases),
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
        """Open the hypothesis-creation conversation for this request."""
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
            state = self._transition(
                request, state, _HYPOTHESIS_CREATION, "chat request accepted"
            )
            state = self._hypothesis_creation(request, state)
            state = self._run_after_creation(request, state)
        except _Fenced as fence:
            if self._execution_cancelled_or_deleted(request):
                return self._outcome(
                    replace(
                        fence.state if fence.state is not None else state,
                        detail=str(fence),
                        fenced=True,
                    )
                )
            state = self._pause(
                request, fence.state if fence.state is not None else state, fence
            )
            self._emit_phase(request, state.phase, {"state": state.phase.value})
        except _PhaseFailure as failure:
            state = self._fail(request, failure.state, failure)
            self._emit_phase(request, state.phase, {"state": state.phase.value})
        except FalsificationComposeError as error:
            state = self._pause_for_compose(request, state, error)
            self._emit_phase(request, state.phase, {"state": state.phase.value})
        except (
            BacktestRunError,
            HarnessError,
            StrategyBundleError,
            StrategyValidationError,
            WorkflowError,
        ) as error:
            state = self._fail(request, state, error)
            self._emit_phase(request, state.phase, {"state": state.phase.value})
        except Exception as error:  # noqa: BLE001 - unexpected infra faults fail closed
            state = self._fail(request, state, error)
            self._emit_phase(request, state.phase, {"state": state.phase.value})
        return self._outcome(state)

    def resume(
        self, request: HypothesisDevelopmentRequest
    ) -> HypothesisDevelopmentOutcome:
        """Run one more hypothesis-creation turn after operator input.

        Called when the operator sends a message to the hypothesis-creation
        node.  ``run`` was a single synchronous pass; this continuation re-runs
        one turn against the durable conversation and, once the operator
        supplies enough information, lets the model judge whether to advance,
        and then runs the remaining phases.
        """
        self._chat.get_execution(request.workspace_id, request.execution_id)
        paused_state = self._paused_states.get(request.execution_id)
        if paused_state is not None and paused_state.phase is _VERDICT:
            return self._resume_verdict(request, paused_state)
        state = HypothesisDevelopmentState(
            workspace_id=request.workspace_id,
            execution_id=request.execution_id,
            phase=_HYPOTHESIS_CREATION,
            intent_epoch=request.intent_epoch,
            transitions=self._replay_transitions(request),
            hypothesis_cycles=sum(
                1
                for transition in self._replay_transitions(request)
                if transition.to_phase is _FALSIFICATION_TRIALS
            ),
        )
        try:
            self._chat.start_workflow(
                request.workspace_id,
                request.execution_id,
                actor_id=request.requested_by,
            )
            state = self._hypothesis_creation(request, state)
            state = self._run_after_creation(request, state)
        except _Fenced as fence:
            if self._execution_cancelled_or_deleted(request):
                return self._outcome(
                    replace(
                        fence.state if fence.state is not None else state,
                        detail=str(fence),
                        fenced=True,
                    )
                )
            state = self._pause(
                request, fence.state if fence.state is not None else state, fence
            )
            self._emit_phase(request, state.phase, {"state": state.phase.value})
        except _PhaseFailure as failure:
            state = self._fail(request, failure.state, failure)
            self._emit_phase(request, state.phase, {"state": state.phase.value})
        except FalsificationComposeError as error:
            state = self._pause_for_compose(request, state, error)
            self._emit_phase(request, state.phase, {"state": state.phase.value})
        except (
            BacktestRunError,
            HarnessError,
            StrategyBundleError,
            StrategyValidationError,
            WorkflowError,
        ) as error:
            state = self._fail(request, state, error)
            self._emit_phase(request, state.phase, {"state": state.phase.value})
        except Exception as error:  # noqa: BLE001 - unexpected infra faults fail closed
            state = self._fail(request, state, error)
            self._emit_phase(request, state.phase, {"state": state.phase.value})
        return self._outcome(state)

    def _resume_verdict(
        self,
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
    ) -> HypothesisDevelopmentOutcome:
        """Apply operator input to the durable verdict conversation."""
        try:
            self._chat.start_workflow(
                request.workspace_id,
                request.execution_id,
                actor_id=request.requested_by,
            )
            active = self._activate_orchestrator(request, _VERDICT)
            body = self._latest_sub_agent_user_message(request, active.sub_agent_id)
            action = _verdict_review_action(body)
            if action is None:
                return self._request_operator_input(
                    request,
                    state,
                    purpose="apply the falsification verdict",
                    detail=(
                        "the operator's reply did not clearly check off or circle "
                        "back the recorded verdict"
                    ),
                    active=active,
                )
            self._paused_states.pop(request.execution_id, None)
            return self._outcome(
                self._continue_after_verdict(request, state, action=action)
            )
        except _Fenced as fence:
            if self._execution_cancelled_or_deleted(request):
                return self._outcome(
                    replace(
                        fence.state if fence.state is not None else state,
                        detail=str(fence),
                        fenced=True,
                    )
                )
            paused = self._pause(
                request, fence.state if fence.state is not None else state, fence
            )
            self._emit_phase(request, paused.phase, {"state": paused.phase.value})
            return self._outcome(paused)
        except _PhaseFailure as failure:
            failed = self._fail(request, failure.state, failure)
            self._emit_phase(request, failed.phase, {"state": failed.phase.value})
            return self._outcome(failed)
        except FalsificationComposeError as error:
            paused = self._pause_for_compose(request, state, error)
            self._emit_phase(request, paused.phase, {"state": paused.phase.value})
            return self._outcome(paused)
        except (
            BacktestRunError,
            HarnessError,
            StrategyBundleError,
            StrategyValidationError,
            WorkflowError,
        ) as error:
            failed = self._fail(request, state, error)
            self._emit_phase(request, failed.phase, {"state": failed.phase.value})
            return self._outcome(failed)
        except Exception as error:  # noqa: BLE001 - unexpected infra faults fail closed
            failed = self._fail(request, state, error)
            self._emit_phase(request, failed.phase, {"state": failed.phase.value})
            return self._outcome(failed)

    def _run_after_creation(
        self,
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
    ) -> HypothesisDevelopmentState:
        """Run the bounded premise and strategy loops to a terminal outcome."""
        document = state.hypothesis
        if document is None:
            raise WorkflowError("falsification trials require a hypothesis")
        state = self._run_trials(request, state)
        # The agent evaluates the trials first, in prose, including any harness
        # or data failure, before the evidence gate decides whether the verdict
        # can be trusted. This is the agent-first replacement for a separate
        # falsification RCA step and guarantees failures reach the model.
        active = self._active_orchestrators.get(request.execution_id)
        if active is not None:
            prose = self._author_falsification_prose(request, state, active)
            state = replace(state, falsification_prose=prose or None)
            if prose:
                self._emit_orchestrator_text(request, prose)
        state = self._transition_checked(
            request,
            state,
            _VERDICT,
            "evidence bound to every planned falsifier",
            lambda: check_evidence_bound(state.plan, state.trials),
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
        revised_question = self._emit_verdict_review(request, state)
        raise _Fenced(revised_question, state)

    def _continue_after_verdict(
        self,
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
        *,
        action: str,
    ) -> HypothesisDevelopmentState:
        """Apply a checked-off verdict or an explicit circle-back request."""
        verdict = state.verdict
        if verdict is None:
            raise WorkflowError("verdict review requires a recorded verdict")
        document = state.hypothesis
        if document is None:
            raise WorkflowError("verdict review requires a hypothesis")
        if action == "circle_back" or check_verdict_advances(verdict) is not None:
            state = self._transition(
                request,
                state,
                _HYPOTHESIS_CREATION,
                "premise refuted; returning to hypothesis creation",
            )
            self._post_phase_handoff(
                request,
                self._premise_handoff(state, verdict),
                kind="falsification_findings",
            )
            if state.hypothesis_cycles >= self._max_hypothesis_cycles:
                return self._stop_for_cycle_budget(
                    request,
                    state,
                    purpose="recover from an exhausted hypothesis revision budget",
                    detail=(
                        "the hypothesis revision budget of 5 attempts is exhausted "
                        "after repeated falsifications; review the falsification "
                        "history and provide operator direction"
                    ),
                )
            state = replace(
                state,
                hypothesis=None,
                draft=None,
                research_model_reference=None,
                research_model_artifact=None,
                fitted_model_reference=None,
                fitted_parameters=None,
                research_model_runtime_configuration=None,
                plan=None,
                trials=(),
                strategy_bundle=None,
                exit_criteria=(),
                strategy_backtest=None,
                optimization_backtest=None,
            )
            return self._hypothesis_creation(request, state)
        state = self._transition(
            request,
            state,
            _STRATEGY_DESIGN,
            "hypothesis survived falsification",
        )
        state = self._design(request, state)
        state = self._transition_checked(
            request,
            state,
            _STRATEGY_BACKTESTING,
            "strategy validated with falsifier exit criteria",
            lambda: check_strategy_design(
                state.strategy_bundle, state.exit_criteria, document.front_matter
            ),
        )
        state = self._run_strategy_backtest(request, state)
        while True:
            state = self._revise_until_strategy_test_passes(request, state, document)
            state = self._transition(
                request,
                state,
                _OPTIMIZATION,
                "strategy backtest completed successfully",
            )
            state = self._optimize(request, state)
            if (
                state.optimization_backtest is None
                or state.optimization_backtest.succeeded
            ):
                break
            state = replace(state, strategy_backtest=state.optimization_backtest)
        journal_id = self._journal_entry(request, state)
        state = replace(state, journal_id=journal_id)
        self._finish_execution(
            request,
            WorkflowExecutionState.COMPLETED,
            ExecutionEventType.WORKFLOW_COMPLETED,
            payload=self._completion_payload(request, state),
        )
        return self._transition(request, state, _COMPLETED, "outcome recorded")

    def _emit_verdict_review(
        self,
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
    ) -> str:
        """Render the verdict as a conversational review, not a workflow gate."""
        verdict = state.verdict
        active = self._active_orchestrators.get(request.execution_id)
        if verdict is None or active is None:
            raise WorkflowError("verdict review requires an active verdict node")
        question = self._operator_question(
            request,
            state,
            active=active,
            purpose="review and apply the falsification verdict",
            detail=(
                f"verdict {verdict.outcome}: {verdict.reason}. The operator may "
                "check off the verdict to apply it, or circle back to revise the "
                "hypothesis."
            ),
        )
        lines = [
            "### Falsification Verdict",
            "",
            f"**{verdict.outcome.capitalize()}**: {verdict.reason}",
            "",
            question,
        ]
        self._emit_orchestrator_text(request, "\n".join(lines))
        self._append_sub_agent(
            request,
            ExecutionEventType.SUB_AGENT_NEEDS_INPUT,
            sub_agent_id=active.sub_agent_id,
            parent_sub_agent_id=None,
            phase=_VERDICT,
            role=active.role,
            context=active.context,
            accepts_input=True,
            extra={
                "kind": "verdict_review",
                "question": question,
                "verdict": verdict.outcome,
            },
        )
        return question

    def _latest_sub_agent_user_message(
        self, request: HypothesisDevelopmentRequest, sub_agent_id: str
    ) -> str:
        """Return the latest operator message addressed to one phase node."""
        for message in reversed(
            self._chat.list_messages(request.workspace_id, request.chat_id)
        ):
            if message.role is not MessageRole.USER:
                continue
            data = message.data
            if isinstance(data, Mapping) and data.get("subAgentId") == sub_agent_id:
                return message.body
        return ""

    # -- hypothesis creation conversation ----------------------------------

    def _hypothesis_creation(
        self,
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
    ) -> HypothesisDevelopmentState:
        """Run one structured model judgment, finalizing when it advances."""
        active = self._ensure_creation_orchestrator(request)
        conversation = self._creation_conversation(request, active.sub_agent_id)
        first_turn = not any(role == "assistant" for role, _ in conversation)
        revision_turn = bool(conversation and conversation[-1][0] == "system")
        reply = self._creation_reply(
            request, active, conversation, first_turn=first_turn
        )
        text = reply.message.strip() or (
            "Tell me more about the regularity you expect so I can refine the model."
        )
        if not first_turn and not revision_turn and reply.advance:
            self._emit_orchestrator_text(request, text)
            return self._finalize_creation(request, state, active)
        if revision_turn:
            purpose = (
                "confirm the revised hypothesis proposal before running "
                "falsification trials"
            )
        elif reply.advance:
            purpose = "review the proposed hypothesis model before finalizing"
        else:
            purpose = "refine the hypothesis model"
        question = self._operator_question(
            request,
            state,
            active=active,
            purpose=purpose,
            detail=(
                f"hypothesis attempt {state.hypothesis_cycles + 1}; the proposal is "
                "ready for operator review"
                if reply.advance
                else f"hypothesis attempt {state.hypothesis_cycles + 1}; the model needs "
                "more operator context to refine the proposal"
            ),
        )
        self._emit_orchestrator_text(request, f"{text}\n\n{question}")
        self._append_sub_agent(
            request,
            ExecutionEventType.SUB_AGENT_NEEDS_INPUT,
            sub_agent_id=active.sub_agent_id,
            parent_sub_agent_id=None,
            phase=active.phase,
            role=active.role,
            context=active.context,
            accepts_input=True,
            extra={
                "question": question,
                "ready": reply.advance and not revision_turn,
            },
        )
        raise _Fenced(question, state)

    def _finalize_creation(
        self,
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
        active: _ActiveSubAgent,
    ) -> HypothesisDevelopmentState:
        """Compose the structured hypothesis + plan and hand off to trials."""
        conversation = self._creation_conversation(request, active.sub_agent_id)
        draft, invocation = self._compose_creation_hypothesis(request, conversation)
        document = self._build_hypothesis(request, draft)
        failure = check_falsifiability(document.front_matter)
        if failure is not None:
            return self._ask_creation_question(request, state, active, failure)
        model_failure = check_research_model(draft.model, document.front_matter)
        if model_failure is not None:
            return self._ask_creation_question(request, state, active, model_failure)
        self._store.save(document)
        state = replace(
            state,
            draft=draft,
            hypothesis=document,
            hypothesis_cycles=state.hypothesis_cycles + 1,
            model_provenance=MappingProxyType(dict(invocation.provenance.as_dict())),
        )
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
        plan = self._plan(request, state)
        plan_failure = check_falsification_plan(plan, document.front_matter)
        if plan_failure is not None:
            return self._ask_creation_question(request, state, active, plan_failure)
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
        self._emit_orchestrator_text(
            request,
            f"Hypothesis finalization recorded. {len(plan.tests)} falsification "
            "trial(s) planned.",
        )
        self._finish_orchestrator(request, status=STATUS_COMPLETED)
        return self._transition(
            request, state, _FALSIFICATION_TRIALS, "falsifiable hypothesis confirmed"
        )

    def _ask_creation_question(
        self,
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
        active: _ActiveSubAgent,
        question: str,
    ) -> HypothesisDevelopmentState:
        """Surface a blocking clarification without losing the conversation."""
        return self._request_operator_input(
            request,
            state,
            purpose="finalize the hypothesis model",
            detail=question,
            active=active,
        )

    def _creation_reply(
        self,
        request: HypothesisDevelopmentRequest,
        active: _ActiveSubAgent,
        conversation: list[tuple[str, str]],
        *,
        first_turn: bool,
    ) -> CreationTurn:
        prompt_for = self._prompt_builder(
            workspace_scope=request.workspace_id,
            role=active.role,
            context_id=active.sub_agent_id,
        )
        task = _creation_turn_prompt(request.ideas, conversation, first_turn=first_turn)
        turn, _ = self._compose_with_repair(
            request,
            self._research_role,
            base_task=task,
            prompt_for=prompt_for,
            compose=JsonCreationTurnComposer().compose,
            response_schema=_CREATION_TURN_SCHEMA,
            emit_activity=False,
        )
        return turn

    def _compose_creation_hypothesis(
        self,
        request: HypothesisDevelopmentRequest,
        conversation: list[tuple[str, str]],
    ) -> tuple[HypothesisDraft, InvocationResult]:
        prompt_for = self._prompt_builder(
            workspace_scope=request.workspace_id,
            role=f"Thesis {self._research_role} hypothesis author",
            context_id=f"{request.execution_id}-finalize",
        )
        return self._compose_with_repair(
            request,
            self._research_role,
            base_task=_creation_finalize_prompt(request.ideas, conversation),
            prompt_for=prompt_for,
            compose=self._composer.compose,
            response_schema=_HYPOTHESIS_SCHEMA,
            emit_output=False,
        )

    def _ensure_creation_orchestrator(
        self, request: HypothesisDevelopmentRequest
    ) -> _ActiveSubAgent:
        active = self._active_orchestrators.get(request.execution_id)
        if active is not None:
            return active
        for event in reversed(
            self._chat.replay_events(request.workspace_id, request.execution_id).events
        ):
            if event.event_type is not ExecutionEventType.SUB_AGENT_SPAWNED:
                continue
            payload = event.payload
            if (
                payload.get("phase") != _HYPOTHESIS_CREATION.value
                or payload.get("parentSubAgentId") is not None
            ):
                continue
            active = _ActiveSubAgent(
                sub_agent_id=str(payload["subAgentId"]),
                phase=_HYPOTHESIS_CREATION,
                role=str(
                    payload.get("role") or "Thesis hypothesis-creation orchestrator"
                ),
                context=SubAgentContextSpec(),
            )
            self._active_orchestrators[request.execution_id] = active
            return active
        self._spawn_orchestrator(request, _HYPOTHESIS_CREATION)
        return self._active_orchestrators[request.execution_id]

    def _creation_conversation(
        self, request: HypothesisDevelopmentRequest, sub_agent_id: str
    ) -> list[tuple[str, str]]:
        """Reconstruct the durable hypothesis-creation transcript in order."""
        turns: list[tuple[float, str, str]] = [(float("-inf"), "user", request.ideas)]
        for message in self._chat.list_messages(request.workspace_id, request.chat_id):
            if message.kind is not TranscriptItemKind.SUB_AGENT:
                continue
            data = message.data
            if (
                not isinstance(data, Mapping)
                or str(data.get("subAgentId")) != sub_agent_id
                or message.role not in {MessageRole.USER, MessageRole.SYSTEM}
            ):
                continue
            turns.append(
                (
                    message.created_at.timestamp(),
                    "user" if message.role is MessageRole.USER else "system",
                    message.body,
                )
            )
        for event in self._chat.replay_events(
            request.workspace_id, request.execution_id
        ).events:
            if event.payload.get("subAgentId") != sub_agent_id:
                continue
            if event.event_type is ExecutionEventType.SUB_AGENT_OUTPUT:
                text = _event_output_text(event.payload)
                if text:
                    turns.append((event.occurred_at.timestamp(), "assistant", text))
        turns.sort(key=lambda turn: turn[0])
        return [(role, text) for _, role, text in turns]

    def _emit_orchestrator_text(
        self, request: HypothesisDevelopmentRequest, text: str
    ) -> None:
        active = self._active_orchestrators.get(request.execution_id)
        if active is None or not text.strip():
            return
        self._append_sub_agent(
            request,
            ExecutionEventType.SUB_AGENT_OUTPUT,
            sub_agent_id=active.sub_agent_id,
            parent_sub_agent_id=None,
            phase=active.phase,
            role=active.role,
            context=active.context,
            accepts_input=active.phase in {_HYPOTHESIS_CREATION, _VERDICT},
            extra={
                "artifact": {"content": text.strip(), "type": "text"},
                "kind": "artifact",
            },
        )

    # -- phases ------------------------------------------------------------

    def _prompt_builder(
        self, *, workspace_scope: str, role: str, context_id: str
    ) -> Callable[[str], PromptContext]:
        """Return a function that renders one task into a scoped prompt."""

        def build(task: str) -> PromptContext:
            return assemble_context(
                workspace_scope=workspace_scope,
                task=task,
                role=role,
                context_id=context_id,
            )

        return build

    def _compose_with_repair(
        self,
        request: HypothesisDevelopmentRequest,
        role: Role,
        *,
        base_task: str,
        prompt_for: Callable[[str], PromptContext],
        compose: Callable[[InvocationResult], _ComposedT],
        response_schema: str,
        emit_activity: bool = True,
        emit_output: bool = True,
    ) -> tuple[_ComposedT, InvocationResult]:
        """Invoke the model and compose, repairing malformed output in place.

        A compose failure never ends the run by itself: the model is re-invoked
        with the validation error and the required schema, up to
        ``max_compose_repairs`` times.  When repairs are exhausted the final
        :class:`FalsificationComposeError` is re-raised so ``run`` can pause for
        operator input instead of failing.

        When ``emit_activity`` is set, the phase orchestrator's task is recorded
        as a ``sub_agent.progress`` event. ``emit_output`` controls whether the
        raw model reply is also recorded; structured calls disable it and emit a
        concise domain summary after composition instead.
        """
        if emit_activity:
            self._emit_orchestrator_progress(request, base_task)
        last_error: FalsificationComposeError | None = None
        invocation: InvocationResult | None = None
        for _ in range(self._max_compose_repairs + 1):
            task = f"{base_task}\n\n{_JSON_ONLY_INSTRUCTION}"
            if last_error is not None:
                task = (
                    f"{base_task}\n\nYour previous reply could not be used: "
                    f"{last_error}. Reply again with a single JSON object that "
                    f"matches this schema: {response_schema}\n\n"
                    f"{_JSON_ONLY_INSTRUCTION}"
                )
            invocation = self._router.invoke(
                role,
                prompt_for(task),
                response_format={"type": "json_object"},
            )
            try:
                composed = compose(invocation)
            except FalsificationComposeError as error:
                last_error = error
                continue
            if emit_activity and emit_output:
                self._emit_orchestrator_output(request, invocation)
            return composed, invocation
        assert invocation is not None and last_error is not None
        raise last_error

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
        if document is None or state.draft is None:
            raise WorkflowError("falsification planning requires a hypothesis")
        model_reference = state.draft.model.reference(request.workspace_id).key
        prompt_for = self._prompt_builder(
            workspace_scope=request.workspace_id,
            role=f"Thesis {self._research_role} falsification planner",
            context_id=f"{request.execution_id}-plan",
        )
        draft, _ = self._compose_with_repair(
            request,
            self._research_role,
            base_task="\n".join(
                [
                    "Design falsification trials and negative controls for the "
                    "hypothesis below. Every test binds to one declared falsifier "
                    "by its falsifierId and describes how that observable is "
                    "computed from historical data. Every control is a "
                    "negative/placebo check (shuffled or permuted returns, an "
                    "unrelated asset pair, a placebo period) that should show no "
                    "regularity. The plan exists to destroy the hypothesis, not to "
                    "confirm it.",
                    "",
                    *_hypothesis_context_lines(document, model_reference),
                ]
            ),
            prompt_for=prompt_for,
            compose=self._planner.compose,
            response_schema=_FALSIFICATION_PLAN_SCHEMA,
            emit_output=False,
        )
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
        falsifiers = {
            falsifier.falsifier_id: falsifier
            for falsifier in document.front_matter.falsifiers
            if falsifier.falsifier_id is not None
        }
        retained = {
            trial.test_id: trial for trial in state.trials if trial.observed is not None
        }
        trials: list[FalsificationTrial] = []
        model_reference = state.research_model_reference
        model_artifact = state.research_model_artifact
        fitted_parameters = state.fitted_parameters
        runtime_configuration = state.research_model_runtime_configuration
        fitted_reference = state.fitted_model_reference
        model_run: ResearchModelRunResult | None = None
        model_error: str | None = None
        if any(test.test_id not in retained for test in plan.tests):
            try:
                model_run = self._run_research_model(
                    request,
                    state,
                    model=state.draft.model,
                    correlation_id=f"falsify-{request.execution_id}-model",
                    label="layer-a-model",
                )
            except (BacktestRunError, HarnessError, ValueError) as error:
                model_error = str(error)
            else:
                model_reference = model_run.model_reference
                model_artifact = model_run.workspace_artifact
                fitted_parameters = model_run.fitted_parameters
                runtime_configuration = model_run.runtime_configuration
                fitted_reference = model_run.fitted_reference
        for test in plan.tests:
            existing = retained.get(test.test_id)
            if existing is not None:
                trials.append(existing)
                continue
            if model_error is not None:
                trials.append(
                    FalsificationTrial(
                        test_id=test.test_id,
                        falsifier_id=test.falsifier_id,
                        run_id=f"falsify-{request.execution_id}-model",
                        digest=_digest({"error": model_error}),
                        run_state="failed",
                        observed=None,
                        falsified=False,
                        detail=model_error,
                    )
                )
                self._append_attempt(
                    request,
                    state,
                    AttemptKind.ABORTED_JOB,
                    f"falsification trial {test.test_id} could not run",
                    {
                        "error": model_error,
                        "falsifierId": test.falsifier_id,
                    },
                )
                continue
            assert model_run is not None
            result = model_run.backtest
            sample_valid = _metric(result.metrics, "sample_valid")
            if sample_valid is not None and sample_valid <= 0:
                observed = None
            else:
                observed = _metric(result.metrics, test.observable)
            falsifier = falsifiers[test.falsifier_id]
            detail = result.error
            if detail is None and not result.succeeded:
                detail = f"harness run ended in state {result.state!r}"
            trials.append(
                FalsificationTrial(
                    test_id=test.test_id,
                    falsifier_id=test.falsifier_id,
                    run_id=result.job_id,
                    digest=_digest(result.as_dict()),
                    run_state=result.state,
                    observed=observed,
                    falsified=observed is not None and falsifier.falsifies(observed),
                    detail=detail,
                )
            )
            self._append_attempt(
                request,
                state,
                AttemptKind.METRIC,
                f"falsification trial {test.test_id} recorded",
                {
                    "detail": detail,
                    "falsifierId": test.falsifier_id,
                    "observed": observed,
                    "runId": result.job_id,
                    "runState": result.state,
                },
            )
        state = replace(
            state,
            trials=tuple(trials),
            research_model_reference=model_reference,
            research_model_artifact=model_artifact,
            fitted_model_reference=fitted_reference,
            fitted_parameters=fitted_parameters,
            research_model_runtime_configuration=runtime_configuration,
        )
        return self._project_hypothesis(request, state)

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

    def _run_strategy_backtest(
        self,
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
    ) -> HypothesisDevelopmentState:
        """Run one strategy attempt and retain a failed result for RCA."""
        bundle = state.strategy_bundle
        if bundle is None:
            raise WorkflowError("strategy backtesting requires a strategy bundle")
        run_id = f"strategy-test-{request.execution_id}-{state.strategy_cycles}"
        try:
            result = self._backtest(
                request,
                state,
                bundle=bundle,
                correlation_id=run_id,
                label="strategy-backtesting",
            )
        except (BacktestRunError, HarnessError) as error:
            result = BacktestRunResult(
                job_id=run_id,
                state="failed",
                metadata={"error": str(error)},
            )
        self._append_attempt(
            request,
            state,
            AttemptKind.VARIANT,
            "strategy backtest recorded",
            result.as_dict(),
        )
        return replace(state, strategy_backtest=result)

    def _revise_until_strategy_test_passes(
        self,
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
        document: HypothesisDocument,
    ) -> HypothesisDevelopmentState:
        """Run bounded strategy RCA/design cycles until one test succeeds."""
        while (
            state.strategy_backtest is not None
            and not state.strategy_backtest.succeeded
        ):
            state = self._transition(
                request,
                state,
                _STRATEGY_RCA,
                "strategy backtest failed; diagnosing the implementation",
            )
            state = self._diagnose_strategy(request, state)
            if state.strategy_cycles >= self._max_rca_cycles:
                return self._stop_for_cycle_budget(
                    request,
                    state,
                    purpose="recover from an exhausted strategy revision budget",
                    detail=(
                        "the strategy revision budget of 5 attempts is exhausted "
                        "after repeated failed backtests; review the backtest "
                        "lineage and provide operator direction"
                    ),
                )
            state = self._transition(
                request,
                state,
                _STRATEGY_DESIGN,
                "strategy RCA recommendations ready for revision",
            )
            self._post_phase_handoff(
                request,
                self._strategy_handoff(state),
                kind="strategy_rca_findings",
            )
            state = self._design(request, state)
            state = self._transition_checked(
                request,
                state,
                _STRATEGY_BACKTESTING,
                "revised strategy validated with falsifier exit criteria",
                lambda current=state: check_strategy_design(
                    current.strategy_bundle,
                    current.exit_criteria,
                    document.front_matter,
                ),
            )
            state = self._run_strategy_backtest(request, state)
        return state

    def _diagnose_strategy(
        self,
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
    ) -> HypothesisDevelopmentState:
        """Ask the persistent strategy RCA node to diagnose one failed run."""
        result = state.strategy_backtest or state.optimization_backtest
        if result is None:
            raise WorkflowError("strategy RCA requires a failed backtest")
        active = self._active_orchestrators.get(request.execution_id)
        if active is None or active.phase is not _STRATEGY_RCA:
            raise WorkflowError("strategy RCA orchestrator is not active")
        prompt_for = self._prompt_builder(
            workspace_scope=request.workspace_id,
            role=active.role,
            context_id=f"{request.execution_id}-strategy-rca-{state.strategy_cycles}",
        )
        task = "\n".join(
            [
                "Diagnose this failed strategy backtest. Separate harness/data defects "
                "from implementation, strategy, or market-regime defects. Return a "
                "concrete revision handoff for the strategy-design agent.",
                "Set falsifierId to strategy_backtest and choose a concise failureClass.",
                f"Strategy attempt: {state.strategy_cycles}",
                f"Backtest result: {json.dumps(result.as_dict(), default=str, sort_keys=True)}",
                f"Backtest metadata: {json.dumps(dict(result.metadata), default=str, sort_keys=True)}",
                f"Strategy manifest: {json.dumps(state.strategy_bundle.manifest() if state.strategy_bundle else {}, default=str, sort_keys=True)}",
                f"Required schema: {_RCA_SCHEMA}",
            ]
        )
        diagnosis, _ = self._compose_with_repair(
            request,
            self._critique_role,
            base_task=task,
            prompt_for=prompt_for,
            compose=self._rca_composer.compose,
            response_schema=_RCA_SCHEMA,
            emit_output=False,
        )
        diagnosis = replace(diagnosis, falsifier_id="strategy_backtest")
        prose = "\n".join(
            [
                "### Strategy Backtest RCA",
                "",
                diagnosis.rationale,
                "",
                *[f"- {follow_up}" for follow_up in diagnosis.follow_ups],
            ]
        )
        self._emit_orchestrator_text(request, prose)
        self._append_attempt(
            request,
            state,
            AttemptKind.ABORTED_JOB,
            "strategy RCA diagnosis recorded",
            diagnosis.as_dict(),
        )
        return replace(
            state,
            rca=(*state.rca, diagnosis),
        )

    def _post_phase_handoff(
        self,
        request: HypothesisDevelopmentRequest,
        body: str,
        *,
        kind: str,
    ) -> None:
        """Persist a system handoff into the currently active phase thread."""
        active = self._active_orchestrators.get(request.execution_id)
        if active is None:
            raise WorkflowError("phase handoff requires an active orchestrator")
        self._chat.create_message(
            request.workspace_id,
            request.chat_id,
            actor_id=_WORKFLOW_ACTOR,
            body=body,
            role=MessageRole.SYSTEM,
            execution_id=request.execution_id,
            kind=TranscriptItemKind.SUB_AGENT,
            data={
                "executionId": request.execution_id,
                "handoffKind": kind,
                "phase": active.phase.value,
                "subAgentId": active.sub_agent_id,
            },
        )

    def _premise_handoff(
        self,
        state: HypothesisDevelopmentState,
        verdict: FalsificationVerdict,
    ) -> str:
        """Render falsification findings for the next hypothesis proposal turn."""
        lines = [
            f"Falsification findings from hypothesis attempt {state.hypothesis_cycles}:",
            verdict.reason,
        ]
        tests = {test.test_id: test for test in state.plan.tests} if state.plan else {}
        for trial in state.trials:
            planned = tests.get(trial.test_id)
            lines.append(
                f"- {trial.test_id}: {planned.observable if planned else trial.falsifier_id} "
                f"observed={_format_observed(trial.observed)}; "
                f"falsified={str(trial.falsified).lower()}"
            )
        lines.append(
            "Revise the statistical premise, model, horizon, or controls in response "
            "to these findings, then present the updated proposal for operator review."
        )
        return "\n".join(lines)

    def _strategy_handoff(self, state: HypothesisDevelopmentState) -> str:
        """Render the latest strategy RCA finding for the next design attempt."""
        diagnosis = state.rca[-1]
        lines = [
            f"Strategy RCA handoff after attempt {state.strategy_cycles}:",
            f"Failure class: {diagnosis.failure_class}",
            diagnosis.rationale,
            "Required revisions:",
            *[f"- {item}" for item in diagnosis.follow_ups],
        ]
        return "\n".join(lines)

    def _stop_for_cycle_budget(
        self,
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
        *,
        purpose: str,
        detail: str,
    ) -> HypothesisDevelopmentState:
        """Stop a loop that exhausted its auditable attempt budget."""
        message = self._operator_question(
            request,
            state,
            active=self._active_orchestrators.get(request.execution_id),
            purpose=purpose,
            detail=detail,
        )
        raise _PhaseFailure(message, state)

    def _design(
        self, request: HypothesisDevelopmentRequest, state: HypothesisDevelopmentState
    ) -> HypothesisDevelopmentState:
        document = state.hypothesis
        model_reference = state.research_model_reference
        fitted_reference = state.fitted_model_reference
        fitted_parameters = state.fitted_parameters
        runtime_configuration = state.research_model_runtime_configuration
        if (
            document is None
            or model_reference is None
            or fitted_reference is None
            or state.draft is None
            or fitted_parameters is None
            or runtime_configuration is None
        ):
            raise WorkflowError("strategy design requires a hypothesis")
        prompt_for = self._prompt_builder(
            workspace_scope=request.workspace_id,
            role=f"Thesis {self._coding_role} strategy author",
            context_id=f"{request.execution_id}-design",
        )
        revision_context: list[str] = []
        if state.rca:
            latest = state.rca[-1]
            revision_context = [
                "",
                "Strategy RCA handoff from the previous failed backtest:",
                f"Failure class: {latest.failure_class}",
                f"Diagnosis: {latest.rationale}",
                "Required revisions:",
                *[f"- {item}" for item in latest.follow_ups],
            ]
        draft, _ = self._compose_with_repair(
            request,
            self._coding_role,
            base_task="\n".join(
                [
                    "Implement a strategy for the surviving hypothesis below. The "
                    "workspace research model is a referenced dependency for the "
                    "entry logic and Layer B monitor; do not copy or encode the "
                    "indicator/model as a strategy. Load and use the frozen Layer A "
                    "fit; never call fit again or estimate replacement parameters. "
                    "Enter when its observations say "
                    "the regularity is present, "
                    "and declare exit criteria that re-check the declared "
                    "falsifiers (by falsifierId) on a trailing window, so that a "
                    "live breakdown of the historical regularity closes the "
                    "position. Publish each current observation under the exact "
                    "falsifier observable with context.publish_metric(name, value) "
                    "so the run record and exit decision share evidence. Make "
                    "the source define the declared Python strategy entrypoint. "
                    "Sizing and risk limits must be explicit. PnL is a guardrail, never an "
                    "exit criterion.",
                    "",
                    *_hypothesis_context_lines(
                        document, model_reference.key, fitted_reference.key
                    ),
                    *revision_context,
                ]
            ),
            prompt_for=prompt_for,
            compose=self._designer.compose,
            response_schema=_STRATEGY_DESIGN_SCHEMA,
            emit_output=False,
        )
        monitor = dict(runtime_configuration)
        strategy_parameters: Mapping[str, object] = {}
        configured_instances = draft.content.harness_configuration.get(
            "strategy_instances"
        )
        if (
            isinstance(configured_instances, Sequence)
            and not isinstance(configured_instances, str)
            and configured_instances
        ):
            first_instance = configured_instances[0]
            if isinstance(first_instance, Mapping) and isinstance(
                first_instance.get("parameters"), Mapping
            ):
                strategy_parameters = dict(first_instance["parameters"])
        draft = replace(
            draft,
            content=replace(
                draft.content,
                harness_configuration={
                    **dict(draft.content.harness_configuration),
                    "strategy_instances": [
                        {
                            "implementation": "harness.strategy.adapter:StrategyBundleAdapter",
                            "instance_id": draft.content.strategy_id,
                            "bundle_hash": "0" * 64,
                            "capital_fraction": "1.0",
                            "symbols": list(document.front_matter.universe),
                            "parameters": {
                                "strategy_source": dict(draft.content.source),
                                "strategy_entrypoint": draft.content.entrypoint,
                                "strategy_parameters": dict(strategy_parameters),
                                "model_monitor": monitor,
                                "exit_criteria": [
                                    {
                                        **criterion.as_dict(),
                                        "threshold": str(criterion.threshold),
                                    }
                                    for criterion in draft.exit_criteria
                                ],
                            },
                        }
                    ],
                },
                dependencies=tuple(
                    dict.fromkeys(
                        (
                            *draft.content.dependencies,
                            model_reference.key,
                            fitted_reference.key,
                        )
                    )
                ),
            ),
        )
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
        return replace(
            state,
            strategy_bundle=bundle,
            exit_criteria=draft.exit_criteria,
            strategy_cycles=state.strategy_cycles + 1,
            strategy_backtest=None,
            optimization_backtest=None,
        )

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
        state = replace(state, optimization_backtest=result)
        self._append_attempt(
            request,
            state,
            AttemptKind.VARIANT,
            "strategy optimization run recorded",
            {"runId": result.job_id, "state": result.state},
        )
        return state

    def _run_research_model(
        self,
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
        *,
        model: ResearchModel,
        correlation_id: str,
        label: str,
    ) -> ResearchModelRunResult:
        """Publish and run one workspace model behind a visible tool job."""
        experiment_id = self._experiment_id or f"experiment-{request.execution_id}"
        job = self._chat.submit_tool_job(
            request.workspace_id,
            request.execution_id,
            tool_name=_MODEL_TOOL_NAME,
            actor_id=_WORKFLOW_ACTOR,
            correlation_id=correlation_id,
        )
        self._chat.start_tool_job(
            request.workspace_id, job.tool_job_id, actor_id=_WORKFLOW_ACTOR
        )
        active = self._active_orchestrators.get(request.execution_id)
        if active is not None:
            self._append_sub_agent(
                request,
                ExecutionEventType.SUB_AGENT_PROGRESS,
                sub_agent_id=active.sub_agent_id,
                parent_sub_agent_id=None,
                phase=active.phase,
                role=active.role,
                context=active.context,
                accepts_input=False,
                extra={
                    "kind": "tool",
                    "toolCall": {
                        "callId": job.tool_job_id,
                        "tool": _MODEL_TOOL_NAME,
                    },
                },
            )
        try:
            model_run = self._research_runner.run(
                ResearchModelRunRequest(
                    run_id=job.correlation_id,
                    workspace_id=request.workspace_id,
                    model=model,
                    workflow_execution_id=request.execution_id,
                    experiment_id=experiment_id,
                    universe=(
                        state.hypothesis.front_matter.universe
                        if state.hypothesis is not None
                        else ()
                    ),
                )
            )
        except Exception as error:
            self._chat.record_tool_result(
                request.workspace_id,
                job.tool_job_id,
                result=None,
                actor_id=_WORKFLOW_ACTOR,
                success=False,
                error_message=str(error),
                intent_epoch=state.intent_epoch,
            )
            if active is not None:
                self._append_sub_agent(
                    request,
                    ExecutionEventType.SUB_AGENT_PROGRESS,
                    sub_agent_id=active.sub_agent_id,
                    parent_sub_agent_id=None,
                    phase=active.phase,
                    role=active.role,
                    context=active.context,
                    accepts_input=False,
                    extra={
                        "kind": "tool",
                        "toolResult": {
                            "callId": job.tool_job_id,
                            "failure": {"message": str(error)},
                            "ok": False,
                            "structured": {},
                            "summary": "Falsification execution failed",
                            "text": str(error),
                            "tool": _MODEL_TOOL_NAME,
                        },
                    },
                )
            raise
        result = model_run.backtest
        self._chat.record_tool_result(
            request.workspace_id,
            job.tool_job_id,
            result={
                **result.as_dict(),
                "researchModelReference": model_run.model_reference.key,
                "indicatorReferences": [
                    reference.key for reference in model_run.indicator_references
                ],
                "workspaceArtifact": model_run.workspace_artifact,
                "fittedModelReference": (
                    None
                    if model_run.fitted_reference is None
                    else model_run.fitted_reference.key
                ),
            },
            actor_id=_WORKFLOW_ACTOR,
            success=result.succeeded,
            error_message=(
                None if result.succeeded else f"state {result.state!r} ({label})"
            ),
            intent_epoch=state.intent_epoch,
        )
        if active is not None:
            self._append_sub_agent(
                request,
                ExecutionEventType.SUB_AGENT_PROGRESS,
                sub_agent_id=active.sub_agent_id,
                parent_sub_agent_id=None,
                phase=active.phase,
                role=active.role,
                context=active.context,
                accepts_input=False,
                extra={
                    "kind": "tool",
                    "toolResult": {
                        "callId": job.tool_job_id,
                        "failure": None,
                        "ok": result.succeeded,
                        "structured": result.as_dict(),
                        "summary": "Falsification execution completed",
                        "text": "",
                        "tool": _MODEL_TOOL_NAME,
                    },
                },
            )
        try:
            self._require_current(request, state)
        except _Fenced as fence:
            raise _Fenced(str(fence), replace(state)) from fence
        return model_run

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
        try:
            result = self._runner.run(
                BacktestRunRequest(
                    run_id=job.correlation_id,
                    workspace_id=request.workspace_id,
                    bundle=bundle,
                    workflow_execution_id=request.execution_id,
                    experiment_id=experiment_id,
                )
            )
        except Exception as error:
            self._chat.record_tool_result(
                request.workspace_id,
                job.tool_job_id,
                result=None,
                actor_id=_WORKFLOW_ACTOR,
                success=False,
                error_message=str(error),
                intent_epoch=state.intent_epoch,
            )
            raise
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

    def _emit_orchestrator_progress(
        self, request: HypothesisDevelopmentRequest, text: str
    ) -> None:
        """Record a safe activity label, never the raw model instruction."""
        active = self._active_orchestrators.get(request.execution_id)
        if active is None or not text.strip():
            return
        activity = {
            _HYPOTHESIS_CREATION: "Finalizing the structured hypothesis model.",
            _FALSIFICATION_TRIALS: "Preparing historical falsification trials.",
            _VERDICT: "Recording the falsification verdict.",
            _STRATEGY_DESIGN: "Preparing the strategy design from the surviving model.",
            _STRATEGY_BACKTESTING: "Testing the designed strategy through the harness.",
            _STRATEGY_RCA: "Diagnosing the failed strategy backtest.",
            _OPTIMIZATION: "Preparing the bounded strategy optimization run.",
        }.get(active.phase, "Preparing the next workflow step.")
        self._append_sub_agent(
            request,
            ExecutionEventType.SUB_AGENT_PROGRESS,
            sub_agent_id=active.sub_agent_id,
            parent_sub_agent_id=None,
            phase=active.phase,
            role=active.role,
            context=active.context,
            accepts_input=active.phase in {_HYPOTHESIS_CREATION, _VERDICT},
            extra={"kind": "activity", "text": activity},
        )

    def _emit_orchestrator_output(
        self, request: HypothesisDevelopmentRequest, invocation: InvocationResult
    ) -> None:
        """Record the active phase orchestrator's reply as a visible artifact."""
        active = self._active_orchestrators.get(request.execution_id)
        output = invocation.output
        if active is None or not output:
            return
        self._append_sub_agent(
            request,
            ExecutionEventType.SUB_AGENT_OUTPUT,
            sub_agent_id=active.sub_agent_id,
            parent_sub_agent_id=None,
            phase=active.phase,
            role=active.role,
            context=active.context,
            accepts_input=active.phase in {_HYPOTHESIS_CREATION, _VERDICT},
            extra={"artifact": {"content": output, "type": "text"}, "kind": "artifact"},
        )

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
            accepts_input=phase in {_HYPOTHESIS_CREATION, _VERDICT},
        )
        self._active_orchestrators[request.execution_id] = _ActiveSubAgent(
            sub_agent_id=sub_agent_id,
            phase=phase,
            role=label,
            context=context,
        )

    def _activate_orchestrator(
        self,
        request: HypothesisDevelopmentRequest,
        phase: HypothesisDevelopmentPhase,
    ) -> _ActiveSubAgent:
        """Reopen one phase's durable conversation node, creating it once."""
        for event in reversed(
            self._chat.replay_events(request.workspace_id, request.execution_id).events
        ):
            if event.event_type is not ExecutionEventType.SUB_AGENT_SPAWNED:
                continue
            payload = event.payload
            if (
                payload.get("phase") != phase.value
                or payload.get("parentSubAgentId") is not None
            ):
                continue
            family, default_role = _PHASE_ORCHESTRATORS[phase]
            tools = self._subagent_mcp_tools.get(family, ())
            active = _ActiveSubAgent(
                sub_agent_id=str(payload["subAgentId"]),
                phase=phase,
                role=str(payload.get("role") or default_role),
                context=SubAgentContextSpec(
                    knowledge_cutoff=None,
                    mcp_enabled=bool(tools),
                    mcp_tools=tools,
                ),
            )
            self._active_orchestrators[request.execution_id] = active
            return active
        self._spawn_orchestrator(request, phase)
        return self._active_orchestrators[request.execution_id]

    def _finish_orchestrator(
        self,
        request: HypothesisDevelopmentRequest,
        *,
        status: str,
        detail: str | None = None,
        summary: str | None = None,
        artifact: Mapping[str, object] | None = None,
    ) -> None:
        active = self._active_orchestrators.pop(request.execution_id, None)
        if active is None:
            return
        if summary:
            artifact_payload: dict[str, object] = (
                dict(artifact)
                if artifact is not None
                else {"content": summary, "type": "text"}
            )
            if "content" not in artifact_payload:
                artifact_payload["content"] = summary
            if "type" not in artifact_payload:
                artifact_payload["type"] = "text"
            self._append_sub_agent(
                request,
                ExecutionEventType.SUB_AGENT_OUTPUT,
                sub_agent_id=active.sub_agent_id,
                parent_sub_agent_id=None,
                phase=active.phase,
                role=active.role,
                context=active.context,
                accepts_input=False,
                extra={
                    "artifact": artifact_payload,
                    "kind": "artifact",
                },
            )
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

    def _phase_summary(
        self, state: HypothesisDevelopmentState, phase: HypothesisDevelopmentPhase
    ) -> str:
        """Summarize what one phase produced, for its transcript."""
        if phase is _HYPOTHESIS_CREATION:
            if state.hypothesis is not None:
                return f"Hypothesis: {state.hypothesis.front_matter.claim}"
            if state.draft is not None:
                return f"Drafted hypothesis: {state.draft.claim}"
        elif phase is _FALSIFICATION_TRIALS:
            return f"Ran {len(state.trials)} falsification trial(s)."
        elif phase is _VERDICT and state.verdict is not None:
            return f"Verdict: {state.verdict.outcome}. {state.verdict.reason}".strip()
        elif phase is _STRATEGY_DESIGN and state.strategy_bundle is not None:
            bundle = state.strategy_bundle
            return (
                f"Designed strategy {bundle.strategy_id} {bundle.version} "
                f"(sha256 {bundle.bundle_sha256[:12]}…)."
            )
        elif phase is _STRATEGY_BACKTESTING and state.strategy_backtest is not None:
            return (
                f"Strategy backtest {state.strategy_backtest.job_id} ended in "
                f"state {state.strategy_backtest.state}."
            )
        elif phase is _STRATEGY_RCA:
            return f"Recorded {len(state.rca)} strategy RCA diagnosis(es)."
        elif phase is _OPTIMIZATION and state.optimization_backtest is not None:
            return "Optimization backtest completed."
        return f"{phase.value.capitalize()} phase completed."

    def _on_phase_entered(
        self,
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
    ) -> None:
        active = self._active_orchestrators.get(request.execution_id)
        if (
            active is not None
            and active.phase is _FALSIFICATION_TRIALS
            and state.trials
        ):
            report = self._falsification_report_artifact(
                state, prose=state.falsification_prose or ""
            )
            self._finish_orchestrator(
                request,
                status=STATUS_COMPLETED,
                summary=self._phase_summary(state, active.phase),
                artifact=report,
            )
        else:
            summary = (
                None if active is None else self._phase_summary(state, active.phase)
            )
            self._finish_orchestrator(request, status=STATUS_COMPLETED, summary=summary)
        if state.phase in _ACTIVE_PHASES:
            self._activate_orchestrator(request, state.phase)
            task = _PHASE_TASKS.get(state.phase)
            if task is not None:
                self._emit_orchestrator_progress(request, task)

    def _author_falsification_prose(
        self,
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
        active: _ActiveSubAgent,
    ) -> str:
        """Prompt the sub-agent for the falsification phase to produce its prose analysis."""
        prompt_for = self._prompt_builder(
            workspace_scope=request.workspace_id,
            role=active.role,
            context_id=f"{request.execution_id}-falsification-prose",
        )
        task = _falsification_turn_prompt(
            state,
            (state.fitted_parameters or {}),
        )
        try:
            invocation = self._router.invoke(
                self._research_role,
                prompt_for(task),
            )
            return (invocation.output or "").strip()
        except Exception:
            return ""

    def _falsification_report_artifact(
        self, state: HypothesisDevelopmentState, prose: str = ""
    ) -> dict[str, object]:
        """Build the rich structured falsification trials report artifact."""
        tests_by_id = {t.test_id: t for t in (state.plan.tests if state.plan else ())}
        passed_count = sum(
            1 for t in state.trials if not t.falsified and t.observed is not None
        )
        falsified_count = sum(1 for t in state.trials if t.falsified)
        total_count = len(state.trials)
        survived = (
            total_count > 0 and falsified_count == 0 and passed_count == total_count
        )

        trials_data: list[dict[str, object]] = []
        for t in state.trials:
            planned = tests_by_id.get(t.test_id)
            trials_data.append(
                {
                    "testId": t.test_id,
                    "falsifierId": t.falsifier_id,
                    "runId": t.run_id,
                    "runState": t.run_state,
                    "detail": t.detail,
                    "observed": t.observed,
                    "falsified": t.falsified,
                    "observable": (planned.observable if planned else t.falsifier_id),
                    "condition": planned.condition if planned else "",
                    "threshold": planned.threshold if planned else 0.0,
                    "description": planned.description if planned else "",
                }
            )

        controls_data = [
            {
                "controlId": c.control_id,
                "description": c.description,
                "expectation": c.expectation,
            }
            for c in (state.plan.controls if state.plan else ())
        ]

        summary_text = (
            f"Ran {total_count} falsification trial(s): {passed_count} survived, {falsified_count} refuted."
            if total_count > 0
            else "No falsification trials were executed."
        )

        metrics_data: dict[str, object] = {}
        for name, val in (state.fitted_parameters or {}).items():
            if isinstance(val, (int, float)) and math.isfinite(float(val)):
                metrics_data[name] = float(val)
            else:
                metrics_data[name] = str(val)

        return {
            "type": "falsification_report",
            "content": summary_text,
            "prose": prose,
            "passedCount": passed_count,
            "falsifiedCount": falsified_count,
            "totalCount": total_count,
            "survived": survived,
            "hypothesis": {
                "claim": (
                    state.hypothesis.front_matter.claim if state.hypothesis else ""
                ),
                "disagreement": (
                    state.hypothesis.front_matter.disagreement
                    if state.hypothesis
                    else ""
                ),
                "universe": (
                    list(state.hypothesis.front_matter.universe)
                    if state.hypothesis
                    else []
                ),
                "horizon": (
                    state.hypothesis.front_matter.horizon if state.hypothesis else ""
                ),
            },
            "trials": trials_data,
            "controls": controls_data,
            "metrics": metrics_data,
        }

    def _request_operator_input(
        self,
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
        *,
        purpose: str,
        detail: str,
        active: _ActiveSubAgent | None = None,
    ) -> NoReturn:
        """Surface an LLM-authored operator request via the orchestrator node.

        Operator-facing text is never a canned workflow string. The model is
        asked to author the request from the concrete reason the workflow
        paused; the only fallback is the raw reason itself, used solely when the
        model is unavailable so the workflow still fails loudly rather than
        inventing conversational text.
        """
        resolved = active or self._active_orchestrators.get(request.execution_id)
        question = self._operator_question(
            request,
            state,
            active=resolved,
            purpose=purpose,
            detail=detail,
        )
        self._emit_orchestrator_text(request, question)
        if resolved is not None:
            self._append_sub_agent(
                request,
                ExecutionEventType.SUB_AGENT_NEEDS_INPUT,
                sub_agent_id=resolved.sub_agent_id,
                parent_sub_agent_id=None,
                phase=resolved.phase,
                role=resolved.role,
                context=resolved.context,
                accepts_input=True,
                extra={"question": question},
            )
        raise _Fenced(question, state)

    def _operator_question(
        self,
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
        *,
        active: _ActiveSubAgent | None,
        purpose: str,
        detail: str,
    ) -> str:
        """Ask the model to author one operator-facing request.

        Returns the raw reason when the model is unavailable, so the operator
        still sees the concrete failure instead of a fabricated prompt.
        """
        del state
        if active is None:
            return detail
        prompt_for = self._prompt_builder(
            workspace_scope=request.workspace_id,
            role=active.role,
            context_id=f"{request.execution_id}-operator-question",
        )
        task = "\n".join(
            [
                "You are the workflow's operator-facing voice. The workflow has "
                "paused and needs the operator's direction to continue.",
                "Author the operator request, not a summary.",
                f"Purpose: {purpose}",
                f"Concrete reason the workflow paused: {detail}",
                "",
                "Write exactly one concise, natural request addressed to the "
                "operator. Include the concrete reason, say what you need from "
                "them, and do not mention internal step names, schemas, or that "
                "you are a model. Reply with the request text only: no quotes, "
                "no JSON, no code fences.",
            ]
        )
        try:
            invocation = self._router.invoke(self._research_role, prompt_for(task))
        except Exception:  # noqa: BLE001 - a failed author falls back to the reason
            return detail
        text = (invocation.output or "").strip()
        return text or detail

    def _transition_checked(
        self,
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
        target: HypothesisDevelopmentPhase,
        reason: str,
        check: Callable[[], str | None],
    ) -> HypothesisDevelopmentState:
        """Validate phase exit criteria, requesting operator input conversationally if blocked."""
        failure = check()
        if failure is not None:
            return self._request_operator_input(
                request,
                state,
                purpose=f"validate the step before {target.value}",
                detail=failure,
            )
        return self._transition(request, state, target, reason)

    def _transition(
        self,
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
        target: HypothesisDevelopmentPhase,
        reason: str,
    ) -> HypothesisDevelopmentState:
        advanced = self._advance(state, target, reason)
        self._emit_phase(request, target, {"reason": reason})
        try:
            self._chat.set_execution_phase(
                request.workspace_id, request.execution_id, target.value
            )
        except Exception:  # noqa: BLE001 - phase is best-effort projection state
            pass
        self._on_phase_entered(request, advanced)
        return advanced

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

    def _pause_for_compose(
        self,
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
        error: FalsificationComposeError,
    ) -> HypothesisDevelopmentState:
        """Pause for operator input when structured output cannot be composed.

        A model-format problem is an operator conversation, not a run failure:
        the open orchestrator is marked as needing input with a concrete
        question, and the execution moves to ``paused`` so it can be resumed.
        """
        active = self._active_orchestrators.get(request.execution_id)
        question = self._operator_question(
            request,
            state,
            active=active,
            purpose="recover from unparseable structured model output",
            detail=str(error),
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
                extra={
                    "kind": "compose",
                    "question": question,
                    "reason": str(error),
                },
            )
        return self._pause(request, state, _Fenced(f"{question} ({error})", state))

    def _pause(
        self,
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
        fence: _Fenced,
    ) -> HypothesisDevelopmentState:
        detail = str(fence)
        if state.phase in {_COMPLETED, _FAILED}:
            return replace(state, detail=detail, fenced=True)
        self._paused_states[request.execution_id] = state
        try:
            paused = self._advance(state, _PAUSED, detail)
        except InvalidPhaseTransition:
            return replace(state, detail=detail, fenced=True)
        self._pause_execution(request)
        return replace(paused, detail=detail, fenced=True)

    def _pause_execution(self, request: HypothesisDevelopmentRequest) -> None:
        """Move a running execution to ``paused`` without ending it."""
        try:
            execution = self._chat.get_execution(
                request.workspace_id, request.execution_id
            )
        except RecordNotFound:
            return
        if execution.state is not WorkflowExecutionState.RUNNING:
            return
        self._chat.pause_workflow(
            request.workspace_id,
            request.execution_id,
            actor_id=request.requested_by,
        )

    def _fail(
        self,
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
        error: Exception,
    ) -> HypothesisDevelopmentState:
        if self._execution_cancelled_or_deleted(request):
            return replace(
                state, error_message=str(error), detail=str(error), fenced=True
            )
        self._finish_orchestrator(
            request,
            status=STATUS_FAILED,
            detail=str(error),
            summary=f"Phase failed: {error}",
        )
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
            research_model_reference=state.research_model_reference,
            research_model_artifact=state.research_model_artifact,
            fitted_model_reference=state.fitted_model_reference,
            fitted_parameters=state.fitted_parameters,
            research_model_runtime_configuration=state.research_model_runtime_configuration,
            plan=state.plan,
            trials=state.trials,
            rca=state.rca,
            verdict=state.verdict,
            strategy_bundle=state.strategy_bundle,
            exit_criteria=state.exit_criteria,
            hypothesis_cycles=state.hypothesis_cycles,
            strategy_cycles=state.strategy_cycles,
            strategy_backtest=state.strategy_backtest,
            optimization_backtest=state.optimization_backtest,
            journal_id=state.journal_id,
            detail=state.detail,
        )

    # -- durable boundaries -------------------------------------------------

    def _replay_transitions(
        self, request: HypothesisDevelopmentRequest
    ) -> tuple[PhaseTransition, ...]:
        """Rebuild phase history when a paused conversation resumes."""
        transitions: list[PhaseTransition] = []
        previous = _REQUESTED
        for event in self._chat.replay_events(
            request.workspace_id, request.execution_id
        ).events:
            if event.event_type is not ExecutionEventType.WORKFLOW_PHASE:
                continue
            raw_phase = event.payload.get("phase")
            try:
                target = HypothesisDevelopmentPhase(str(raw_phase))
            except ValueError:
                continue
            if target in {_PAUSED, _FAILED} or target is previous:
                continue
            transitions.append(
                PhaseTransition(
                    sequence=len(transitions) + 1,
                    from_phase=previous,
                    to_phase=target,
                    at=event.occurred_at,
                    intent_epoch=event.intent_epoch,
                    reason=str(
                        event.payload.get("reason") or "durable phase transition"
                    ),
                )
            )
            previous = target
        return tuple(transitions)

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

    def _execution_cancelled_or_deleted(
        self, request: HypothesisDevelopmentRequest
    ) -> bool:
        """Return whether deletion or cancellation invalidated this worker."""
        try:
            execution = self._chat.get_execution(
                request.workspace_id, request.execution_id
            )
        except RecordNotFound:
            return True
        return execution.state in {
            WorkflowExecutionState.STOPPED,
            WorkflowExecutionState.CANCELLED,
        }

    def _check_stale_request(self, request: HypothesisDevelopmentRequest) -> None:
        try:
            execution = self._chat.get_execution(
                request.workspace_id, request.execution_id
            )
        except RecordNotFound as error:
            raise _Fenced("execution was deleted") from error
        if execution.state in _TERMINAL_EXECUTION_STATES:
            raise WorkflowError(f"execution is already {execution.state.value}")
        if execution.intent_epoch != request.intent_epoch:
            raise _Fenced("request predates newer steering")

    def _require_current(
        self,
        request: HypothesisDevelopmentRequest,
        state: HypothesisDevelopmentState,
    ) -> None:
        try:
            execution = self._chat.get_execution(
                request.workspace_id, request.execution_id
            )
        except RecordNotFound as error:
            raise _Fenced("execution was deleted") from error
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
    if value is None:
        custom = metrics.get("custom")
        if isinstance(custom, Mapping):
            value = custom.get(observable)
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return None
    else:
        return None
    return number if math.isfinite(number) else None


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
    "DEFAULT_MAX_COMPOSE_REPAIRS",
    "DEFAULT_MAX_HYPOTHESIS_CYCLES",
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
    "VERDICT_INCONCLUSIVE",
    "VERDICT_REFUTED",
    "VERDICT_SUPPORTED",
    "WorkflowError",
    "check_evidence_bound",
    "check_falsifiability",
    "check_falsification_plan",
    "check_research_model",
    "check_strategy_design",
    "check_verdict_advances",
    "transition_phase",
]
