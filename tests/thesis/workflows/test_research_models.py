from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import cast

from thesis.research.indicators import (
    IndicatorScope,
    InMemoryIndicatorStore,
    WorkspaceIndicator,
)
from thesis.research.models import (
    IndicatorBinding,
    InMemoryResearchModelStore,
    ResearchModel,
)
from thesis.workflows.backtests import (
    BacktestRunRequest,
    BacktestRunResult,
)
from thesis.workflows.research_models import (
    BacktestResearchModelRunner,
    ResearchModelRunRequest,
)


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


def test_runner_publishes_workspace_sources_and_uses_platform_adapter() -> None:
    indicator = WorkspaceIndicator(
        name="rolling_corr",
        version="1.0.0",
        description="Pair correlation.",
        source="def calculate(context, parameters):\n    return 0.73\n",
        tests={"test_indicator.py": "def test_indicator():\n    assert True\n"},
    )
    model = ResearchModel(
        model_id="pair-model",
        version="1.0.0",
        description="Pair regularity model.",
        source=(
            "def fit(context):\n"
            "    return {'baseline': 0.5}\n\n"
            "def observe(fit, context):\n"
            "    return {'rolling_corr_60d_median': "
            "context['indicators']['pair_corr']}\n"
        ),
        fit_entrypoint="fit",
        observe_entrypoint="observe",
        outputs=("rolling_corr_60d_median",),
        harness_configuration={"symbols": ["MSTR", "IBIT"], "seed": 7},
        indicators=(
            IndicatorBinding(
                alias="pair_corr",
                scope=IndicatorScope.WORKSPACE,
                name="rolling_corr",
                symbol="MSTR",
                definition=indicator,
            ),
        ),
        tests={"test_model.py": "def test_model():\n    assert True\n"},
    )
    backtests = _Backtests()
    models = InMemoryResearchModelStore()
    indicators = InMemoryIndicatorStore()
    runner = BacktestResearchModelRunner(
        backtests,
        models=models,
        indicators=indicators,
    )

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
    assert result.indicator_references[0].workspace_id == "workspace-one"
    assert result.fitted_reference is not None
    assert result.fitted_reference.key.startswith("workspace-fit:workspace-one/")
    assert result.workspace_artifact.startswith("models/runs/")
    evidence = models.run_artifact("workspace-one", result.workspace_artifact)
    assert b'"rolling_corr_60d_median":"0.73"' in evidence
    request = backtests.requests[0]
    assert request.workspace_id == "workspace-one"
    config = cast("Mapping[str, object]", request.bundle.harness_configuration)
    instances = cast("Sequence[Mapping[str, object]]", config["strategy_instances"])
    assert instances[0]["implementation"] == (
        "harness.research.adapter:ResearchModelAdapter"
    )
    parameters = cast("Mapping[str, object]", instances[0]["parameters"])
    assert parameters["model_source"] == model.source
    bindings = cast("Sequence[Mapping[str, object]]", parameters["indicators"])
    assert bindings[0]["source"] == indicator.source
    dependencies = cast("Sequence[str]", request.bundle.dependencies)
    assert result.model_reference.key in dependencies


def test_runner_falls_back_to_request_universe_when_configuration_omits_symbols() -> (
    None
):
    model = ResearchModel(
        model_id="relative-value",
        version="1.0.0",
        description="Relative value model.",
        source=(
            "def fit(context):\n"
            "    return {'baseline': 0.5}\n\n"
            "def observe(fit, context):\n"
            "    return {'observed_metric': 1.0}\n"
        ),
        fit_entrypoint="fit",
        observe_entrypoint="observe",
        outputs=("observed_metric",),
        harness_configuration={"requiredSymbols": ["MSTR", "IBIT"], "seed": 7},
        indicators=(),
        tests={"test_model.py": "def test_model():\n    assert True\n"},
    )
    backtests = _Backtests()
    runner = BacktestResearchModelRunner(
        backtests,
        models=InMemoryResearchModelStore(),
        indicators=InMemoryIndicatorStore(),
    )

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
    instances = cast("Sequence[Mapping[str, object]]", config["strategy_instances"])
    assert list(cast("Sequence[object]", instances[0]["symbols"])) == ["MSTR", "IBIT"]
