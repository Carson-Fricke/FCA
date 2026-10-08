"""One falsification execution identity across trials, resume, and rebuild."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import cast

from thesis.research.models import ResearchModelRef
from thesis.workflows.backtests import BacktestRunResult
from thesis.workflows.registry.hypothesis_development.artifacts import (
    build_hypothesis,
)
from thesis.workflows.registry.hypothesis_development.contracts.trials import (
    FalsificationTrial,
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

    def __init__(self, *, result_job_id: str | None = None) -> None:
        self.requests: list[ResearchModelRunRequest] = []
        self._result_job_id = result_job_id

    def run(self, request: ResearchModelRunRequest) -> ResearchModelRunResult:
        self.requests.append(request)
        job_id = self._result_job_id or request.job_id
        return ResearchModelRunResult(
            model_reference=ResearchModelRef(
                workspace_id=WORKSPACE,
                model_id="momentum-model",
                content_sha256="a" * 64,
            ),
            backtest=BacktestRunResult(
                job_id=job_id,
                state="succeeded",
                metrics={"sharpe": 1.0, "sample_valid": 1.0},
            ),
            workspace_artifact="workspace-model:momentum-model",
            runtime_configuration={},
            fitted_reference=None,
        )


def _resolved_state(
    kit,
    request,
    *,
    falsifiers: list[dict[str, object]] | None = None,
    trials: tuple[FalsificationTrial, ...] = (),
) -> HypothesisDevelopmentState:
    draft = _draft_from_contract(_hypothesis_envelope(falsifiers))
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
        trials=trials,
    )


def test_first_execution_allocates_one_job_id_for_tool_job_and_run(
    tmp_path: Path,
) -> None:
    runner = _RecordingStudyRunner()
    kit = _kit(tmp_path, payloads=[], results=[], research_runner=runner)
    request = _request(kit)
    state = _resolved_state(kit, request)

    final = run_trials(cast(DevelopmentEngine, kit.workflow), request, state)

    assert len(runner.requests) == 1
    submitted = runner.requests[0].job_id
    assert submitted.startswith("job-")
    trial = final.trials[0]
    assert trial.job_id == submitted
    tool_job = kit.chat.get_tool_job(WORKSPACE, submitted)
    assert tool_job.job_id == submitted


def test_resumed_pending_trial_reuses_the_persisted_job_id(tmp_path: Path) -> None:
    runner = _RecordingStudyRunner()
    kit = _kit(tmp_path, payloads=[], results=[], research_runner=runner)
    request = _request(kit)
    persisted = FalsificationTrial(
        test_id="T01",
        falsifier_id="f1",
        job_id="job-persisted",
        digest="a" * 64,
        run_state="succeeded",
        observed=None,
        falsified=False,
    )
    state = _resolved_state(kit, request, trials=(persisted,))

    final = run_trials(cast(DevelopmentEngine, kit.workflow), request, state)

    assert len(runner.requests) == 1
    assert runner.requests[0].job_id == "job-persisted"
    assert final.trials[0].job_id == "job-persisted"


def test_multiple_falsifiers_share_one_execution_job(tmp_path: Path) -> None:
    runner = _RecordingStudyRunner()
    kit = _kit(tmp_path, payloads=[], results=[], research_runner=runner)
    request = _request(kit)
    falsifiers: list[dict[str, object]] = [
        {
            "falsifierId": "f1",
            "observable": "sharpe",
            "condition": "lt",
            "threshold": 0.0,
            "test": "Require a negative Sharpe.",
        },
        {
            "falsifierId": "f2",
            "observable": "sample_valid",
            "condition": "lt",
            "threshold": 1.0,
            "test": "Require a valid sample.",
        },
    ]
    state = _resolved_state(kit, request, falsifiers=falsifiers)

    final = run_trials(cast(DevelopmentEngine, kit.workflow), request, state)

    assert len(runner.requests) == 1
    submitted = runner.requests[0].job_id
    assert {trial.test_id for trial in final.trials} == {"T01", "T02"}
    assert {trial.falsifier_id for trial in final.trials} == {"f1", "f2"}
    assert {trial.job_id for trial in final.trials} == {submitted}


def test_trial_round_trips_the_job_identity() -> None:
    trial = FalsificationTrial(
        test_id="T01",
        falsifier_id="f1",
        job_id="job-1",
        digest="a" * 64,
        run_state="succeeded",
        observed=1.0,
        falsified=False,
    )
    payload = trial.as_dict()
    assert payload["jobId"] == "job-1"
    assert "runId" not in payload
    assert FalsificationTrial.from_dict(payload) == trial
