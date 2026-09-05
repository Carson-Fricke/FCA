"""Deterministic strategy-development workflow orchestration.

This module is the first complete Thesis vertical workflow.  It takes a chat
request context, generates a strategy through the policy-driven model router,
validates the resulting content-addressed bundle through the published sandbox
handoff, runs a harness backtest behind an injected runner port, imports the
reproducibility-critical result artifacts, and records the labeled evidence
outcome in the workspace command journal.

Design rules:

- Workflow transitions are explicit and auditable: every move between phases
  goes through :func:`transition_phase` and is recorded as an immutable
  :class:`PhaseTransition`.
- No wall-clock participates in state decisions.  Time and identifiers are
  injected, and steering fences are computed from durable intent epochs only.
- Every external effect sits behind an injected port: model invocation through
  :class:`thesis.agent.router.ModelRouter`, sandbox validation through the
  published :class:`thesis.strategies.validation.StrategySandboxValidator`,
  backtests through :class:`thesis.workflows.backtests.BacktestRunner`, and
  durability through chat persistence, the execution event store, the artifact
  importer, and the command journal.
- The workflow is fully orchestratable without a live model: tests inject a
  deterministic fake provider, validator, and runner.

Steering is immediate and non-destructive: a steering message advances the
durable intent epoch without cancelling in-flight work.  The workflow re-reads
the durable execution between phases; when steering or a stop arrives, it
records any late tool result without stale writes and pauses instead of
recording outcomes that no longer reflect the current intent.
"""

from __future__ import annotations

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
from thesis.artifacts.importer import (
    ArtifactImporter,
    ArtifactImportError,
    HarnessManifest,
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
from thesis.workflows.evidence import EvidenceReport, build_evidence_report

_WORKFLOW_ID = "strategy-development"
_WORKFLOW_VERSION = "1.0.0"
_WORKFLOW_TITLE = "Strategy development"
_TOOL_NAME = "harness.backtest"
_TOOL_CORRELATION_PREFIX = "backtest"
_WORKFLOW_ACTOR = "thesis-workflow"
_REPORT_MEDIA_TYPE = "application/json"
_REPORT_SCHEMA = "fca-thesis-evidence-report-v1"

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
_WORKSPACE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")


class WorkflowError(RuntimeError):
    """Base error for invalid strategy-development workflow operations."""


class InvalidPhaseTransition(WorkflowError):
    """A phase cannot move directly to the requested phase."""


class StrategyGenerationError(WorkflowError):
    """Raised when generated strategy content cannot be turned into a bundle."""


class _Fenced(Exception):
    """Internal signal that a continuation became stale and must pause.

    Carries the partial state recorded so far so in-flight results are not
    lost when the workflow pauses instead of continuing.
    """

    def __init__(
        self,
        message: str,
        state: StrategyDevelopmentState | None = None,
    ) -> None:
        super().__init__(message)
        self.state = state


class _PhaseFailure(WorkflowError):
    """Internal failure signal that carries the partial workflow state."""

    def __init__(self, message: str, state: StrategyDevelopmentState) -> None:
        super().__init__(message)
        self.state = state


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


class StrategyDevelopmentPhase(StrEnum):
    """The explicit, ordered phases of the strategy-development workflow."""

    REQUESTED = "requested"
    GENERATING = "generating"
    VALIDATING = "validating"
    BACKTESTING = "backtesting"
    IMPORTING = "importing"
    RECORDING = "recording"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"


_REQUESTED = StrategyDevelopmentPhase.REQUESTED
_GENERATING = StrategyDevelopmentPhase.GENERATING
_VALIDATING = StrategyDevelopmentPhase.VALIDATING
_BACKTESTING = StrategyDevelopmentPhase.BACKTESTING
_IMPORTING = StrategyDevelopmentPhase.IMPORTING
_RECORDING = StrategyDevelopmentPhase.RECORDING
_COMPLETED = StrategyDevelopmentPhase.COMPLETED
_FAILED = StrategyDevelopmentPhase.FAILED
_PAUSED = StrategyDevelopmentPhase.PAUSED

_TRANSITIONS: Mapping[StrategyDevelopmentPhase, frozenset[StrategyDevelopmentPhase]] = {
    _REQUESTED: frozenset({_GENERATING, _PAUSED, _FAILED}),
    _GENERATING: frozenset({_VALIDATING, _PAUSED, _FAILED}),
    _VALIDATING: frozenset({_BACKTESTING, _PAUSED, _FAILED}),
    _BACKTESTING: frozenset({_IMPORTING, _RECORDING, _PAUSED, _FAILED}),
    _IMPORTING: frozenset({_RECORDING, _PAUSED, _FAILED}),
    _RECORDING: frozenset({_COMPLETED, _FAILED}),
    _PAUSED: frozenset(
        {_GENERATING, _VALIDATING, _BACKTESTING, _IMPORTING, _RECORDING}
    ),
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

STRATEGY_DEVELOPMENT_PHASES: tuple[str, ...] = (
    _REQUESTED.value,
    _GENERATING.value,
    _VALIDATING.value,
    _BACKTESTING.value,
    _IMPORTING.value,
    _RECORDING.value,
)

STRATEGY_DEVELOPMENT_WORKFLOW = WorkflowDefinition(
    workflow_id=_WORKFLOW_ID,
    version=_WORKFLOW_VERSION,
    title=_WORKFLOW_TITLE,
    phases=STRATEGY_DEVELOPMENT_PHASES,
)


def transition_phase(
    current: StrategyDevelopmentPhase, target: StrategyDevelopmentPhase
) -> StrategyDevelopmentPhase:
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


def _text(value: str, label: str, *, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkflowError(f"{label} must be non-empty")
    if len(value) > max_length or any(ord(character) < 32 for character in value):
        raise WorkflowError(f"{label} contains unsupported characters")
    return value


@dataclass(frozen=True, slots=True)
class PhaseTransition:
    """One immutable, ordered record of a workflow phase change."""

    sequence: int
    from_phase: StrategyDevelopmentPhase
    to_phase: StrategyDevelopmentPhase
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
class StrategyDevelopmentRequest:
    """The chat request context that triggers one workflow execution."""

    workspace_id: str
    chat_id: str
    execution_id: str
    trigger_message_id: str
    requested_by: str
    objective: str
    intent_epoch: int = 0
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        _workspace_id(self.workspace_id)
        for name in ("chat_id", "execution_id", "trigger_message_id", "requested_by"):
            _identifier(getattr(self, name), name)
        _text(self.objective, "objective", max_length=8_192)
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
            "createdAt": created_at,
            "executionId": self.execution_id,
            "intentEpoch": self.intent_epoch,
            "objective": self.objective,
            "requestedBy": self.requested_by,
            "triggerMessageId": self.trigger_message_id,
            "workspaceId": self.workspace_id,
        }


@dataclass(frozen=True, slots=True)
class GenerationDraft:
    """Strategy content extracted from one policy-bound model invocation."""

    strategy_id: str
    version: str
    source: Mapping[str, str]
    tests: Mapping[str, str]
    dependencies: tuple[str, ...] = ()
    platform_versions: Mapping[str, object] = field(default_factory=dict)
    harness_configuration: Mapping[str, object] = field(default_factory=dict)
    risk_declarations: Mapping[str, object] = field(default_factory=dict)
    capability_declarations: Mapping[str, object] = field(default_factory=dict)
    limitations: tuple[str, ...] = ()
    harness_manifest_reference: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.strategy_id, "strategy_id")
        _text(self.version, "strategy version", max_length=64)
        for name in ("source", "tests"):
            files = getattr(self, name)
            if not isinstance(files, Mapping) or not files:
                raise StrategyGenerationError(f"{name} must be a non-empty mapping")
            for path, content in files.items():
                if not isinstance(path, str) or not path.strip():
                    raise StrategyGenerationError(f"{name} paths must be non-empty")
                if not isinstance(content, str):
                    raise StrategyGenerationError(
                        f"{name} values must be path-to-text mappings"
                    )
        object.__setattr__(self, "source", MappingProxyType(dict(self.source)))
        object.__setattr__(self, "tests", MappingProxyType(dict(self.tests)))
        dependencies = tuple(self.dependencies)
        if any(not isinstance(item, str) or not item.strip() for item in dependencies):
            raise StrategyGenerationError("dependencies must contain non-empty strings")
        object.__setattr__(
            self, "dependencies", tuple(sorted(dict.fromkeys(dependencies)))
        )
        for name in (
            "platform_versions",
            "harness_configuration",
            "risk_declarations",
            "capability_declarations",
        ):
            value = getattr(self, name)
            if not isinstance(value, Mapping):
                raise StrategyGenerationError(f"{name} must be a mapping")
            object.__setattr__(self, name, MappingProxyType(dict(value)))
        limitations = tuple(self.limitations)
        if any(not isinstance(item, str) or not item.strip() for item in limitations):
            raise StrategyGenerationError("limitations must contain non-empty strings")
        object.__setattr__(
            self, "limitations", tuple(sorted(dict.fromkeys(limitations)))
        )
        if self.harness_manifest_reference is not None:
            _text(
                self.harness_manifest_reference,
                "harness manifest reference",
                max_length=128,
            )


class StrategyComposer(Protocol):
    """Injected boundary that turns model output into strategy content."""

    def compose(self, invocation: InvocationResult) -> GenerationDraft:
        """Extract the strategy draft from one model invocation."""


class JsonStrategyComposer:
    """Parse the canonical JSON strategy envelope from model output.

    The envelope is the deterministic Phase 1 generation contract: the model
    returns one JSON object with ``strategyId``, ``version``, ``files``, and
    ``tests`` keys plus optional bundle metadata.  A live coding deployment can
    replace this composer through the same protocol.
    """

    def compose(self, invocation: InvocationResult) -> GenerationDraft:
        output = invocation.response.output
        if not isinstance(output, str) or not output.strip():
            raise StrategyGenerationError(
                "model provider returned no inline strategy output"
            )
        try:
            document = json.loads(output)
        except json.JSONDecodeError as error:
            raise StrategyGenerationError(
                "model output is not valid JSON strategy content"
            ) from error
        if not isinstance(document, dict):
            raise StrategyGenerationError("strategy content must be a JSON object")
        files = document.get("files")
        tests = document.get("tests")
        strategy_id = document.get("strategyId")
        version = document.get("version")
        if not isinstance(files, dict) or not files:
            raise StrategyGenerationError("strategy content requires source files")
        if not isinstance(tests, dict) or not tests:
            raise StrategyGenerationError("strategy content requires tests")
        if not isinstance(strategy_id, str) or not strategy_id.strip():
            raise StrategyGenerationError("strategy content requires strategyId")
        if not isinstance(version, str) or not version.strip():
            raise StrategyGenerationError("strategy content requires version")
        return GenerationDraft(
            strategy_id=strategy_id,
            version=version,
            source=files,
            tests=tests,
            dependencies=_strings(document.get("dependencies")),
            platform_versions=_mapping(document, "platformVersions"),
            harness_configuration=_mapping(document, "harnessConfiguration"),
            risk_declarations=_mapping(document, "riskDeclarations"),
            capability_declarations=_mapping(document, "capabilityDeclarations"),
            limitations=_strings(document.get("limitations")),
            harness_manifest_reference=_optional_text(
                document.get("harnessManifestReference")
            ),
        )


def _strings(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise StrategyGenerationError("envelope lists must contain strings")
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise StrategyGenerationError("envelope lists must contain strings")
    return tuple(dict.fromkeys(value))


def _mapping(document: Mapping[str, object], key: str) -> Mapping[str, object]:
    value = document.get(key)
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise StrategyGenerationError(f"{key} must be a JSON object")
    return MappingProxyType(dict(value))


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise StrategyGenerationError("optional text must be non-empty when set")
    return value.strip()


@dataclass(frozen=True, slots=True)
class StrategyDevelopmentState:
    """The immutable state snapshot after each workflow transition."""

    workspace_id: str
    execution_id: str
    phase: StrategyDevelopmentPhase
    intent_epoch: int
    transitions: tuple[PhaseTransition, ...] = ()
    draft: GenerationDraft | None = None
    bundle: StrategyBundle | None = None
    validation_id: str | None = None
    backtest: BacktestRunResult | None = None
    import_receipt: Mapping[str, object] | None = None
    report: EvidenceReport | None = None
    journal_id: str | None = None
    model_provenance: Mapping[str, object] | None = None
    error_message: str | None = None
    detail: str | None = None
    fenced: bool = False


@dataclass(frozen=True, slots=True)
class StrategyDevelopmentOutcome:
    """The detached result of one workflow run, safe for UI transport."""

    workspace_id: str
    execution_id: str
    phase: StrategyDevelopmentPhase
    fenced: bool
    transitions: tuple[PhaseTransition, ...]
    bundle: StrategyBundle | None = None
    validation_id: str | None = None
    backtest: BacktestRunResult | None = None
    import_receipt: Mapping[str, object] | None = None
    report: EvidenceReport | None = None
    journal_id: str | None = None
    detail: str | None = None

    def as_dict(self) -> dict[str, object]:
        """Return the transport representation of the outcome."""
        return {
            "backtest": None if self.backtest is None else self.backtest.as_dict(),
            "bundleManifest": None if self.bundle is None else self.bundle.manifest(),
            "detail": self.detail,
            "executionId": self.execution_id,
            "fenced": self.fenced,
            "importReceipt": None
            if self.import_receipt is None
            else dict(self.import_receipt),
            "journalId": self.journal_id,
            "phase": self.phase.value,
            "report": None if self.report is None else self.report.as_dict(),
            "transitions": [item.as_dict() for item in self.transitions],
            "validationId": self.validation_id,
            "workspaceId": self.workspace_id,
        }


class StrategyDevelopmentWorkflow:
    """Orchestrate the documented chat-to-evidence strategy workflow."""

    def __init__(
        self,
        *,
        chat: ChatService,
        router: ModelRouter,
        validator: StrategySandboxValidator,
        runner: BacktestRunner,
        composer: StrategyComposer | None = None,
        importer: ArtifactImporter | None = None,
        journal: CommandJournal | None = None,
        clock: Callable[[], datetime] = _utc_now,
        id_factory: Callable[[str], str] = _new_id,
        role: Role = "coding",
        experiment_id: str | None = None,
    ) -> None:
        self._chat = chat
        self._router = router
        self._validator = validator
        self._runner = runner
        self._composer = JsonStrategyComposer() if composer is None else composer
        self._importer = importer
        self._journal = journal
        self._clock = clock
        self._id_factory = id_factory
        self._role = role
        self._experiment_id = experiment_id

    @property
    def definition(self) -> WorkflowDefinition:
        """Return the versioned workflow definition registered in chat."""
        return STRATEGY_DEVELOPMENT_WORKFLOW

    def launch(
        self,
        workspace_id: str,
        chat_id: str,
        *,
        actor_id: str,
        objective: str,
        investigation_id: str | None = None,
    ) -> StrategyDevelopmentRequest:
        """Persist the chat trigger and return the runnable request."""
        execution = self._chat.launch_workflow(
            workspace_id,
            chat_id,
            workflow=self.definition,
            actor_id=actor_id,
            body=objective,
            investigation_id=investigation_id,
        )
        return StrategyDevelopmentRequest(
            workspace_id=workspace_id,
            chat_id=chat_id,
            execution_id=execution.execution_id,
            trigger_message_id=execution.trigger_message_id,
            requested_by=actor_id,
            objective=objective,
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

    def run(self, request: StrategyDevelopmentRequest) -> StrategyDevelopmentOutcome:
        """Execute the documented phases once for this request snapshot."""
        state = StrategyDevelopmentState(
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
            state = self._advance(state, _GENERATING, "chat request accepted")
            draft, provenance = self._generate(request)
            state = replace(
                state,
                draft=draft,
                model_provenance=MappingProxyType(dict(provenance)),
            )
            state = self._advance(state, _VALIDATING, "strategy draft generated")
            bundle, validation_id = self._validate(draft)
            state = replace(state, bundle=bundle, validation_id=validation_id)
            state = self._advance(
                state, _BACKTESTING, "bundle accepted by sandbox validation"
            )
            state = self._backtest(request, state)
            self._require_current(request, state)
            if self._can_import(state):
                state = self._advance(state, _IMPORTING, "harness backtest completed")
                receipt = self._import(request, state)
                state = replace(state, import_receipt=receipt)
                self._require_current(request, state)
            state = self._advance(state, _RECORDING, "backtest artifacts recorded")
            report = self._report(request, state)
            journal_id = self._record(request, state, report)
            state = replace(state, report=report, journal_id=journal_id)
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
            ArtifactImportError,
            BacktestRunError,
            HarnessError,
            StrategyBundleError,
            StrategyGenerationError,
            StrategyValidationError,
            WorkflowError,
        ) as error:
            state = self._fail(request, state, error)
        return self._outcome(state)

    # -- phases ------------------------------------------------------------

    def _generate(
        self, request: StrategyDevelopmentRequest
    ) -> tuple[GenerationDraft, dict[str, object]]:
        context = assemble_context(
            workspace_scope=request.workspace_id,
            task=request.objective,
            role=f"Thesis {self._role} strategy developer",
            context_id=request.execution_id,
        )
        invocation = self._router.invoke(self._role, context)
        draft = self._composer.compose(invocation)
        return draft, invocation.provenance.as_dict()

    def _validate(self, draft: GenerationDraft) -> tuple[StrategyBundle, str | None]:
        bundle = self._bundle(draft)
        result = validate_strategy_bundle(bundle, self._validator)
        return bundle, result.validation_id

    def _bundle(self, draft: GenerationDraft) -> StrategyBundle:
        try:
            return StrategyBundle.create(
                strategy_id=draft.strategy_id,
                version=draft.version,
                source=dict(draft.source),
                tests=dict(draft.tests),
                dependencies=draft.dependencies,
                platform_versions=dict(draft.platform_versions),
                harness_configuration=dict(draft.harness_configuration),
                risk_declarations=dict(draft.risk_declarations),
                capability_declarations=dict(draft.capability_declarations),
                limitations=draft.limitations,
                harness_manifest_reference=draft.harness_manifest_reference,
            )
        except StrategyBundleError as error:
            raise WorkflowError(
                f"generated strategy bundle is invalid: {error}"
            ) from error

    def _backtest(
        self, request: StrategyDevelopmentRequest, state: StrategyDevelopmentState
    ) -> StrategyDevelopmentState:
        bundle = state.bundle
        if bundle is None:
            raise WorkflowError("backtest requires a validated strategy bundle")
        self._require_current(request, state)
        correlation_id = f"{_TOOL_CORRELATION_PREFIX}-{request.execution_id}"
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

    def _can_import(self, state: StrategyDevelopmentState) -> bool:
        return (
            self._importer is not None
            and state.backtest is not None
            and state.backtest.manifest is not None
        )

    def _import(
        self, request: StrategyDevelopmentRequest, state: StrategyDevelopmentState
    ) -> dict[str, object]:
        bundle = state.bundle
        backtest = state.backtest
        if self._importer is None or bundle is None or backtest is None:
            raise WorkflowError("artifact import requires a bundle and a backtest")
        manifest = _manifest(backtest.manifest)
        experiment_id = self._experiment_id or f"experiment-{request.execution_id}"
        receipt = self._importer.import_manifest(
            manifest,
            strategy_version=bundle.version,
            experiment_id=experiment_id,
            workflow_execution_id=request.execution_id,
            source_root=backtest.result_root,
        )
        return {
            "artifacts": [reference.as_dict() for reference in receipt.artifacts],
            "experimentId": receipt.experiment_id,
            "gitCommit": receipt.git_commit,
            "harnessManifest": {
                "id": receipt.harness_manifest_id,
                "sha256": receipt.harness_manifest_sha256,
            },
            "provenanceUri": receipt.provenance_uri,
            "strategyVersion": receipt.strategy_version,
            "workflowExecutionId": receipt.workflow_execution_id,
        }

    def _report(
        self, request: StrategyDevelopmentRequest, state: StrategyDevelopmentState
    ) -> EvidenceReport:
        bundle = state.bundle
        if bundle is None:
            raise WorkflowError("evidence report requires a validated bundle")
        return build_evidence_report(
            workspace_id=request.workspace_id,
            execution_id=request.execution_id,
            bundle=bundle,
            backtest=None if state.backtest is None else state.backtest.as_dict(),
            import_receipt=state.import_receipt,
            journal_id=(
                None if self._journal is None else f"workflow-{request.execution_id}"
            ),
            limitations=(state.draft.limitations if state.draft is not None else ()),
        )

    def _record(
        self,
        request: StrategyDevelopmentRequest,
        state: StrategyDevelopmentState,
        report: EvidenceReport,
    ) -> str | None:
        if self._journal is None:
            return None
        bundle = state.bundle
        if bundle is None:
            raise WorkflowError("journal recording requires a validated bundle")
        definition = self.definition
        journal_id = f"workflow-{request.execution_id}"
        self._journal.accept(
            "workflow.strategy_development.completed",
            {
                "executionId": request.execution_id,
                "requestedBy": request.requested_by,
                "strategyIds": [bundle.strategy_id],
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
                    f"reports/{request.execution_id}/evidence.json",
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
        state: StrategyDevelopmentState,
        target: StrategyDevelopmentPhase,
        reason: str,
    ) -> StrategyDevelopmentState:
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
        self, state: StrategyDevelopmentState, fence: _Fenced
    ) -> StrategyDevelopmentState:
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
        request: StrategyDevelopmentRequest,
        state: StrategyDevelopmentState,
        error: Exception,
    ) -> StrategyDevelopmentState:
        self._finish_execution(
            request,
            WorkflowExecutionState.FAILED,
            ExecutionEventType.WORKFLOW_FAILED,
            payload={"errorMessage": str(error), "state": "failed"},
        )
        failed = self._advance(state, _FAILED, f"workflow failed: {error}")
        return replace(failed, error_message=str(error), detail=str(error))

    def _outcome(self, state: StrategyDevelopmentState) -> StrategyDevelopmentOutcome:
        return StrategyDevelopmentOutcome(
            workspace_id=state.workspace_id,
            execution_id=state.execution_id,
            phase=state.phase,
            fenced=state.fenced,
            transitions=state.transitions,
            bundle=state.bundle,
            validation_id=state.validation_id,
            backtest=state.backtest,
            import_receipt=state.import_receipt,
            report=state.report,
            journal_id=state.journal_id,
            detail=state.detail,
        )

    # -- durable boundaries -------------------------------------------------

    def _require_execution(
        self, request: StrategyDevelopmentRequest
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

    def _check_stale_request(self, request: StrategyDevelopmentRequest) -> None:
        execution = self._chat.get_execution(request.workspace_id, request.execution_id)
        if execution.state in _TERMINAL_EXECUTION_STATES:
            raise WorkflowError(f"execution is already {execution.state.value}")
        if execution.intent_epoch != request.intent_epoch:
            raise _Fenced("request predates newer steering")

    def _require_current(
        self, request: StrategyDevelopmentRequest, state: StrategyDevelopmentState
    ) -> None:
        execution = self._chat.get_execution(request.workspace_id, request.execution_id)
        if execution.state in _TERMINAL_EXECUTION_STATES:
            raise _Fenced(f"execution became {execution.state.value}")
        if execution.intent_epoch != state.intent_epoch:
            raise _Fenced("steering advanced the intent epoch")

    def _finish_execution(
        self,
        request: StrategyDevelopmentRequest,
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
        self, request: StrategyDevelopmentRequest, report: EvidenceReport | None
    ) -> dict[str, object]:
        payload: dict[str, object] = {"state": _COMPLETED.value}
        if report is not None:
            payload["report"] = {
                "name": f"reports/{request.execution_id}/evidence.json",
                "sha256": report.content_sha256,
                "schema": _REPORT_SCHEMA,
            }
        return payload


def _manifest(value: object) -> HarnessManifest:
    if isinstance(value, HarnessManifest):
        return value
    if isinstance(value, Mapping):
        return HarnessManifest.from_mapping(value)
    raise WorkflowError("harness manifest is missing or unsupported")


__all__ = [
    "InvalidPhaseTransition",
    "JsonStrategyComposer",
    "PhaseTransition",
    "STRATEGY_DEVELOPMENT_PHASES",
    "STRATEGY_DEVELOPMENT_WORKFLOW",
    "StrategyComposer",
    "StrategyDevelopmentOutcome",
    "StrategyDevelopmentPhase",
    "StrategyDevelopmentRequest",
    "StrategyDevelopmentState",
    "StrategyDevelopmentWorkflow",
    "StrategyGenerationError",
    "WorkflowError",
    "transition_phase",
]
