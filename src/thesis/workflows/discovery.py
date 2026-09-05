"""Bounded autonomous strategy-discovery workflow orchestration.

This module mirrors the :mod:`thesis.workflows.strategy_development`
state-machine pattern: explicit phases and transitions, immutable state
snapshots, intent-epoch chat fencing, and injected ports for every external
effect.  It extends the pattern with the controls autonomous discovery needs:

- Bounded discovery: an explicit :class:`~thesis.agent.search_policy.DiscoveryBudget`
  caps candidates, model invocations, and harness backtests.  Budget guards
  return explicit decisions, and budget exhaustion is the explicit
  ``STOPPED`` workflow phase rather than a hidden error path.
- Deterministic candidate accounting: every generated, ranked, rejected,
  aborted, and implemented candidate is recorded with an explicit research
  rationale.  There are no invisible attempts: out-of-scope drafts,
  duplicates, budget-refused drafts, triage failures, inconclusive checks,
  and failed harness jobs all become ledger records.
- Cheap-to-expensive evaluation: the search policy ranks candidates
  cheap-to-expensive, and each candidate must pass ``sanity``, ``placebo``,
  and ``plausibility`` checks through an injected triage port before any
  harness backtest is authorized.
- Negative results are first-class: a run that rejects every candidate still
  records a labeled report and completes; runs that stop on budget
  exhaustion or unresolved uncertainty report what was and was not learned.

Ledger consolidation seam: the durable observed/declared attempt ledger is
being delivered by the experiments capability (``src/thesis/experiments/``,
CLEO T164).  Until that module lands, this workflow owns minimal local
interfaces (``AttemptKind``, ``AttemptRecord``, ``AttemptLedger``) and accepts
any injected implementation.  The workflow consumes only the
:class:`AttemptLedger` protocol, so re-pointing the interfaces at the shared
experiments contracts later is a local import change and does not change
workflow behavior.
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
from typing import Literal, Protocol

from thesis.agent.context import assemble_context
from thesis.agent.router import (
    InvocationResult,
    ModelInvocationError,
    ModelRouter,
    Role,
)
from thesis.agent.search_policy import (
    BudgetUsage,
    DiscoveryBudget,
    DiscoveryCandidate,
    RationaleKind,
    SearchPolicy,
    SearchPolicyError,
    SearchRationale,
)
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

_WORKFLOW_ID = "strategy-discovery"
_WORKFLOW_VERSION = "1.0.0"
_WORKFLOW_TITLE = "Autonomous strategy discovery"
_TOOL_NAME = "harness.backtest"
_TOOL_CORRELATION_PREFIX = "backtest"
_WORKFLOW_ACTOR = "thesis-workflow"
_REPORT_MEDIA_TYPE = "application/json"
_REPORT_SCHEMA = "fca-thesis-discovery-report-v1"
_CANDIDATE_VERSION = "1.0.0"

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
_WORKSPACE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")

DISCOVERY_DISCLOSURE = (
    "Discovery outcomes are exploratory: candidates were produced by an "
    "adaptive bounded search, and results are not corrected for the size or "
    "adaptivity of that search."
)
MULTIPLE_TESTING_DISCLOSURE = (
    "Candidate selection, stopping, and multiple-testing exposure are "
    "recorded in the attempt ledger and must be reflected in any later "
    "evidence grading."
)


class DiscoveryError(RuntimeError):
    """Base error for invalid strategy-discovery workflow operations."""


class InvalidPhaseTransition(DiscoveryError):
    """A phase cannot move directly to the requested phase."""


class DiscoveryGenerationError(DiscoveryError):
    """Raised when generated candidate content cannot be admitted."""


class _Fenced(Exception):
    """Internal signal that a continuation became stale and must pause.

    Carries the partial state recorded so far so in-flight results are not
    lost when the workflow pauses instead of continuing.
    """

    def __init__(
        self,
        message: str,
        state: DiscoveryState | None = None,
    ) -> None:
        super().__init__(message)
        self.state = state


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


class DiscoveryPhase(StrEnum):
    """The explicit, ordered phases of the discovery workflow."""

    REQUESTED = "requested"
    SCOPING = "scoping"
    GENERATING = "generating"
    RANKING = "ranking"
    TRIAGING = "triaging"
    BACKTESTING = "backtesting"
    RECORDING = "recording"
    COMPLETED = "completed"
    STOPPED = "stopped"
    FAILED = "failed"
    PAUSED = "paused"


_REQUESTED = DiscoveryPhase.REQUESTED
_SCOPING = DiscoveryPhase.SCOPING
_GENERATING = DiscoveryPhase.GENERATING
_RANKING = DiscoveryPhase.RANKING
_TRIAGING = DiscoveryPhase.TRIAGING
_BACKTESTING = DiscoveryPhase.BACKTESTING
_RECORDING = DiscoveryPhase.RECORDING
_COMPLETED = DiscoveryPhase.COMPLETED
_STOPPED = DiscoveryPhase.STOPPED
_FAILED = DiscoveryPhase.FAILED
_PAUSED = DiscoveryPhase.PAUSED

_TRANSITIONS: Mapping[DiscoveryPhase, frozenset[DiscoveryPhase]] = {
    _REQUESTED: frozenset({_SCOPING, _PAUSED, _FAILED}),
    _SCOPING: frozenset({_GENERATING, _PAUSED, _FAILED}),
    _GENERATING: frozenset({_RANKING, _PAUSED, _FAILED}),
    _RANKING: frozenset({_TRIAGING, _RECORDING, _PAUSED, _FAILED}),
    _TRIAGING: frozenset({_TRIAGING, _BACKTESTING, _RECORDING, _PAUSED, _FAILED}),
    _BACKTESTING: frozenset({_TRIAGING, _RECORDING, _PAUSED, _FAILED}),
    _RECORDING: frozenset({_COMPLETED, _STOPPED, _FAILED}),
    _PAUSED: frozenset(
        {_SCOPING, _GENERATING, _RANKING, _TRIAGING, _BACKTESTING, _RECORDING}
    ),
    _COMPLETED: frozenset(),
    _STOPPED: frozenset(),
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

DISCOVERY_PHASES: tuple[str, ...] = (
    _REQUESTED.value,
    _SCOPING.value,
    _GENERATING.value,
    _RANKING.value,
    _TRIAGING.value,
    _BACKTESTING.value,
    _RECORDING.value,
)

DISCOVERY_WORKFLOW = WorkflowDefinition(
    workflow_id=_WORKFLOW_ID,
    version=_WORKFLOW_VERSION,
    title=_WORKFLOW_TITLE,
    phases=DISCOVERY_PHASES,
)


def transition_phase(current: DiscoveryPhase, target: DiscoveryPhase) -> DiscoveryPhase:
    """Move between phases through the explicit transition table."""
    if target not in _TRANSITIONS[current]:
        raise InvalidPhaseTransition(
            f"workflow phase cannot move from {current.value} to {target.value}"
        )
    return target


def _workspace_id(value: str) -> str:
    if not isinstance(value, str) or _WORKSPACE_ID.fullmatch(value) is None:
        raise DiscoveryError("workspace_id must be a safe workspace identifier")
    return value


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise DiscoveryError(f"{label} must be a safe identifier")
    return value


def _text(value: str, label: str, *, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DiscoveryError(f"{label} must be non-empty")
    if len(value) > max_length or any(ord(character) < 32 for character in value):
        raise DiscoveryError(f"{label} contains unsupported characters")
    return value


def _scope_values(values: Sequence[str], label: str) -> tuple[str, ...]:
    if isinstance(values, str) or not isinstance(values, Sequence) or not values:
        raise DiscoveryError(f"{label} must be a non-empty sequence of identifiers")
    normalized = tuple(dict.fromkeys(values))
    for value in normalized:
        _identifier(value, f"{label} entry")
    return tuple(sorted(normalized))


@dataclass(frozen=True, slots=True)
class PhaseTransition:
    """One immutable, ordered record of a workflow phase change."""

    sequence: int
    from_phase: DiscoveryPhase
    to_phase: DiscoveryPhase
    at: datetime
    intent_epoch: int
    reason: str

    def __post_init__(self) -> None:
        if self.sequence < 1:
            raise DiscoveryError("phase transition sequence starts at one")
        if self.at.tzinfo is None or self.at.utcoffset() is None:
            raise DiscoveryError("phase transition timestamp must be timezone-aware")
        if not isinstance(self.reason, str) or not self.reason.strip():
            raise DiscoveryError("phase transition reason must be non-empty")
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
class DiscoveryScope:
    """The supported markets, horizons, and directions discovery may choose."""

    markets: tuple[str, ...]
    horizons: tuple[str, ...]
    directions: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "markets", _scope_values(self.markets, "markets"))
        object.__setattr__(self, "horizons", _scope_values(self.horizons, "horizons"))
        object.__setattr__(
            self, "directions", _scope_values(self.directions, "directions")
        )

    def admits(self, market: str, horizon: str, direction: str) -> bool:
        """Return whether a candidate direction is inside the declared scope."""
        return (
            market in self.markets
            and horizon in self.horizons
            and direction in self.directions
        )

    def as_dict(self) -> dict[str, object]:
        """Return the portable scope representation."""
        return {
            "directions": list(self.directions),
            "horizons": list(self.horizons),
            "markets": list(self.markets),
        }


@dataclass(frozen=True, slots=True)
class DiscoveryRequest:
    """The chat request context that triggers one discovery execution."""

    workspace_id: str
    chat_id: str
    execution_id: str
    trigger_message_id: str
    requested_by: str
    objective: str
    markets: Sequence[str]
    horizons: Sequence[str]
    directions: Sequence[str]
    intent_epoch: int = 0
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        _workspace_id(self.workspace_id)
        for name in ("chat_id", "execution_id", "trigger_message_id", "requested_by"):
            _identifier(getattr(self, name), name)
        _text(self.objective, "objective", max_length=8_192)
        object.__setattr__(self, "markets", _scope_values(self.markets, "markets"))
        object.__setattr__(self, "horizons", _scope_values(self.horizons, "horizons"))
        object.__setattr__(
            self, "directions", _scope_values(self.directions, "directions")
        )
        if self.intent_epoch < 0:
            raise DiscoveryError("intent_epoch must not be negative")
        if self.created_at is not None:
            if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
                raise DiscoveryError("created_at must be timezone-aware")
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
            "createdAt": created_at,
            "directions": list(self.directions),
            "executionId": self.execution_id,
            "horizons": list(self.horizons),
            "intentEpoch": self.intent_epoch,
            "markets": list(self.markets),
            "objective": self.objective,
            "requestedBy": self.requested_by,
            "triggerMessageId": self.trigger_message_id,
            "workspaceId": self.workspace_id,
        }


class AttemptKind(StrEnum):
    """The observed/declared attempt-ledger kinds for discovery candidates."""

    GENERATED = "generated"
    RANKED = "ranked"
    REJECTED = "rejected"
    ABORTED = "aborted"
    IMPLEMENTED = "implemented"


_ATTEMPTED_KINDS = frozenset(
    {AttemptKind.REJECTED, AttemptKind.ABORTED, AttemptKind.IMPLEMENTED}
)


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    """One immutable candidate-accounting entry with its explicit rationale."""

    attempt_id: str
    sequence: int
    candidate_id: str
    kind: AttemptKind
    rationale: SearchRationale
    at: datetime
    intent_epoch: int
    detail: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _identifier(self.attempt_id, "attempt_id")
        if not isinstance(self.sequence, int) or self.sequence < 1:
            raise DiscoveryError("attempt sequence starts at one")
        _identifier(self.candidate_id, "candidate_id")
        if not isinstance(self.kind, AttemptKind):
            raise DiscoveryError("attempt kind must be an AttemptKind")
        if not isinstance(self.rationale, SearchRationale):
            raise DiscoveryError("attempt rationale must be a SearchRationale")
        if self.at.tzinfo is None or self.at.utcoffset() is None:
            raise DiscoveryError("attempt timestamp must be timezone-aware")
        if self.intent_epoch < 0:
            raise DiscoveryError("attempt intent_epoch must not be negative")
        if not isinstance(self.detail, Mapping):
            raise DiscoveryError("attempt detail must be a mapping")
        object.__setattr__(self, "at", self.at.astimezone(UTC))
        object.__setattr__(self, "detail", MappingProxyType(dict(self.detail)))

    def as_dict(self) -> dict[str, object]:
        """Return the portable ledger representation of this attempt."""
        return {
            "attemptId": self.attempt_id,
            "candidateId": self.candidate_id,
            "detail": dict(self.detail),
            "intentEpoch": self.intent_epoch,
            "kind": self.kind.value,
            "rationale": self.rationale.as_dict(),
            "sequence": self.sequence,
            "at": self.at.isoformat().replace("+00:00", "Z"),
        }


class AttemptLedger(Protocol):
    """The durable observed/declared attempt ledger boundary.

    Consolidation seam: this protocol is the narrow surface the workflow
    needs from the experiments capability.  When the shared ledger lands,
    an adapter implementing this protocol replaces the local default.
    """

    def append(self, entry: AttemptRecord) -> str:
        """Persist one attempt record and return its durable id."""
        ...

    def records(self) -> tuple[AttemptRecord, ...]:
        """Return all persisted attempt records in append order."""
        ...


class InMemoryAttemptLedger:
    """Deterministic in-memory attempt ledger used as the local substrate."""

    def __init__(self) -> None:
        self._entries: list[AttemptRecord] = []

    def append(self, entry: AttemptRecord) -> str:
        self._entries.append(entry)
        return entry.attempt_id

    def records(self) -> tuple[AttemptRecord, ...]:
        return tuple(self._entries)


TriageStage = Literal["sanity", "placebo", "plausibility"]
TRIAGE_STAGES: tuple[TriageStage, ...] = ("sanity", "placebo", "plausibility")
TriageOutcome = Literal["passed", "failed", "inconclusive"]
_TRIAGE_OUTCOMES = frozenset({"passed", "failed", "inconclusive"})


@dataclass(frozen=True, slots=True)
class TriageRequest:
    """One cheap check request for one candidate at one stage."""

    candidate: DiscoveryCandidate
    stage: TriageStage
    seed: int

    def __post_init__(self) -> None:
        if not isinstance(self.candidate, DiscoveryCandidate):
            raise DiscoveryError("triage candidate must be a DiscoveryCandidate")
        if self.stage not in TRIAGE_STAGES:
            raise DiscoveryError("triage stage must be a supported stage")
        if (
            not isinstance(self.seed, int)
            or isinstance(self.seed, bool)
            or self.seed < 0
        ):
            raise DiscoveryError("triage seed must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class TriageResult:
    """The deterministic outcome of one cheap check."""

    stage: TriageStage
    outcome: TriageOutcome
    diagnostics: tuple[str, ...] = ()
    detail: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.stage not in TRIAGE_STAGES:
            raise DiscoveryError("triage stage must be a supported stage")
        if self.outcome not in _TRIAGE_OUTCOMES:
            raise DiscoveryError(
                "triage outcome must be passed, failed, or inconclusive"
            )
        diagnostics = tuple(self.diagnostics)
        if any(not isinstance(item, str) for item in diagnostics):
            raise DiscoveryError("triage diagnostics must contain strings")
        if not isinstance(self.detail, Mapping):
            raise DiscoveryError("triage detail must be a mapping")
        object.__setattr__(self, "diagnostics", diagnostics)
        object.__setattr__(self, "detail", MappingProxyType(dict(self.detail)))

    def as_dict(self) -> dict[str, object]:
        """Return the portable triage outcome representation."""
        return {
            "detail": dict(self.detail),
            "diagnostics": list(self.diagnostics),
            "outcome": self.outcome,
            "stage": self.stage,
        }


class DiscoveryTriage(Protocol):
    """Injected boundary for cheap sanity, placebo, and plausibility checks."""

    def run(self, request: TriageRequest) -> TriageResult:
        """Run one cheap check and return its explicit outcome."""
        ...


class JsonCandidateComposer:
    """Parse the canonical JSON candidate envelope from model output.

    The envelope is the deterministic generation contract: the model returns
    one JSON object with a ``candidates`` array whose entries carry the
    hypothesis, chosen market/horizon/direction, an explicit research
    rationale, and optional implementation files.
    """

    def compose(self, invocation: InvocationResult) -> tuple[DiscoveryCandidate, ...]:
        """Extract the candidate drafts from one model invocation."""
        output = invocation.response.output
        if not isinstance(output, str) or not output.strip():
            raise DiscoveryGenerationError(
                "model provider returned no inline candidate output"
            )
        try:
            document = json.loads(output)
        except json.JSONDecodeError as error:
            raise DiscoveryGenerationError(
                "model output is not valid JSON candidate content"
            ) from error
        if not isinstance(document, dict):
            raise DiscoveryGenerationError("candidate content must be a JSON object")
        raw_candidates = document.get("candidates")
        if not isinstance(raw_candidates, list):
            raise DiscoveryGenerationError(
                "candidate content requires a 'candidates' array"
            )
        return tuple(self._candidate(entry, invocation) for entry in raw_candidates)

    def _candidate(
        self, entry: object, invocation: InvocationResult
    ) -> DiscoveryCandidate:
        if not isinstance(entry, dict):
            raise DiscoveryGenerationError("each candidate must be a JSON object")
        try:
            return DiscoveryCandidate(
                candidate_id=self._identifier(entry, "candidateId"),
                hypothesis=self._text(entry, "hypothesis", max_length=8_192),
                market=self._identifier(entry, "market"),
                horizon=self._identifier(entry, "horizon"),
                direction=self._identifier(entry, "direction"),
                rationale=self._rationale(entry),
                cost=self._cost(entry),
                source=self._files(entry, "files"),
                tests=self._files(entry, "tests"),
                harness_configuration=self._configuration(entry),
                model_invocation_id=invocation.provenance.invocation_id,
            )
        except SearchPolicyError as error:
            raise DiscoveryGenerationError(
                f"model returned an invalid discovery candidate: {error}"
            ) from error

    def _rationale(self, entry: Mapping[str, object]) -> SearchRationale:
        raw = entry.get("rationale")
        if not isinstance(raw, dict):
            raise DiscoveryGenerationError("candidate requires a rationale object")
        kind_text = raw.get("kind")
        if kind_text not in (
            RationaleKind.ECONOMIC_PREMISE.value,
            RationaleKind.NOVELTY.value,
        ):
            raise DiscoveryGenerationError(
                "candidate rationale kind must be 'economic-premise' or 'novelty'"
            )
        statement = raw.get("statement")
        if not isinstance(statement, str) or not statement.strip():
            raise DiscoveryGenerationError("candidate rationale requires a statement")
        references = raw.get("references", ())
        if references is None:
            references = ()
        if isinstance(references, str) or not isinstance(references, list):
            raise DiscoveryGenerationError(
                "candidate rationale references must be a list"
            )
        for reference in references:
            if (
                not isinstance(reference, str)
                or _IDENTIFIER.fullmatch(reference) is None
            ):
                raise DiscoveryGenerationError(
                    "candidate rationale references must be safe identifiers"
                )
        return SearchRationale(
            kind=RationaleKind(kind_text),
            statement=statement,
            references=tuple(references),
        )

    def _cost(self, entry: Mapping[str, object]) -> int:
        value = entry.get("estimatedCost", 3)
        if not isinstance(value, int) or isinstance(value, bool):
            raise DiscoveryGenerationError("candidate estimatedCost must be an integer")
        return value

    def _files(self, entry: Mapping[str, object], key: str) -> Mapping[str, str] | None:
        value = entry.get(key)
        if value is None:
            return None
        if not isinstance(value, dict) or not value:
            raise DiscoveryGenerationError(
                f"candidate {key} must be a non-empty object"
            )
        for path, content in value.items():
            if not isinstance(path, str) or not path.strip():
                raise DiscoveryGenerationError(f"candidate {key} paths must be text")
            if not isinstance(content, str):
                raise DiscoveryGenerationError(f"candidate {key} content must be text")
        return value

    def _configuration(self, entry: Mapping[str, object]) -> Mapping[str, object]:
        value = entry.get("harnessConfiguration")
        if value is None:
            return MappingProxyType({})
        if not isinstance(value, dict):
            raise DiscoveryGenerationError(
                "candidate harnessConfiguration must be an object"
            )
        return MappingProxyType(value)

    def _identifier(self, entry: Mapping[str, object], key: str) -> str:
        value = entry.get(key)
        if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
            raise DiscoveryGenerationError(f"candidate {key} must be a safe identifier")
        return value

    def _text(self, entry: Mapping[str, object], key: str, *, max_length: int) -> str:
        value = entry.get(key)
        if not isinstance(value, str) or not value.strip():
            raise DiscoveryGenerationError(f"candidate {key} must be non-empty text")
        if len(value) > max_length or any(ord(c) < 32 for c in value):
            raise DiscoveryGenerationError(
                f"candidate {key} contains unsupported characters"
            )
        return value


class DiscoveryOutcomeKind(StrEnum):
    """The explicit terminal outcome of one discovery run."""

    IMPLEMENTED = "implemented"
    NEGATIVE = "negative"
    BUDGET_EXHAUSTED = "budget-exhausted"
    UNRESOLVED_UNCERTAINTY = "unresolved-uncertainty"


_STOP_OUTCOMES = frozenset(
    {DiscoveryOutcomeKind.BUDGET_EXHAUSTED, DiscoveryOutcomeKind.UNRESOLVED_UNCERTAINTY}
)


@dataclass(frozen=True, slots=True)
class DiscoveryReport:
    """The complete, labeled outcome record for one discovery run."""

    workspace_id: str
    execution_id: str
    scope: DiscoveryScope
    outcome: DiscoveryOutcomeKind
    budget: DiscoveryBudget
    usage: BudgetUsage
    counts: Mapping[str, int]
    generation_rounds: int
    empty_generation_rounds: int
    model_invocation_ids: tuple[str, ...]
    implemented: tuple[Mapping[str, object], ...]
    negative_findings: tuple[str, ...]
    attempts: tuple[Mapping[str, object], ...]
    pending_candidate_ids: tuple[str, ...]
    limitations: tuple[str, ...]
    schema: str = _REPORT_SCHEMA

    def __post_init__(self) -> None:
        _workspace_id(self.workspace_id)
        _identifier(self.execution_id, "execution_id")
        if not isinstance(self.scope, DiscoveryScope):
            raise DiscoveryError("report scope must be a DiscoveryScope")
        if not isinstance(self.outcome, DiscoveryOutcomeKind):
            raise DiscoveryError("report outcome must be a DiscoveryOutcomeKind")
        if not isinstance(self.budget, DiscoveryBudget):
            raise DiscoveryError("report budget must be a DiscoveryBudget")
        if not isinstance(self.usage, BudgetUsage):
            raise DiscoveryError("report usage must be a BudgetUsage")
        if not isinstance(self.counts, Mapping):
            raise DiscoveryError("report counts must be a mapping")
        object.__setattr__(self, "counts", MappingProxyType(dict(self.counts)))
        for finding in self.negative_findings:
            _text(finding, "negative finding", max_length=4_096)
        object.__setattr__(self, "limitations", _limitations(self.limitations))

    def as_dict(self) -> dict[str, object]:
        """Return the portable report representation for transport and Git."""
        return {
            "attempts": [dict(attempt) for attempt in self.attempts],
            "budget": self.budget.as_dict(),
            "counts": dict(self.counts),
            "emptyGenerationRounds": self.empty_generation_rounds,
            "executionId": self.execution_id,
            "generationRounds": self.generation_rounds,
            "implemented": [dict(entry) for entry in self.implemented],
            "limitations": list(self.limitations),
            "modelInvocationIds": list(self.model_invocation_ids),
            "negativeFindings": list(self.negative_findings),
            "outcome": self.outcome.value,
            "pendingCandidateIds": list(self.pending_candidate_ids),
            "schema": self.schema,
            "scope": self.scope.as_dict(),
            "usage": self.usage.as_dict(),
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


def _limitations(values: Sequence[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise DiscoveryError("limitations must contain non-empty strings")
        normalized.append(value.strip())
    normalized.extend((DISCOVERY_DISCLOSURE, MULTIPLE_TESTING_DISCLOSURE))
    return tuple(sorted(dict.fromkeys(normalized)))


@dataclass(frozen=True, slots=True)
class DiscoveryState:
    """The immutable state snapshot after each workflow transition."""

    workspace_id: str
    execution_id: str
    phase: DiscoveryPhase
    intent_epoch: int
    transitions: tuple[PhaseTransition, ...] = ()
    scope: DiscoveryScope | None = None
    usage: BudgetUsage = field(default_factory=BudgetUsage)
    attempts: tuple[AttemptRecord, ...] = ()
    candidates: tuple[DiscoveryCandidate, ...] = ()
    ordered: tuple[DiscoveryCandidate, ...] = ()
    generation_rounds: int = 0
    empty_generation_rounds: int = 0
    model_provenance: tuple[Mapping[str, object], ...] = ()
    tool_correlation_ids: tuple[str, ...] = ()
    backtests: tuple[BacktestRunResult, ...] = ()
    implemented: tuple[Mapping[str, object], ...] = ()
    uncertainty_seen: bool = False
    stop_pending: bool = False
    report: DiscoveryReport | None = None
    journal_id: str | None = None
    error_message: str | None = None
    detail: str | None = None
    fenced: bool = False


@dataclass(frozen=True, slots=True)
class DiscoveryOutcome:
    """The detached result of one workflow run, safe for UI transport."""

    workspace_id: str
    execution_id: str
    phase: DiscoveryPhase
    fenced: bool
    transitions: tuple[PhaseTransition, ...]
    usage: BudgetUsage
    attempts: tuple[AttemptRecord, ...]
    report: DiscoveryReport | None = None
    journal_id: str | None = None
    detail: str | None = None

    def as_dict(self) -> dict[str, object]:
        """Return the transport representation of the outcome."""
        return {
            "attempts": [attempt.as_dict() for attempt in self.attempts],
            "detail": self.detail,
            "executionId": self.execution_id,
            "fenced": self.fenced,
            "journalId": self.journal_id,
            "phase": self.phase.value,
            "report": None if self.report is None else self.report.as_dict(),
            "transitions": [item.as_dict() for item in self.transitions],
            "usage": self.usage.as_dict(),
            "workspaceId": self.workspace_id,
        }


class DiscoveryWorkflow:
    """Orchestrate the bounded autonomous strategy-discovery workflow."""

    def __init__(
        self,
        *,
        chat: ChatService,
        router: ModelRouter,
        validator: StrategySandboxValidator,
        runner: BacktestRunner,
        triage: DiscoveryTriage,
        policy: SearchPolicy,
        composer: JsonCandidateComposer | None = None,
        ledger: AttemptLedger | None = None,
        journal: CommandJournal | None = None,
        clock: Callable[[], datetime] = _utc_now,
        id_factory: Callable[[str], str] = _new_id,
        role: Role = "research",
        experiment_id: str | None = None,
    ) -> None:
        self._chat = chat
        self._router = router
        self._validator = validator
        self._runner = runner
        self._triage = triage
        self._policy = policy
        self._composer = JsonCandidateComposer() if composer is None else composer
        self._ledger = ledger
        self._journal = journal
        self._clock = clock
        self._id_factory = id_factory
        self._role = role
        self._experiment_id = experiment_id

    @property
    def definition(self) -> WorkflowDefinition:
        """Return the versioned workflow definition registered in chat."""
        return DISCOVERY_WORKFLOW

    @property
    def policy(self) -> SearchPolicy:
        """Return the bounded search policy guarding this workflow."""
        return self._policy

    def launch(
        self,
        workspace_id: str,
        chat_id: str,
        *,
        actor_id: str,
        objective: str,
        markets: Sequence[str],
        horizons: Sequence[str],
        directions: Sequence[str],
        investigation_id: str | None = None,
    ) -> DiscoveryRequest:
        """Persist the chat trigger and return the runnable request."""
        execution = self._chat.launch_workflow(
            workspace_id,
            chat_id,
            workflow=self.definition,
            actor_id=actor_id,
            body=objective,
            investigation_id=investigation_id,
        )
        return DiscoveryRequest(
            workspace_id=workspace_id,
            chat_id=chat_id,
            execution_id=execution.execution_id,
            trigger_message_id=execution.trigger_message_id,
            requested_by=actor_id,
            objective=objective,
            markets=markets,
            horizons=horizons,
            directions=directions,
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

    def run(self, request: DiscoveryRequest) -> DiscoveryOutcome:
        """Execute the documented phases once for this request snapshot."""
        state = DiscoveryState(
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
            state = self._advance(state, _SCOPING, "discovery request accepted")
            scope = DiscoveryScope(
                markets=tuple(request.markets),
                horizons=tuple(request.horizons),
                directions=tuple(request.directions),
            )
            state = replace(state, scope=scope)
            state = self._advance(
                state, _GENERATING, "scope validated within platform budget"
            )
            state = self._generate(request, state)
            state = self._advance(state, _RANKING, "generation rounds complete")
            state = self._rank(state)
            if state.ordered:
                state = self._evaluate(request, state)
            state = self._advance(state, _RECORDING, "candidate evaluation complete")
            report = self._report(state)
            journal_id = self._record(request, state, report)
            state = replace(state, report=report, journal_id=journal_id)
            outcome_kind = self._outcome_kind(state)
            if outcome_kind in _STOP_OUTCOMES:
                self._finish_execution(
                    request,
                    WorkflowExecutionState.STOPPED,
                    ExecutionEventType.WORKFLOW_STOPPED,
                    payload=self._completion_payload(state, report, outcome_kind),
                )
                state = self._advance(
                    state, _STOPPED, f"discovery stopped: {outcome_kind.value}"
                )
            else:
                self._finish_execution(
                    request,
                    WorkflowExecutionState.COMPLETED,
                    ExecutionEventType.WORKFLOW_COMPLETED,
                    payload=self._completion_payload(state, report, outcome_kind),
                )
                state = self._advance(state, _COMPLETED, "discovery outcome recorded")
        except _Fenced as fence:
            state = self._pause(
                fence.state if fence.state is not None else state, fence
            )
        except (
            BacktestRunError,
            DiscoveryError,
            HarnessError,
            ModelInvocationError,
            StrategyBundleError,
            StrategyValidationError,
        ) as error:
            state = self._fail(request, state, error)
        return self._outcome(state)

    # -- phases ------------------------------------------------------------

    def _generate(
        self, request: DiscoveryRequest, state: DiscoveryState
    ) -> DiscoveryState:
        """Run bounded generation rounds until a budget or the model stops."""
        scope = state.scope
        if scope is None:
            raise DiscoveryError("generation requires a validated scope")
        usage = state.usage
        rounds = 0
        empty_rounds = 0
        while True:
            candidate_decision = self._policy.authorize("candidate", usage)
            if not candidate_decision.granted:
                break
            invocation_decision = self._policy.authorize("model_invocation", usage)
            if not invocation_decision.granted:
                break
            try:
                self._require_current(request, state)
            except _Fenced as fence:
                raise _Fenced(str(fence), state) from fence
            rounds += 1
            context = assemble_context(
                workspace_scope=request.workspace_id,
                task=self._generation_task(request, scope, state.candidates, usage),
                role=f"Thesis {self._role} discovery researcher",
                context_id=f"{request.execution_id}-generation-{rounds}",
            )
            invocation = self._router.invoke(self._role, context)
            usage = usage.consumed("model_invocation")
            state = replace(
                state,
                usage=usage,
                model_provenance=(
                    *state.model_provenance,
                    invocation.provenance.as_dict(),
                ),
            )
            drafts = self._composer.compose(invocation)
            if not drafts:
                empty_rounds += 1
                break
            for draft in drafts:
                state, usage = self._admit(state, draft, usage)
            if usage.candidates_generated >= self._policy.budget.max_candidates:
                break
        return replace(
            state,
            usage=usage,
            generation_rounds=rounds,
            empty_generation_rounds=empty_rounds,
        )

    def _admit(
        self,
        state: DiscoveryState,
        draft: DiscoveryCandidate,
        usage: BudgetUsage,
    ) -> tuple[DiscoveryState, BudgetUsage]:
        """Admit one drafted candidate or record its explicit rejection."""
        scope = state.scope
        if scope is None:
            raise DiscoveryError("admission requires a validated scope")
        if any(
            candidate.candidate_id == draft.candidate_id
            for candidate in state.candidates
        ):
            state = self._record_attempt(
                state,
                draft,
                AttemptKind.REJECTED,
                SearchRationale(
                    kind=RationaleKind.REJECTION,
                    statement=(
                        f"duplicate candidate id {draft.candidate_id} "
                        "was already admitted"
                    ),
                ),
            )
            return state, usage
        if not scope.admits(draft.market, draft.horizon, draft.direction):
            state = self._record_attempt(
                state,
                draft,
                AttemptKind.REJECTED,
                SearchRationale(
                    kind=RationaleKind.REJECTION,
                    statement=(
                        f"candidate direction {draft.market}/{draft.horizon}/"
                        f"{draft.direction} is outside the declared workspace scope"
                    ),
                ),
            )
            return state, usage
        if usage.candidates_generated >= self._policy.budget.max_candidates:
            state = self._record_attempt(
                state,
                draft,
                AttemptKind.REJECTED,
                SearchRationale(
                    kind=RationaleKind.REJECTION,
                    statement=(
                        "candidate budget exhausted: "
                        f"{usage.candidates_generated} of "
                        f"{self._policy.budget.max_candidates} candidates admitted"
                    ),
                ),
            )
            return state, usage
        usage = usage.consumed("candidate")
        state = replace(state, candidates=(*state.candidates, draft))
        state = self._record_attempt(
            state, draft, AttemptKind.GENERATED, draft.rationale
        )
        return state, usage

    def _rank(self, state: DiscoveryState) -> DiscoveryState:
        """Rank admitted candidates cheap-to-expensive through the policy."""
        plan = self._policy.rank(state.candidates)
        state = replace(state, ordered=plan.ordered)
        for candidate in plan.ordered:
            state = self._record_attempt(
                state,
                candidate,
                AttemptKind.RANKED,
                SearchRationale(
                    kind=RationaleKind.RANKING,
                    statement=(
                        f"ranked {candidate.rank} by cheap-to-expensive cost "
                        "ordering with a deterministic identifier tie-break"
                    ),
                ),
                {"cost": candidate.cost, "rank": candidate.rank},
            )
        for duplicate in plan.duplicates:
            state = self._record_attempt(
                state,
                duplicate,
                AttemptKind.REJECTED,
                SearchRationale(
                    kind=RationaleKind.REJECTION,
                    statement=(
                        f"duplicate candidate id {duplicate.candidate_id} "
                        "collapsed during ranking"
                    ),
                ),
            )
        return state

    def _evaluate(
        self, request: DiscoveryRequest, state: DiscoveryState
    ) -> DiscoveryState:
        """Triage and, for survivors, backtest each ranked candidate."""
        usage = state.usage
        stop_pending = False
        for candidate in state.ordered:
            try:
                state = self._advance(
                    state,
                    _TRIAGING,
                    f"candidate {candidate.candidate_id} enters bounded triage",
                )
                state, verdict = self._triage_candidate(request, state, candidate)
                if verdict is not None:
                    state = replace(
                        state,
                        uncertainty_seen=(
                            state.uncertainty_seen or verdict == "inconclusive"
                        ),
                    )
                    continue
                if candidate.source is None or candidate.tests is None:
                    state = self._record_attempt(
                        state,
                        candidate,
                        AttemptKind.ABORTED,
                        SearchRationale(
                            kind=RationaleKind.ABORT,
                            statement=(
                                "candidate did not provide an implementation "
                                "to validate"
                            ),
                        ),
                    )
                    continue
                decision = self._policy.authorize("backtest", usage)
                if not decision.granted:
                    stop_pending = True
                    break
                state, usage = self._implement(request, state, candidate, usage)
            except _Fenced as fence:
                raise _Fenced(str(fence), state) from fence
        return replace(state, usage=usage, stop_pending=stop_pending)

    def _triage_candidate(
        self,
        request: DiscoveryRequest,
        state: DiscoveryState,
        candidate: DiscoveryCandidate,
    ) -> tuple[DiscoveryState, str | None]:
        """Run the cheap checks in order and record rejections and aborts.

        Returns the updated state and ``None`` when the candidate passed
        every stage, ``"failed"`` when it was rejected, and
        ``"inconclusive"`` when it was aborted for unresolved uncertainty.
        """
        seed = self._policy.candidate_seed(candidate.candidate_id)
        for stage in TRIAGE_STAGES:
            self._require_current(request, state)
            result = self._triage.run(
                TriageRequest(candidate=candidate, stage=stage, seed=seed)
            )
            if result.outcome == "failed":
                state = self._record_attempt(
                    state,
                    candidate,
                    AttemptKind.REJECTED,
                    SearchRationale(
                        kind=RationaleKind.REJECTION,
                        statement=f"candidate failed the {stage} check",
                    ),
                    {"diagnostics": list(result.diagnostics), "stage": stage},
                )
                return state, "failed"
            if result.outcome == "inconclusive":
                state = self._record_attempt(
                    state,
                    candidate,
                    AttemptKind.ABORTED,
                    SearchRationale(
                        kind=RationaleKind.ABORT,
                        statement=(
                            f"unresolved uncertainty: the {stage} check was "
                            "inconclusive"
                        ),
                    ),
                    {"diagnostics": list(result.diagnostics), "stage": stage},
                )
                return state, "inconclusive"
        return state, None

    def _implement(
        self,
        request: DiscoveryRequest,
        state: DiscoveryState,
        candidate: DiscoveryCandidate,
        usage: BudgetUsage,
    ) -> tuple[DiscoveryState, BudgetUsage]:
        """Validate the candidate bundle and run one harness backtest."""
        state = self._advance(
            state,
            _BACKTESTING,
            f"candidate {candidate.candidate_id} authorized for harness backtest",
        )
        try:
            bundle = self._bundle(candidate)
            validate_strategy_bundle(bundle, self._validator)
        except (StrategyBundleError, StrategyValidationError) as error:
            state = self._record_attempt(
                state,
                candidate,
                AttemptKind.ABORTED,
                SearchRationale(
                    kind=RationaleKind.ABORT,
                    statement=f"sandbox validation rejected the candidate: {error}",
                ),
            )
            return state, usage
        usage = usage.consumed("backtest")
        state = replace(state, usage=usage)
        correlation_id = (
            f"{_TOOL_CORRELATION_PREFIX}-{request.execution_id}-"
            f"{candidate.candidate_id}"
        )
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
        state = replace(
            state, tool_correlation_ids=(*state.tool_correlation_ids, correlation_id)
        )
        result = self._runner.run(
            BacktestRunRequest(
                run_id=job.correlation_id,
                bundle=bundle,
                workflow_execution_id=request.execution_id,
                experiment_id=self._experiment_id
                or f"experiment-{request.execution_id}",
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
        state = replace(state, backtests=(*state.backtests, result))
        try:
            self._require_current(request, state)
        except _Fenced as fence:
            raise _Fenced(str(fence), state) from fence
        if result.succeeded:
            recorded = self._record_attempt(
                state,
                candidate,
                AttemptKind.IMPLEMENTED,
                SearchRationale(
                    kind=RationaleKind.IMPLEMENTATION,
                    statement=(
                        f"candidate passed bounded triage and harness "
                        f"backtest {result.job_id}"
                    ),
                    references=(
                        f"job:{result.job_id}",
                        f"sha256:{bundle.bundle_sha256}",
                    ),
                ),
                {
                    "bundleSha256": bundle.bundle_sha256,
                    "jobId": result.job_id,
                    "metrics": result.metrics,
                },
            )
            summary: dict[str, object] = {
                "attemptId": recorded.attempts[-1].attempt_id,
                "backtestState": result.state,
                "bundleSha256": bundle.bundle_sha256,
                "candidateId": candidate.candidate_id,
                "jobId": result.job_id,
                "metrics": result.metrics,
                "strategyId": bundle.strategy_id,
                "version": bundle.version,
            }
            return (
                replace(recorded, implemented=(*recorded.implemented, summary)),
                usage,
            )
        return (
            self._record_attempt(
                state,
                candidate,
                AttemptKind.ABORTED,
                SearchRationale(
                    kind=RationaleKind.ABORT,
                    statement=(
                        f"harness backtest {result.job_id} ended in state "
                        f"{result.state!r}"
                    ),
                    references=(f"job:{result.job_id}",),
                ),
            ),
            usage,
        )

    def _bundle(self, candidate: DiscoveryCandidate) -> StrategyBundle:
        configuration = dict(candidate.harness_configuration)
        configuration.setdefault(
            "seed", self._policy.candidate_seed(candidate.candidate_id)
        )
        return StrategyBundle.create(
            strategy_id=candidate.candidate_id,
            version=_CANDIDATE_VERSION,
            source=dict(candidate.source or {}),
            tests=dict(candidate.tests or {}),
            harness_configuration=configuration,
        )

    # -- reporting ---------------------------------------------------------

    def _report(self, state: DiscoveryState) -> DiscoveryReport:
        scope = state.scope
        if scope is None:
            raise DiscoveryError("reporting requires a validated scope")
        counts = {kind.value: 0 for kind in AttemptKind}
        attempted: set[str] = set()
        for record in state.attempts:
            counts[record.kind.value] += 1
            if record.kind in _ATTEMPTED_KINDS:
                attempted.add(record.candidate_id)
        pending = tuple(
            candidate.candidate_id
            for candidate in state.ordered
            if candidate.candidate_id not in attempted
        )
        return DiscoveryReport(
            workspace_id=state.workspace_id,
            execution_id=state.execution_id,
            scope=scope,
            outcome=self._outcome_kind(state),
            budget=self._policy.budget,
            usage=state.usage,
            counts=counts,
            generation_rounds=state.generation_rounds,
            empty_generation_rounds=state.empty_generation_rounds,
            model_invocation_ids=tuple(
                str(provenance.get("invocationId"))
                for provenance in state.model_provenance
                if provenance.get("invocationId") is not None
            ),
            implemented=state.implemented,
            negative_findings=self._negative_findings(state, pending),
            attempts=tuple(record.as_dict() for record in state.attempts),
            pending_candidate_ids=pending,
            limitations=(),
        )

    def _outcome_kind(self, state: DiscoveryState) -> DiscoveryOutcomeKind:
        """Return the explicit terminal outcome for this run."""
        attempted = {
            record.candidate_id
            for record in state.attempts
            if record.kind in _ATTEMPTED_KINDS
        }
        if any(candidate.candidate_id not in attempted for candidate in state.ordered):
            return DiscoveryOutcomeKind.BUDGET_EXHAUSTED
        if any(record.kind is AttemptKind.IMPLEMENTED for record in state.attempts):
            return DiscoveryOutcomeKind.IMPLEMENTED
        if state.uncertainty_seen:
            return DiscoveryOutcomeKind.UNRESOLVED_UNCERTAINTY
        return DiscoveryOutcomeKind.NEGATIVE

    def _negative_findings(
        self, state: DiscoveryState, pending: tuple[str, ...]
    ) -> tuple[str, ...]:
        findings = [
            f"{record.candidate_id} [{record.kind.value}]: {record.rationale.statement}"
            for record in state.attempts
            if record.kind in (AttemptKind.REJECTED, AttemptKind.ABORTED)
        ]
        if not state.candidates:
            findings.append("no candidates were generated within budget")
        if pending:
            findings.append(
                f"{len(pending)} ranked candidates remain unevaluated: "
                + ", ".join(pending)
            )
        return tuple(findings)

    def _record(
        self,
        request: DiscoveryRequest,
        state: DiscoveryState,
        report: DiscoveryReport,
    ) -> str | None:
        if self._journal is None:
            return None
        definition = self.definition
        journal_id = f"workflow-{request.execution_id}"
        self._journal.accept(
            "workflow.strategy_discovery.completed",
            {
                "executionId": request.execution_id,
                "requestedBy": request.requested_by,
                "triggerMessageId": request.trigger_message_id,
                "workflow": {
                    "id": definition.workflow_id,
                    "version": definition.version,
                },
            },
            {"phase": state.phase.value, "report": report.as_dict()},
            workflow_hash=content_hash(
                {"id": definition.workflow_id, "version": definition.version}
            ),
            model_hash=content_hash([dict(item) for item in state.model_provenance]),
            artifacts=(
                ArtifactRef.from_bytes(
                    f"reports/{request.execution_id}/discovery.json",
                    report.to_bytes(),
                    media_type=_REPORT_MEDIA_TYPE,
                ),
            ),
            tool_correlation_ids=state.tool_correlation_ids,
            idempotency_id=journal_id,
        )
        return journal_id

    def _record_attempt(
        self,
        state: DiscoveryState,
        candidate: DiscoveryCandidate,
        kind: AttemptKind,
        rationale: SearchRationale,
        detail: Mapping[str, object] | None = None,
    ) -> DiscoveryState:
        """Append one candidate-accounting record to state and the ledger."""
        record = AttemptRecord(
            attempt_id=self._id_factory("attempt"),
            sequence=len(state.attempts) + 1,
            candidate_id=candidate.candidate_id,
            kind=kind,
            rationale=rationale,
            at=self._clock(),
            intent_epoch=state.intent_epoch,
            detail=dict(detail or {}),
        )
        if self._ledger is not None:
            self._ledger.append(record)
        return replace(state, attempts=(*state.attempts, record))

    def _generation_task(
        self,
        request: DiscoveryRequest,
        scope: DiscoveryScope,
        candidates: tuple[DiscoveryCandidate, ...],
        usage: BudgetUsage,
    ) -> str:
        prior = ", ".join(item.candidate_id for item in candidates) or "none"
        remaining = self._policy.budget.max_candidates - usage.candidates_generated
        return (
            f"Discovery objective: {request.objective}\n"
            f"Choose markets from: {', '.join(scope.markets)}\n"
            f"Choose horizons from: {', '.join(scope.horizons)}\n"
            f"Choose strategy directions from: {', '.join(scope.directions)}\n"
            f"Already admitted candidates: {prior}\n"
            f"Remaining candidate budget: {remaining}\n"
            "Return one JSON object with a 'candidates' array. Each candidate "
            "needs candidateId, hypothesis, market, horizon, direction, and a "
            "rationale object with kind 'economic-premise' or 'novelty', a "
            "statement, and references. Optionally include estimatedCost, "
            "files, tests, and harnessConfiguration. Every candidate must "
            "carry an explicit, auditable research rationale."
        )

    # -- state helpers -----------------------------------------------------

    def _advance(
        self,
        state: DiscoveryState,
        target: DiscoveryPhase,
        reason: str,
    ) -> DiscoveryState:
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

    def _pause(self, state: DiscoveryState, fence: _Fenced) -> DiscoveryState:
        detail = str(fence)
        if state.phase in {_COMPLETED, _STOPPED, _FAILED}:
            return replace(state, detail=detail, fenced=True)
        try:
            paused = self._advance(state, _PAUSED, detail)
        except InvalidPhaseTransition:
            return replace(state, detail=detail, fenced=True)
        return replace(paused, detail=detail, fenced=True)

    def _fail(
        self,
        request: DiscoveryRequest,
        state: DiscoveryState,
        error: Exception,
    ) -> DiscoveryState:
        self._finish_execution(
            request,
            WorkflowExecutionState.FAILED,
            ExecutionEventType.WORKFLOW_FAILED,
            payload={"errorMessage": str(error), "state": "failed"},
        )
        failed = self._advance(state, _FAILED, f"workflow failed: {error}")
        return replace(failed, error_message=str(error), detail=str(error))

    def _outcome(self, state: DiscoveryState) -> DiscoveryOutcome:
        return DiscoveryOutcome(
            workspace_id=state.workspace_id,
            execution_id=state.execution_id,
            phase=state.phase,
            fenced=state.fenced,
            transitions=state.transitions,
            usage=state.usage,
            attempts=state.attempts,
            report=state.report,
            journal_id=state.journal_id,
            detail=state.detail,
        )

    # -- durable boundaries -------------------------------------------------

    def _require_execution(self, request: DiscoveryRequest) -> WorkflowExecution:
        try:
            execution = self._chat.get_execution(
                request.workspace_id, request.execution_id
            )
        except RecordNotFound as error:
            raise DiscoveryError(
                f"execution {request.execution_id!r} does not exist"
            ) from error
        if execution.chat_id != request.chat_id:
            raise DiscoveryError("execution does not belong to the request chat")
        if execution.trigger_message_id != request.trigger_message_id:
            raise DiscoveryError("trigger message does not match the execution")
        return execution

    def _check_stale_request(self, request: DiscoveryRequest) -> None:
        execution = self._chat.get_execution(request.workspace_id, request.execution_id)
        if execution.state in _TERMINAL_EXECUTION_STATES:
            raise DiscoveryError(f"execution is already {execution.state.value}")
        if execution.intent_epoch != request.intent_epoch:
            raise _Fenced("request predates newer steering")

    def _require_current(
        self, request: DiscoveryRequest, state: DiscoveryState
    ) -> None:
        execution = self._chat.get_execution(request.workspace_id, request.execution_id)
        if execution.state in _TERMINAL_EXECUTION_STATES:
            raise _Fenced(f"execution became {execution.state.value}")
        if execution.intent_epoch != state.intent_epoch:
            raise _Fenced("steering advanced the intent epoch")

    def _finish_execution(
        self,
        request: DiscoveryRequest,
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
        state: DiscoveryState,
        report: DiscoveryReport | None,
        outcome_kind: DiscoveryOutcomeKind,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "state": state.phase.value,
            "outcome": outcome_kind.value,
        }
        if report is not None:
            payload["report"] = {
                "name": f"reports/{state.execution_id}/discovery.json",
                "sha256": report.content_sha256,
                "schema": _REPORT_SCHEMA,
            }
        return payload


__all__ = [
    "DISCOVERY_DISCLOSURE",
    "DISCOVERY_PHASES",
    "DISCOVERY_WORKFLOW",
    "MULTIPLE_TESTING_DISCLOSURE",
    "TRIAGE_STAGES",
    "AttemptKind",
    "AttemptLedger",
    "AttemptRecord",
    "DiscoveryError",
    "DiscoveryGenerationError",
    "DiscoveryOutcome",
    "DiscoveryOutcomeKind",
    "DiscoveryPhase",
    "DiscoveryReport",
    "DiscoveryRequest",
    "DiscoveryScope",
    "DiscoveryState",
    "DiscoveryTriage",
    "DiscoveryWorkflow",
    "InMemoryAttemptLedger",
    "InvalidPhaseTransition",
    "JsonCandidateComposer",
    "PhaseTransition",
    "TriageOutcome",
    "TriageRequest",
    "TriageResult",
    "TriageStage",
    "transition_phase",
]
