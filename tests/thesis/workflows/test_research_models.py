from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

import pytest

from thesis.research.models import (
    InMemoryResearchModelStore,
    ResearchModel,
    ResearchModelError,
    WorkspaceResearchModelStore,
)
from thesis.workflows.backtests import (
    BacktestRunRequest,
    BacktestRunResult,
)
from thesis.workflows.research_models import (
    BacktestResearchModelRunner,
    ResearchModelRunRequest,
    study_window,
)

_STUDY_SOURCE = """use harness_strategy_sdk::{
    export_study_program, BarEvent, MetricPublisher, StudyProgram,
};
use harness_strategy_sdk::serde_json::Value;

struct PairModel;

impl StudyProgram for PairModel {
    fn new(_parameters: Value) -> Self {
        Self
    }

    fn on_bar(&mut self, _event: &BarEvent) {}

    fn on_stop(&mut self, metrics: &mut MetricPublisher) {
        let _ = metrics.publish("rolling_corr_60d_median", 0.73);
        let _ = metrics.publish("fit.baseline", 0.5);
    }
}

export_study_program!(PairModel);
"""


def _model(**overrides: object) -> ResearchModel:
    fields: dict[str, object] = {
        "model_id": "pair-model",
        "version": "1.0.0",
        "description": "Pair regularity model.",
        "source": _STUDY_SOURCE,
        "outputs": ("rolling_corr_60d_median",),
        "harness_configuration": {
            "symbols": ["MSTR", "IBIT"],
            "seed": 7,
            "start": "2024-01-01T00:00:00Z",
            "end": "2024-03-01T00:00:00Z",
            "interval": "1min",
        },
        "tests": {"test_model.txt": "Covered by the harness jit suite.\n"},
    }
    fields.update(overrides)
    return ResearchModel(**fields)


class _Backtests:
    def __init__(self) -> None:
        self.requests: list[BacktestRunRequest] = []

    def run(self, request: BacktestRunRequest) -> BacktestRunResult:
        self.requests.append(request)
        return BacktestRunResult(
            job_id=request.run_id,
            state="completed",
            metrics={
                "custom": {
                    "fit.baseline": "0.5",
                    "rolling_corr_60d_median": "0.73",
                }
            },
        )


class _Paths:
    """Workspace path provider rooted at a temporary directory."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def resolve_path(
        self,
        workspace_id: str,
        relative: str | Path = ".",
        *,
        must_exist: bool = False,
    ) -> Path:
        boundary = (self.root / workspace_id).resolve(strict=False)
        boundary.mkdir(parents=True, exist_ok=True)
        path = (boundary / relative).resolve(strict=must_exist)
        if not path.is_relative_to(boundary):
            raise ValueError("escaped")
        return path


def test_runner_publishes_the_authored_rust_program_as_a_jit_study() -> None:
    model = _model()
    backtests = _Backtests()
    models = InMemoryResearchModelStore()
    runner = BacktestResearchModelRunner(backtests, models=models)

    result = runner.run(
        ResearchModelRunRequest(
            run_id="run-pair-model",
            workspace_id="workspace-one",
            model=model,
            workflow_execution_id="execution-one",
            experiment_id="experiment-one",
        )
    )

    assert result.backtest.metrics == {
        "custom": {
            "fit.baseline": "0.5",
            "rolling_corr_60d_median": "0.73",
        }
    }
    assert result.model_reference.workspace_id == "workspace-one"
    assert result.fitted_reference is not None
    assert result.fitted_reference.key.startswith("workspace-fit:workspace-one/")
    assert result.workspace_artifact.startswith("models/runs/")
    evidence = models.run_artifact("workspace-one", result.workspace_artifact)
    assert b'"rolling_corr_60d_median":"0.73"' in evidence
    request = backtests.requests[0]
    assert request.workspace_id == "workspace-one"
    config = cast("Mapping[str, object]", request.bundle.harness_configuration)
    # The model's declared window and bar interval are piped through to the
    # study's data block verbatim, so the study replays exactly the bars the
    # hypothesis asked for.
    assert config["start"] == "2024-01-01T00:00:00Z"
    assert config["end"] == "2024-03-01T00:00:00Z"
    data = cast("Mapping[str, object]", config["data"])
    assert data["bar_interval"] == "1min"
    assert "interval" not in config
    instances = cast("Sequence[Mapping[str, object]]", config["strategy_instances"])
    assert instances[0]["implementation"] == "jit"
    assert (
        instances[0]["bundle_hash"]
        == hashlib.sha256(model.source.encode("utf-8")).hexdigest()
    )
    parameters = cast("Mapping[str, object]", instances[0]["parameters"])
    assert parameters["source"] == model.source
    dependencies = cast("Sequence[str]", request.bundle.dependencies)
    assert result.model_reference.key in dependencies


def test_runner_rejects_a_model_source_that_is_not_sdk_authored() -> None:
    with pytest.raises(ResearchModelError, match="harness-strategy-sdk"):
        _model(source="def fit(context):\n    return {}\n")

    with pytest.raises(ResearchModelError, match="export_study_program!"):
        _model(source="use harness_strategy_sdk::StudyProgram;\nfn main() {}\n")


def test_runner_persists_artifacts_through_workspace_stores(tmp_path: Path) -> None:
    paths = _Paths(tmp_path)
    model = _model()
    models = WorkspaceResearchModelStore(paths)
    runner = BacktestResearchModelRunner(_Backtests(), models=models)

    result = runner.run(
        ResearchModelRunRequest(
            run_id="run-pair-model",
            workspace_id="workspace-one",
            model=model,
            workflow_execution_id="execution-one",
            experiment_id="experiment-one",
        )
    )

    workspace = tmp_path / "workspace-one"
    model_directory = workspace / result.model_reference.relative_path
    assert (model_directory / "definition.json").is_file()
    assert (model_directory / "program.rs").read_text(encoding="utf-8") == model.source
    assert result.fitted_reference is not None
    assert (workspace / result.fitted_reference.relative_path).is_file()
    assert result.workspace_artifact.startswith("models/runs/")
    assert (workspace / result.workspace_artifact).is_file()
    assert models.load("workspace-one", result.model_reference) == model


def test_runner_falls_back_to_request_universe_when_configuration_omits_symbols() -> (
    None
):
    model = _model(
        model_id="relative-value",
        description="Relative value model.",
        outputs=("observed_metric",),
        harness_configuration={
            "requiredSymbols": ["MSTR", "IBIT"],
            "seed": 7,
            "start": "2024-01-01T00:00:00Z",
            "end": "2024-03-01T00:00:00Z",
            "interval": "1min",
        },
    )
    backtests = _Backtests()
    runner = BacktestResearchModelRunner(backtests, models=InMemoryResearchModelStore())

    runner.run(
        ResearchModelRunRequest(
            run_id="run-rv-model",
            workspace_id="workspace-rv",
            model=model,
            workflow_execution_id="execution-rv",
            experiment_id="experiment-rv",
            universe=("MSTR", "IBIT", "BTC/USD"),
        )
    )

    request = backtests.requests[0]
    config = cast("Mapping[str, object]", request.bundle.harness_configuration)
    data = cast("Mapping[str, object]", config["data"])
    assert list(cast("Sequence[object]", data["symbols"])) == ["MSTR", "IBIT"]
    assert data["bar_interval"] == "1min"
    assert "interval" not in config
    instances = cast("Sequence[Mapping[str, object]]", config["strategy_instances"])
    assert list(cast("Sequence[object]", instances[0]["symbols"])) == ["MSTR", "IBIT"]


def test_runner_emits_the_models_bar_interval_into_the_study_data() -> None:
    model = _model(
        harness_configuration={
            "symbols": ["SPY"],
            "start": "2024-01-01T00:00:00Z",
            "end": "2024-03-01T00:00:00Z",
            "interval": "1d",
        }
    )
    backtests = _Backtests()
    runner = BacktestResearchModelRunner(backtests, models=InMemoryResearchModelStore())

    runner.run(
        ResearchModelRunRequest(
            run_id="run-daily-model",
            workspace_id="workspace-daily",
            model=model,
            workflow_execution_id="execution-daily",
            experiment_id="experiment-daily",
        )
    )

    bundle = backtests.requests[0].bundle
    config = cast("Mapping[str, object]", bundle.harness_configuration)
    data = cast("Mapping[str, object]", config["data"])
    assert data["bar_interval"] == "1d"
    # The interval is emitted exactly once, as the harness reads it.
    assert "interval" not in config


def test_model_requires_a_declared_bar_interval() -> None:
    # A model without an interval cannot be replayed as a bar study, so it is
    # invalid rather than silently defaulted.
    with pytest.raises(ResearchModelError, match="bar interval"):
        _model(
            harness_configuration={
                "symbols": ["SPY"],
                "start": "2024-01-01T00:00:00Z",
                "end": "2024-03-01T00:00:00Z",
            }
        )

    with pytest.raises(ResearchModelError, match="bar interval"):
        _model(
            harness_configuration={
                "symbols": ["SPY"],
                "start": "2024-01-01T00:00:00Z",
                "end": "2024-03-01T00:00:00Z",
                "interval": "5min",
            }
        )


def test_study_window_requires_the_model_to_declare_both_bounds() -> None:
    # The window is the model's to declare. Substituting a default is what let
    # a 42-session model fetch 2.75 years of history it never read, so a
    # missing bound is a contract violation rather than a fallback.
    with pytest.raises(ResearchModelError, match="must declare its sample window"):
        study_window({})

    with pytest.raises(ResearchModelError, match="missing start"):
        study_window({"end": "2026-09-17T00:00:00Z"})

    with pytest.raises(ResearchModelError, match="missing end"):
        study_window({"start": "2026-08-01T00:00:00Z"})


def test_study_window_normalizes_declared_dates_to_exclusive_midnights() -> None:
    start, end = study_window({"start_date": "2024-01-11", "end_date": "2024-03-01"})

    assert start == "2024-01-11T00:00:00Z"
    assert end == "2024-03-02T00:00:00Z"


def test_study_window_honours_canonical_rfc3339_bounds() -> None:
    start, end = study_window(
        {"start": "2023-05-01T00:00:00Z", "end": "2024-06-01T00:00:00Z"}
    )

    assert start == "2023-05-01T00:00:00Z"
    assert end == "2024-06-01T00:00:00Z"


def test_study_window_rejects_a_sentinel_instead_of_piping_it_to_the_fetch() -> None:
    # A sentinel is not a date. An earlier version normalized only the shape it
    # recognized and fell through for anything else, which is how a placeholder
    # could reach the harness as a literal bound; undeclared now fails loudly.
    with pytest.raises(ResearchModelError, match="missing start"):
        study_window(
            {"dataStart": "latest_complete_historical_session", "start": None},
        )

    with pytest.raises(ResearchModelError, match="missing end"):
        study_window(
            {
                "start": "2026-08-01T00:00:00Z",
                "end_date": "latest_complete_historical_session",
            },
        )


def test_study_window_pipes_a_declared_month_through_unchanged() -> None:
    # The regression: an operator asking about the last month must produce a
    # month of bounds, never a default period orders of magnitude larger.
    start, end = study_window(
        {"start": "2026-08-17T00:00:00Z", "end": "2026-09-17T00:00:00Z"}
    )

    assert start == "2026-08-17T00:00:00Z"
    assert end == "2026-09-17T00:00:00Z"
