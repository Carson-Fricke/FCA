"""Falsification resume reuses the tracked submission for the same revision."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import cast

from thesis.chat.contracts import (
    HARNESS_RESEARCH_MODEL_TOOL,
    ToolJob,
    ToolJobState,
)
from thesis.research.models import ResearchModel, ResearchModelRef
from thesis.workflows.backtests import BacktestRunResult
from thesis.workflows.registry.hypothesis_development.artifacts import (
    build_hypothesis,
)
from thesis.workflows.registry.hypothesis_development.contracts.trials import (
    FalsificationTrial,
)
from thesis.workflows.registry.hypothesis_development.jobs import (
    research_model_attribution,
)
from thesis.workflows.registry.hypothesis_development.phases import (
    HypothesisDevelopmentPhase,
)
from thesis.workflows.registry.hypothesis_development.phases.trials import (
    _draft_from_contract,
    _plan_from_document,
    run_trials,
)
from thesis.workflows.registry.hypothesis_development.services import (
    DevelopmentEngine,
)
from thesis.workflows.registry.hypothesis_development.state import (
    HypothesisDevelopmentState,
)
from thesis.workflows.research_models import (
    ResearchModelRunRequest,
    ResearchModelRunResult,
    StudyProgramReference,
    resolve_study_reference,
)

from ._support import (
    WORKSPACE,
    FakePrograms,
    _hypothesis_envelope,
    _kit,
    _request,
)


class _RecordingStudyRunner:
    """A research-model port that records the submitted job id."""

    def __init__(self) -> None:
        self.requests: list[ResearchModelRunRequest] = []

    def run(self, request: ResearchModelRunRequest) -> ResearchModelRunResult:
        self.requests.append(request)
        return ResearchModelRunResult(
            model_reference=ResearchModelRef(
                workspace_id=WORKSPACE,
                model_id="momentum-model",
                content_sha256="a" * 64,
            ),
            backtest=BacktestRunResult(
                job_id=request.job_id,
                state="succeeded",
                metrics={"sharpe": 1.0, "sample_valid": 1.0},
            ),
            workspace_artifact="workspace-model:momentum-model",
            runtime_configuration={},
            fitted_reference=None,
        )


def _resolved_state(kit, request) -> HypothesisDevelopmentState:
    draft = _draft_from_contract(_hypothesis_envelope())
    reference = draft.model
    assert isinstance(reference, StudyProgramReference)
    draft = replace(
        draft,
        model=resolve_study_reference(FakePrograms(), WORKSPACE, reference),
    )
    document = build_hypothesis(kit.workflow, request, draft)
    return HypothesisDevelopmentState(
        workspace_id=WORKSPACE,
        execution_id=request.execution_id,
        phase=HypothesisDevelopmentPhase.FALSIFICATION_TRIALS,
        intent_epoch=0,
        draft=draft,
        hypothesis=document,
        plan=_plan_from_document(document),
        trials=(),
    )


def _model_attribution(state: HypothesisDevelopmentState) -> str:
    assert state.draft is not None
    model = state.draft.model
    assert isinstance(model, ResearchModel)
    model_sha256 = research_model_attribution(model)["modelSha256"]
    assert isinstance(model_sha256, str)
    return model_sha256


def _track(
    kit,
    request,
    state: HypothesisDevelopmentState,
    *,
    job_id: str,
    model_sha256: str,
) -> None:
    kit.chat.submit_tool_job(
        WORKSPACE,
        request.execution_id,
        tool_name=HARNESS_RESEARCH_MODEL_TOOL,
        actor_id="thesis-workflow",
        job_id=job_id,
        attribution={"modelSha256": model_sha256},
    )
    kit.chat.start_tool_job(WORKSPACE, job_id, actor_id="thesis-workflow")


def test_interrupted_submission_is_rejoined_after_acceptance(tmp_path: Path) -> None:
    runner = _RecordingStudyRunner()
    kit = _kit(tmp_path, payloads=[], results=[], research_runner=runner)
    request = _request(kit)
    state = _resolved_state(kit, request)
    _track(
        kit,
        request,
        state,
        job_id="job-interrupted",
        model_sha256=_model_attribution(state),
    )

    final = run_trials(cast(DevelopmentEngine, kit.workflow), request, state)

    assert len(runner.requests) == 1
    assert runner.requests[0].job_id == "job-interrupted"
    assert final.trials[0].job_id == "job-interrupted"
    assert final.trials[0].observed == 1.0
    assert [job.job_id for job in kit.chat.persistence.list(ToolJob, WORKSPACE)] == [
        "job-interrupted"
    ]
    assert kit.chat.get_execution(WORKSPACE, request.execution_id).job_ids == (
        "job-interrupted",
    )


def test_stale_submission_from_another_revision_is_not_reused(
    tmp_path: Path,
) -> None:
    runner = _RecordingStudyRunner()
    kit = _kit(tmp_path, payloads=[], results=[], research_runner=runner)
    request = _request(kit)
    state = _resolved_state(kit, request)
    _track(kit, request, state, job_id="job-stale", model_sha256="f" * 64)

    final = run_trials(cast(DevelopmentEngine, kit.workflow), request, state)

    submitted = runner.requests[0].job_id
    assert submitted != "job-stale"
    assert submitted.startswith("job-")
    assert final.trials[0].job_id == submitted
    stale = kit.chat.get_tool_job(WORKSPACE, "job-stale")
    assert stale.state is ToolJobState.RUNNING
    assert stale.attribution == {"modelSha256": "f" * 64}
    assert set(kit.chat.get_execution(WORKSPACE, request.execution_id).job_ids) == {
        "job-stale",
        submitted,
    }


def test_persisted_trial_job_id_wins_over_recovery(tmp_path: Path) -> None:
    runner = _RecordingStudyRunner()
    kit = _kit(tmp_path, payloads=[], results=[], research_runner=runner)
    request = _request(kit)
    state = _resolved_state(kit, request)
    persisted = FalsificationTrial(
        test_id="T01",
        falsifier_id="f1",
        job_id="job-persisted",
        digest="a" * 64,
        run_state="failed",
        observed=None,
        falsified=False,
    )
    _track(
        kit,
        request,
        state,
        job_id="job-recoverable",
        model_sha256=_model_attribution(state),
    )

    final = run_trials(
        cast(DevelopmentEngine, kit.workflow),
        request,
        replace(state, trials=(persisted,)),
    )

    assert runner.requests[0].job_id == "job-persisted"
    assert final.trials[0].job_id == "job-persisted"
