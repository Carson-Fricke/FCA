"""Fitted-parameter extraction at the public research-model runner boundary."""

from __future__ import annotations

import pytest

from thesis.research.models import ResearchModel
from thesis.workflows.backtests import BacktestRunRequest, BacktestRunResult
from thesis.workflows.research_models import (
    BacktestResearchModelRunner,
    ResearchModelRunRequest,
)

_SOURCE = """use harness_strategy_sdk::{export_study_program, MarketConsumer, MetricPublisher,
    ObservationContext, QuoteEvent, StudyProgram, Subscription};
use harness_strategy_sdk::serde_json::Value;
#[derive(Default)]
struct Model;
impl MarketConsumer for Model {
    type Context = ObservationContext;
    fn subscriptions(&self) -> Vec<Subscription> {
        vec![Subscription::Quote { symbols: vec!["SPY".into()] }]
    }
    fn on_quote(&mut self, _: &QuoteEvent, _: &mut ObservationContext) {}
}
impl StudyProgram for Model {
    fn new(_: Value) -> Self { Self }
    fn on_stop(&mut self, _: &mut MetricPublisher) {}
}
export_study_program!(Model);
"""
_WINDOW = {"start": "2026-09-21T13:30:00Z", "end": "2026-09-28T13:30:00Z"}


def _model() -> ResearchModel:
    return ResearchModel(
        model_id="fit-study",
        version="a" * 64,
        description="Fit extraction",
        source=_SOURCE,
        outputs=("data_ok",),
        harness_configuration={
            **_WINDOW,
            "subscriptions": [{"data_type": "quote", "symbols": ["SPY"]}],
        },
        tests={"tests/model.rs": "#[test] fn publication() {}"},
    )


class _Backtests:
    def __init__(self, metrics: object) -> None:
        self._metrics = metrics

    def run(self, request: BacktestRunRequest) -> BacktestRunResult:
        return BacktestRunResult("job-fit", "succeeded", metrics=self._metrics)


def _request() -> ResearchModelRunRequest:
    return ResearchModelRunRequest(
        "job-fit", "workspace-fit", _model(), "workflow-fit", "experiment-fit"
    )


def test_successful_run_extracts_fit_parameters() -> None:
    result = BacktestResearchModelRunner(
        _Backtests({"custom": {"fit.baseline": "0.5", "sharpe": 1.0}})
    ).run(_request())

    assert result.fitted_parameters == {"baseline": 0.5}
    assert result.backtest.succeeded


def test_missing_fit_metrics_name_the_published_metrics() -> None:
    with pytest.raises(ValueError) as error:
        BacktestResearchModelRunner(
            _Backtests({"custom": {"data_ok": "1", "sharpe": "1.0"}})
        ).run(_request())

    message = str(error.value)
    assert "published no fit parameters" in message
    assert "data_ok" in message
    assert "sharpe" in message
