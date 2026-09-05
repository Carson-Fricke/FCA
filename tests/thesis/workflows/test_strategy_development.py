from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from itertools import count
from pathlib import Path
from typing import cast

import pytest

from thesis.agent.context import PromptContext
from thesis.agent.router import (
    ModelProfile,
    ModelResponse,
    ModelRouter,
    PolicyBundle,
    Role,
    RolePolicy,
)
from thesis.agent.skills import SkillRegistry, ThesisSkill
from thesis.artifacts.importer import ArtifactImporter, HarnessArtifact, HarnessManifest
from thesis.chat.contracts import ToolJobState, WorkflowExecutionState
from thesis.chat.service import ChatService
from thesis.execution.events import ExecutionEventType
from thesis.integrations import (
    BacktestRequest,
    CapabilityReport,
    CapabilityStatus,
    HarnessBacktestResult,
    ThesisHarnessClient,
)
from thesis.persistence import CommandJournal, GitCoordinator, PublicationService
from thesis.strategies import StrategyBundle
from thesis.strategies.validation import (
    StrategyValidationRequest,
    StrategyValidationResult,
)
from thesis.workflows import (
    EXPLORATORY_DISCLOSURE,
    STRATEGY_DEVELOPMENT_PHASES,
    STRATEGY_DEVELOPMENT_WORKFLOW,
    BacktestMode,
    BacktestRunRequest,
    BacktestRunResult,
    EvidenceReportError,
    HarnessBacktestRunner,
    InvalidPhaseTransition,
    StrategyDevelopmentPhase,
    StrategyDevelopmentRequest,
    StrategyDevelopmentWorkflow,
    transition_phase,
)

WORKSPACE = "workspace-demo"
OWNER = "owner-1"
NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def _clock() -> Callable[[], datetime]:
    ticks = count(1)

    def clock() -> datetime:
        return NOW.replace(microsecond=next(ticks))

    return clock


def _id_factory() -> Callable[[str], str]:
    counter = count(1)
    return lambda prefix: f"{prefix}-{next(counter)}"


def _envelope() -> dict[str, object]:
    return {
        "strategyId": "momentum",
        "version": "1.2.0",
        "files": {
            "strategy.py": (
                "from harness.strategy import Strategy\n\n"
                "class Momentum(Strategy):\n    pass\n"
            )
        },
        "tests": {"test_strategy.py": "def test_strategy():\n    assert True\n"},
        "dependencies": ["numpy==2.1.0"],
        "platformVersions": {"python": "3.12.6", "harness": "1.0.0"},
        "harnessConfiguration": {"seed": 7, "symbols": ["SPY"]},
        "riskDeclarations": {"max_position": "100"},
        "capabilityDeclarations": {"network": "disabled"},
        "limitations": ["single-symbol momentum only"],
    }


class FakeProvider:
    """Deterministic fake model provider behind the policy boundary."""

    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.prompts: list[str] = []
        self.steer: Callable[[], object] | None = None

    def invoke(
        self,
        *,
        profile: ModelProfile,
        prompt: PromptContext,
        allowed_tools: tuple[str, ...],
    ) -> ModelResponse:
        self.prompts.append(prompt.rendered)
        if self.steer is not None:
            steer_once = self.steer
            self.steer = None
            steer_once()
        return ModelResponse(output=json.dumps(self.payload))


class RecordingValidator:
    def __init__(
        self,
        *,
        accepted: bool = True,
        diagnostics: tuple[str, ...] = (),
    ) -> None:
        self.accepted = accepted
        self.diagnostics = diagnostics
        self.requests: list[StrategyValidationRequest] = []

    def validate(self, request: StrategyValidationRequest) -> StrategyValidationResult:
        self.requests.append(request)
        return StrategyValidationResult(
            self.accepted, self.diagnostics, validation_id="validation-1"
        )


class FakeRunner:
    """Deterministic fake backtest runner behind the runner port."""

    def __init__(
        self,
        *,
        manifest: object | None,
        state: str = "completed",
        result_root: str | None = None,
        on_run: Callable[[], object] | None = None,
    ) -> None:
        self.requests: list[BacktestRunRequest] = []
        self.on_run: Callable[[], object] | None = on_run
        self._manifest = manifest
        self._state = state
        self._result_root = result_root

    def run(self, request: BacktestRunRequest) -> BacktestRunResult:
        self.requests.append(request)
        if self.on_run is not None:
            on_run = self.on_run
            self.on_run = None
            on_run()
        return BacktestRunResult(
            job_id="job-1",
            state=self._state,
            mode="local",
            manifest=self._manifest,
            metrics={"sharpe": 1.25, "totalReturn": 0.08},
            result_root=self._result_root,
        )


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ("git", *arguments),
        cwd=repository,
        capture_output=True,
        check=True,
        encoding="utf-8",
        stdin=subprocess.DEVNULL,
        timeout=30,
    )
    return result.stdout.strip()


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "workspace"
    repository.mkdir(parents=True)
    _git(repository, "init", "--quiet")
    _git(repository, "config", "user.name", "Thesis Test")
    _git(repository, "config", "user.email", "thesis@example.invalid")
    (repository / "README.md").write_text("workspace\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "--quiet", "-m", "initial")
    return repository


def _harness_result(tmp_path: Path) -> tuple[HarnessManifest, Path]:
    root = tmp_path
    fills = root / "run-1" / "fills.csv"
    fills.parent.mkdir(parents=True)
    content = b"symbol,qty,price\nSPY,10,500.0\n"
    fills.write_bytes(content)
    artifact = HarnessArtifact(
        name="fills.csv",
        uri="run-1/fills.csv",
        sha256=hashlib.sha256(content).hexdigest(),
        size_bytes=len(content),
        media_type="text/csv",
    )
    manifest = HarnessManifest.from_mapping(
        {
            "manifestId": "manifest-1",
            "artifacts": [artifact.as_dict()],
            "strategyVersion": "1.2.0",
        }
    )
    return manifest, root


class _Kit:
    def __init__(
        self,
        *,
        workflow: StrategyDevelopmentWorkflow,
        chat: ChatService,
        journal: CommandJournal,
        repository: Path,
        provider: FakeProvider,
        validator: RecordingValidator,
        runner: FakeRunner,
    ) -> None:
        self.workflow = workflow
        self.chat = chat
        self.journal = journal
        self.repository = repository
        self.provider = provider
        self.validator = validator
        self.runner = runner


def _workflow(
    tmp_path: Path,
    *,
    provider: FakeProvider | None = None,
    validator: RecordingValidator | None = None,
    runner: FakeRunner | None = None,
    with_importer: bool = True,
    state: str = "completed",
) -> _Kit:
    repository = _repository(tmp_path)
    clock = _clock()
    id_factory = _id_factory()
    chat = ChatService(clock=clock, id_factory=id_factory)
    selected_provider = provider if provider is not None else FakeProvider(_envelope())
    skills = SkillRegistry(
        (
            ThesisSkill(
                "coding",
                "1.0.0",
                "Inspect tests before changing implementation.",
                ("read_workspace", "write_patch"),
            ),
        )
    )
    profiles = {
        "coding-profile": ModelProfile(
            profile_id="coding-profile",
            version="1.0.0",
            provider="fake",
            model="coding-model",
        )
    }
    policies: dict[Role, RolePolicy] = {
        "coding": RolePolicy(
            role="coding",
            version="1.0.0",
            model_profile="coding-profile",
            skills=("coding",),
            allowed_tools=("read_workspace", "write_patch"),
        )
    }
    router = ModelRouter(
        role_policies=policies,
        model_profiles=profiles,
        providers={"fake": selected_provider},
        skills=skills,
        bundle=PolicyBundle(
            bundle_id="thesis-platform",
            version="1.0.0",
            bundle_sha256="a" * 64,
            policy_sha256="b" * 64,
        ),
        clock=clock,
        id_factory=lambda: id_factory("invocation"),
    )
    selected_validator = validator if validator is not None else RecordingValidator()
    manifest, root = _harness_result(tmp_path / "harness-results")
    selected_runner = (
        runner
        if runner is not None
        else FakeRunner(manifest=manifest, state=state, result_root=str(root))
    )
    importer = ArtifactImporter(repository) if with_importer else None
    journal = CommandJournal(tmp_path / "journal.sqlite3")
    workflow = StrategyDevelopmentWorkflow(
        chat=chat,
        router=router,
        validator=selected_validator,
        runner=selected_runner,
        importer=importer,
        journal=journal,
        clock=clock,
        id_factory=id_factory,
        experiment_id="experiment-1",
    )
    return _Kit(
        workflow=workflow,
        chat=chat,
        journal=journal,
        repository=repository,
        provider=selected_provider,
        validator=selected_validator,
        runner=selected_runner,
    )


def _request(kit: _Kit) -> StrategyDevelopmentRequest:
    chat = kit.chat
    chat_id = chat.create_chat(
        WORKSPACE, title="strategy chat", owner_actor_id=OWNER
    ).chat_id
    return kit.workflow.launch(
        WORKSPACE,
        chat_id,
        actor_id=OWNER,
        objective="Build a momentum strategy and prove it on the harness",
    )


def test_chat_request_executes_documented_path_end_to_end(tmp_path: Path) -> None:
    kit = _workflow(tmp_path)
    request = _request(kit)

    outcome = kit.workflow.run(request)

    assert outcome.phase is StrategyDevelopmentPhase.COMPLETED
    assert outcome.fenced is False
    assert [item.to_phase for item in outcome.transitions] == [
        StrategyDevelopmentPhase.GENERATING,
        StrategyDevelopmentPhase.VALIDATING,
        StrategyDevelopmentPhase.BACKTESTING,
        StrategyDevelopmentPhase.IMPORTING,
        StrategyDevelopmentPhase.RECORDING,
        StrategyDevelopmentPhase.COMPLETED,
    ]
    execution = kit.chat.get_execution(WORKSPACE, request.execution_id)
    assert execution.state is WorkflowExecutionState.COMPLETED
    assert execution.finished_at is not None
    assert execution.tool_job_ids
    assert outcome.bundle is not None
    assert outcome.bundle.strategy_id == "momentum"
    assert outcome.backtest is not None
    assert outcome.backtest.succeeded
    assert outcome.journal_id == f"workflow-{request.execution_id}"
    assert outcome.report is not None
    assert outcome.report.execution_id == request.execution_id


def test_workflow_produces_artifacts_manifests_metrics_and_git_links(
    tmp_path: Path,
) -> None:
    kit = _workflow(tmp_path)
    request = _request(kit)

    outcome = kit.workflow.run(request)

    assert outcome.bundle is not None
    manifest = outcome.bundle.manifest()
    assert manifest["bundleId"] == outcome.bundle.bundle_id
    assert outcome.import_receipt is not None
    assert outcome.import_receipt["strategyVersion"] == "1.2.0"
    assert outcome.import_receipt["experimentId"] == "experiment-1"
    assert outcome.import_receipt["workflowExecutionId"] == request.execution_id
    head = _git(kit.repository, "rev-parse", "HEAD")
    assert outcome.import_receipt["gitCommit"] == head
    references = cast("list[dict[str, object]]", outcome.import_receipt["artifacts"])
    reference = references[0]
    assert (kit.repository / str(reference["uri"])).is_file()
    assert reference["gitCommit"] == head
    report = outcome.report
    assert report is not None
    assert report.metrics == {"sharpe": 1.25, "totalReturn": 0.08}
    assert report.harness_manifest_reference is not None
    assert report.harness_manifest_reference.startswith("manifest-1@sha256:")
    assert report.git_commits
    entry = kit.journal.get(f"workflow-{request.execution_id}")
    assert entry is not None
    assert entry.artifacts[0].uri == f"reports/{request.execution_id}/evidence.json"
    assert entry.artifacts[0].sha256 == report.content_sha256
    publication = PublicationService(
        kit.journal, GitCoordinator(kit.repository)
    ).publish_next()
    assert publication is not None
    assert publication.commit_hash == _git(kit.repository, "rev-parse", "HEAD")
    kit.journal.verify_integrity()


def test_steering_is_non_destructive_and_fences_stale_continuations(
    tmp_path: Path,
) -> None:
    kit = _workflow(tmp_path)
    chat = kit.chat
    chat_id = chat.create_chat(WORKSPACE, title="chat", owner_actor_id=OWNER).chat_id
    request = kit.workflow.launch(
        WORKSPACE, chat_id, actor_id=OWNER, objective="Build a momentum strategy"
    )
    kit.provider.steer = lambda: chat.steer_workflow(
        WORKSPACE, request.execution_id, actor_id=OWNER, body="tighten risk limits"
    )

    outcome = kit.workflow.run(request)

    assert outcome.fenced is True
    assert outcome.phase is StrategyDevelopmentPhase.PAUSED
    assert outcome.detail == "steering advanced the intent epoch"
    assert outcome.report is None
    assert outcome.journal_id is None
    assert kit.runner.requests == []
    execution = chat.get_execution(WORKSPACE, request.execution_id)
    assert execution.state is WorkflowExecutionState.RUNNING
    assert execution.intent_epoch == 1
    assert len(execution.steering_message_ids) == 1

    continuation = replace(request, intent_epoch=execution.intent_epoch)
    resumed = kit.workflow.run(continuation)

    assert resumed.fenced is False
    assert resumed.phase is StrategyDevelopmentPhase.COMPLETED
    assert len(kit.provider.prompts) == 2


def test_in_flight_backtest_result_is_kept_and_stale_continuation_fenced(
    tmp_path: Path,
) -> None:
    kit = _workflow(tmp_path)
    chat = kit.chat
    chat_id = chat.create_chat(WORKSPACE, title="chat", owner_actor_id=OWNER).chat_id
    request = kit.workflow.launch(
        WORKSPACE, chat_id, actor_id=OWNER, objective="Build a momentum strategy"
    )
    kit.runner.on_run = lambda: chat.steer_workflow(
        WORKSPACE, request.execution_id, actor_id=OWNER, body="prefer lower turnover"
    )

    outcome = kit.workflow.run(request)

    assert outcome.fenced is True
    assert outcome.phase is StrategyDevelopmentPhase.PAUSED
    assert outcome.backtest is not None
    assert outcome.journal_id is None
    replay = kit.workflow.observe(WORKSPACE, request.execution_id)
    submitted = [
        event
        for event in replay.events
        if event.event_type is ExecutionEventType.TOOL_SUBMITTED
    ]
    job = chat.get_tool_job(WORKSPACE, str(submitted[0].payload["toolJobId"]))
    assert job.state is ToolJobState.SUCCEEDED
    assert job.late_result is True
    execution = chat.get_execution(WORKSPACE, request.execution_id)
    assert execution.state is WorkflowExecutionState.RUNNING
    assert execution.intent_epoch == 1

    continuation = replace(request, intent_epoch=execution.intent_epoch)
    resumed = kit.workflow.run(continuation)

    assert resumed.fenced is False
    assert resumed.phase is StrategyDevelopmentPhase.COMPLETED
    assert kit.journal.get(f"workflow-{request.execution_id}") is not None


def test_reconnecting_client_observes_progress_and_final_report(
    tmp_path: Path,
) -> None:
    kit = _workflow(tmp_path)
    request = _request(kit)

    outcome = kit.workflow.run(request)

    replay = kit.workflow.observe(WORKSPACE, request.execution_id)
    types = [event.event_type for event in replay.events]
    assert types[0] is ExecutionEventType.MESSAGE_ACCEPTED
    assert types[1] is ExecutionEventType.WORKFLOW_LAUNCHED
    assert ExecutionEventType.WORKFLOW_STARTED in types
    assert types.count(ExecutionEventType.TOOL_SUBMITTED) == 1
    assert types.count(ExecutionEventType.TOOL_STARTED) == 1
    assert types.count(ExecutionEventType.TOOL_RESULT) == 1
    assert types[-1] is ExecutionEventType.WORKFLOW_COMPLETED
    final = replay.events[-1]
    report_reference = cast("dict[str, object]", final.payload["report"])
    assert outcome.report is not None
    assert report_reference["sha256"] == outcome.report.content_sha256
    page = kit.workflow.observe(
        WORKSPACE, request.execution_id, after_cursor=3, limit=2
    )
    assert [event.cursor for event in page.events] == [4, 5]
    assert page.has_more
    tail = kit.workflow.observe(
        WORKSPACE, request.execution_id, after_cursor=page.cursor
    )
    assert tail.events[-1].event_type is ExecutionEventType.WORKFLOW_COMPLETED


def test_phase_1_findings_are_labeled_exploratory(tmp_path: Path) -> None:
    kit = _workflow(tmp_path)
    request = _request(kit)

    outcome = kit.workflow.run(request)

    report = outcome.report
    assert report is not None
    assert report.evidence_grade == "exploratory"
    assert report.historical is True
    assert report.phase_label == "phase-1-historical"
    assert EXPLORATORY_DISCLOSURE in report.limitations
    assert "single-symbol momentum only" in report.limitations
    document = report.as_dict()
    assert document["evidenceGrade"] == "exploratory"
    assert document["historical"] is True
    with pytest.raises(EvidenceReportError):
        replace(report, evidence_grade="validated")


def test_rejected_validation_fails_workflow_before_backtest(tmp_path: Path) -> None:
    kit = _workflow(
        tmp_path,
        validator=RecordingValidator(accepted=False, diagnostics=("forbidden import",)),
    )
    request = _request(kit)

    outcome = kit.workflow.run(request)

    assert outcome.phase is StrategyDevelopmentPhase.FAILED
    assert outcome.fenced is False
    assert outcome.detail is not None
    assert "forbidden import" in outcome.detail
    assert kit.runner.requests == []
    execution = kit.chat.get_execution(WORKSPACE, request.execution_id)
    assert execution.state is WorkflowExecutionState.FAILED
    assert kit.journal.entries() == ()
    replay = kit.workflow.observe(WORKSPACE, request.execution_id)
    assert replay.events[-1].event_type is ExecutionEventType.WORKFLOW_FAILED


def test_failed_backtest_state_fails_workflow_with_recorded_result(
    tmp_path: Path,
) -> None:
    kit = _workflow(tmp_path, state="aborted")
    request = _request(kit)

    outcome = kit.workflow.run(request)

    assert outcome.phase is StrategyDevelopmentPhase.FAILED
    assert outcome.backtest is not None
    assert outcome.import_receipt is None
    assert kit.journal.entries() == ()
    execution = kit.chat.get_execution(WORKSPACE, request.execution_id)
    assert execution.state is WorkflowExecutionState.FAILED


def test_phase_transitions_are_explicit_and_auditable() -> None:
    assert (
        transition_phase(
            StrategyDevelopmentPhase.REQUESTED, StrategyDevelopmentPhase.GENERATING
        )
        is StrategyDevelopmentPhase.GENERATING
    )
    with pytest.raises(InvalidPhaseTransition, match="requested to recording"):
        transition_phase(
            StrategyDevelopmentPhase.REQUESTED, StrategyDevelopmentPhase.RECORDING
        )
    with pytest.raises(InvalidPhaseTransition, match="completed to generating"):
        transition_phase(
            StrategyDevelopmentPhase.COMPLETED, StrategyDevelopmentPhase.GENERATING
        )
    assert STRATEGY_DEVELOPMENT_WORKFLOW.phases == STRATEGY_DEVELOPMENT_PHASES


def test_run_requires_a_live_execution(tmp_path: Path) -> None:
    kit = _workflow(tmp_path, with_importer=False)
    request = StrategyDevelopmentRequest(
        workspace_id=WORKSPACE,
        chat_id="chat-1",
        execution_id="execution-x",
        trigger_message_id="message-x",
        requested_by=OWNER,
        objective="Build a momentum strategy",
    )

    outcome = kit.workflow.run(request)

    assert outcome.phase is StrategyDevelopmentPhase.FAILED
    assert outcome.detail is not None
    assert "does not exist" in outcome.detail


def test_harness_runner_adapter_uses_integrations_client_boundary() -> None:
    capabilities = CapabilityReport.from_statuses(
        (
            CapabilityStatus("backtest", True, "available"),
            CapabilityStatus("result_query", True, "available"),
        )
    )

    class FakeHarnessPort:
        def __init__(self) -> None:
            self.launched: list[BacktestRequest] = []
            self.queries: list[str] = []

        def capabilities(self) -> CapabilityReport:
            return capabilities

        def launch_backtest(self, request: BacktestRequest) -> HarnessBacktestResult:
            self.launched.append(request)
            return HarnessBacktestResult(
                job_id="job-remote-1",
                state="running",
                manifest=None,
                metrics=None,
                result_artifacts=None,
                capability_status=capabilities,
                raw={"resultRoot": "results/root"},
            )

        def query_backtest(self, job_id: str) -> HarnessBacktestResult:
            self.queries.append(job_id)
            return HarnessBacktestResult(
                job_id=job_id,
                state="completed",
                manifest={"manifestId": "m-2", "artifacts": []},
                metrics={"sharpe": 1.1},
                result_artifacts=[],
                capability_status=capabilities,
                raw={},
            )

    port = FakeHarnessPort()
    runner = HarnessBacktestRunner(ThesisHarnessClient(port), mode="local")
    bundle = StrategyBundle.create(
        strategy_id="momentum",
        version="1.0.0",
        source={"strategy.py": "from harness.strategy import Strategy\n"},
        tests={"test_strategy.py": "def test_strategy():\n    assert True\n"},
        harness_configuration={"seed": 3},
    )

    result = runner.run(
        BacktestRunRequest(
            run_id="run-1",
            bundle=bundle,
            workflow_execution_id="execution-1",
            experiment_id="experiment-1",
        )
    )

    assert len(port.launched) == 1
    assert port.queries == ["job-remote-1"]
    launched = port.launched[0]
    assert launched.run_id == "run-1"
    assert launched.metadata["workflowExecutionId"] == "execution-1"
    assert launched.metadata["experimentId"] == "experiment-1"
    strategy_bundle = cast("dict[str, object]", launched.strategy_bundle)
    assert strategy_bundle["strategyId"] == "momentum"
    assert result.succeeded is True
    assert result.state == "completed"
    mode: BacktestMode = result.mode
    assert mode == "local"
    assert result.result_root == "results/root"
