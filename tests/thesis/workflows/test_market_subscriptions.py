"""Exact market subscriptions survive model publication and study compilation."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from thesis.research.models import (
    ResearchModel,
    ResearchModelError,
    WorkspaceResearchModelStore,
    validate_market_subscriptions,
)
from thesis.strategies.bundles import StrategyBundleError
from thesis.strategies.contracts import StrategyDesign
from thesis.workflows.backtests import BacktestRunRequest, BacktestRunResult
from thesis.workflows.registry.hypothesis_development.composers.hypothesis import (
    _compose_research_model,
    _compose_universe,
    decode_persisted_model,
)
from thesis.workflows.registry.hypothesis_development.contracts.validation import (
    hypothesis_payload_errors,
)
from thesis.workflows.registry.hypothesis_development.errors import (
    FalsificationComposeError,
)
from thesis.workflows.registry.hypothesis_development.prompts.model_creation import (
    _model_creation_instructions,
)
from thesis.workflows.registry.hypothesis_development.prompts.strategy_design import (
    _strategy_design_instructions,
)
from thesis.workflows.research_models import (
    BacktestResearchModelRunner,
    ResearchModelRunRequest,
    StudyProgramReference,
    resolve_study_reference,
    study_configuration,
)

_SOURCE = """
use harness_strategy_sdk::{export_study_program, BarFrame, MarketConsumer,
    ObservationContext, QuoteEvent, Subscription, StudyProgram, MetricPublisher};
use harness_strategy_sdk::serde_json::Value;
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
    fn on_stop(&mut self, metrics: &mut MetricPublisher) {
        metrics.publish("data_ok", 1.0);
        metrics.publish("fit.baseline", 0.5);
    }
}
export_study_program!(Model);
""".strip()
_WINDOW = {"start": "2026-09-21T13:30:00Z", "end": "2026-09-28T13:30:00Z"}


def _model(subscriptions: object) -> ResearchModel:
    return ResearchModel(
        model_id="market-study",
        version="1.0.0",
        description="Exact market inputs",
        source=_SOURCE,
        outputs=("data_ok",),
        harness_configuration={**_WINDOW, "subscriptions": subscriptions},
        tests={"tests/model.rs": "#[test] fn publication() {}"},
    )


class _WorkspacePaths:
    def __init__(self, root: Path) -> None:
        self.root = root

    def resolve_path(
        self, workspace_id: str, relative: str, *, must_exist: bool = False
    ) -> Path:
        path = self.root / workspace_id / relative
        if must_exist and not path.exists():
            raise FileNotFoundError(path)
        return path


class _Backtests:
    request: BacktestRunRequest | None = None

    def run(self, request: BacktestRunRequest) -> BacktestRunResult:
        self.request = request
        return BacktestRunResult(
            "job-market", "succeeded", metrics={"custom": {"fit.baseline": 0.5}}
        )


@pytest.mark.parametrize(
    "subscriptions",
    [
        [{"data_type": "quote", "symbols": ["SPY"]}],
        [
            {"data_type": "quote", "symbols": ["QQQ", "SPY", "QQQ"]},
            {"data_type": "quote", "symbols": ["SPY"]},
        ],
        [
            {"data_type": "quote", "symbols": ["SPY"]},
            {"data_type": "bars", "symbols": ["QQQ"], "interval": "1min"},
            {"data_type": "trade", "symbols": ["SPY"]},
            {"data_type": "bars", "symbols": ["SPY"], "interval": "1min"},
        ],
    ],
)
def test_stored_model_and_runner_preserve_exact_subscriptions(
    tmp_path: Path, subscriptions: list[dict[str, object]]
) -> None:
    model = _model(subscriptions)
    store = WorkspaceResearchModelStore(_WorkspacePaths(tmp_path))
    reference = store.publish("workspace-market", model)
    loaded = store.load("workspace-market", reference)
    assert loaded.canonical_document == model.canonical_document
    assert loaded.harness_configuration["subscriptions"] == subscriptions
    backtests = _Backtests()
    result = BacktestResearchModelRunner(backtests, models=store).run(
        ResearchModelRunRequest(
            "market-run",
            "workspace-market",
            loaded,
            "workflow-market",
            "experiment-market",
            universe=("SPY", "QQQ", "UNREQUESTED"),
        )
    )
    assert result.backtest.succeeded
    assert backtests.request is not None
    configuration = backtests.request.bundle.canonical_document["harnessConfiguration"]
    assert isinstance(configuration, dict)
    assert configuration["data"]["subscriptions"] == subscriptions
    assert "bar_interval" not in configuration["data"]
    assert "interval" not in configuration
    assert "subscriptions" not in configuration
    assert configuration["start"] == _WINDOW["start"]
    assert configuration["end"] == _WINDOW["end"]
    assert "UNREQUESTED" not in configuration["data"]["symbols"]
    assert configuration["strategy_instances"][0]["parameters"]["source"] == _SOURCE


@pytest.mark.parametrize(
    "subscriptions",
    [
        None,
        [],
        {},
        (),
        [None],
        [{"data_type": "quotes", "symbols": ["SPY"]}],
        [{"data_type": [], "symbols": ["SPY"]}],
        [{"data_type": "quote", "symbols": []}],
        [{"data_type": "quote", "symbols": "SPY"}],
        [{"data_type": "quote", "symbols": [True]}],
        [{"data_type": "quote", "symbols": [""]}],
        [{"data_type": "quote", "symbols": [" SPY"]}],
        [{"data_type": "quote", "symbols": ["SPY "]}],
        [{"data_type": "quote", "symbols": ["spy"]}],
        [{"data_type": "quote", "symbols": ["BTC usd"]}],
        [{"data_type": "quote", "symbols": ["BTC+USD"]}],
        [{"data_type": "quote", "symbols": ["/BTC"]}],
        [{"data_type": "trade", "symbols": ["SPY"], "interval": None}],
        [{"data_type": "quote", "symbols": ["SPY"], "extra": True}],
        [{"data_type": "bars", "symbols": ["SPY"]}],
        [{"data_type": "bars", "symbols": ["SPY"], "interval": "1m"}],
        [{"data_type": "bars", "symbols": ["SPY"], "interval": " 1min"}],
        [{"data_type": "bars", "symbols": ["SPY"], "interval": True}],
        [{"data_type": "bars", "symbols": ["SPY"], "interval": []}],
        [
            {"data_type": "bars", "symbols": ["SPY"], "interval": "1min"},
            {"data_type": "bars", "symbols": ["QQQ"], "interval": "1d"},
        ],
    ],
)
def test_subscription_shape_is_strict(subscriptions: object) -> None:
    with pytest.raises(ResearchModelError):
        validate_market_subscriptions(subscriptions)
    with pytest.raises(ResearchModelError):
        _model(subscriptions)


def _payload(model: ResearchModel) -> dict[str, object]:
    return {
        "claim": "Quotes show a regularity.",
        "disagreement": "The null has no regularity.",
        "universe": ["SPY"],
        "falsifiers": [
            {
                "observable": "data_ok",
                "condition": "lt",
                "threshold": 1.0,
                "test": "Count quotes.",
            }
        ],
        "model": {
            "workspaceId": "workspace-market",
            "kind": "study",
            "programId": model.model_id,
            "gitCommit": "a" * 40,
            "version": "b" * 64,
            "description": model.description,
            "outputs": list(model.outputs),
            "tests": dict(model.tests),
        },
    }


def _resolve(
    model: ResearchModel,
    reference: StudyProgramReference | None = None,
    *,
    configuration: Mapping[str, object] | None = None,
) -> ResearchModel:
    class Snapshots:
        def load(self, workspace_id, kind, program_id, commit=None):
            return {
                "workspaceId": workspace_id,
                "kind": kind,
                "programId": program_id,
                "gitCommit": commit,
                "version": "b" * 64,
                "source": model.source,
                "configuration": dict(
                    model.harness_configuration
                    if configuration is None
                    else configuration
                ),
            }

    return resolve_study_reference(
        Snapshots(),
        "workspace-market",
        reference or _compose_research_model(_payload(model)["model"]),
    )


def test_tick_only_contract_and_separate_impl_publications() -> None:
    model = _model([{"data_type": "quote", "symbols": ["SPY"]}])
    assert model.bar_interval is None
    payload = _payload(model)
    assert hypothesis_payload_errors(payload) == []
    reference = _compose_research_model(payload["model"])
    composed = _resolve(model, reference)
    assert composed.source == model.source
    assert composed.harness_configuration == model.harness_configuration
    assert composed.outputs == model.outputs
    persisted = decode_persisted_model(model.canonical_document)
    assert isinstance(persisted, ResearchModel)
    assert persisted.canonical_document == model.canonical_document
    assert _compose_universe(None, composed) == ("SPY",)


def test_subscription_symbols_must_belong_to_universe() -> None:
    model = _model([{"data_type": "quote", "symbols": ["QQQ"]}])
    assert hypothesis_payload_errors(_payload(model)) == []
    resolved = _resolve(model)
    with pytest.raises(FalsificationComposeError, match="universe"):
        _compose_universe(["SPY"], resolved)
    with pytest.raises(ResearchModelError, match="universe"):
        study_configuration(resolved, run_id="test", universe=("SPY",))


def test_persisted_bar_model_round_trips_its_subscriptions(tmp_path: Path) -> None:
    subscriptions = [{"data_type": "bars", "symbols": ["SPY"], "interval": "1d"}]
    model = _model(subscriptions)
    store = WorkspaceResearchModelStore(_WorkspacePaths(tmp_path))
    loaded = store.load("workspace-market", store.publish("workspace-market", model))
    assert loaded.bar_interval == "1d"
    configuration = study_configuration(loaded, run_id="persisted")
    assert cast(Mapping[str, object], configuration["data"])["subscriptions"] == (
        subscriptions
    )
    with pytest.raises(ResearchModelError, match="subscriptions"):
        ResearchModel(
            "interval-only",
            "1.0.0",
            "Interval only",
            _SOURCE,
            ("data_ok",),
            {**_WINDOW, "interval": "1d", "symbols": ["SPY"]},
            {"tests/model.rs": "#[test] fn publication() {}"},
        )


@pytest.mark.parametrize(
    "fields",
    [
        {"interval": "1d"},
        {"interval": None},
        {"symbols": ["SPY", "QQQ"]},
        {"symbols": ["QQQ"]},
        {"symbols": []},
        {"symbols": "SPY"},
        {"symbols": ["spy"]},
        {"symbols": [" SPY"]},
        {"requiredSymbols": ["QQQ"]},
        {"required_symbols": ["QQQ"]},
        {"data": {"symbols": ["SPY", "QQQ"]}},
        {"data": {"bar_interval": "1d"}},
        {"data": {"bar_interval": None}},
        {"data": "invalid"},
    ],
)
def test_model_and_contract_reject_contradictory_fetch_fields(
    fields: dict[str, object],
) -> None:
    model = _model([{"data_type": "bars", "symbols": ["SPY"], "interval": "1min"}])
    configuration = {**model.harness_configuration, **fields}
    with pytest.raises(ResearchModelError):
        replace(model, harness_configuration=configuration)
    document = {**model.canonical_document, "harnessConfiguration": configuration}
    payload = {**_payload(model), "model": document}
    assert hypothesis_payload_errors(payload)
    with pytest.raises(FalsificationComposeError):
        decode_persisted_model(document)
    # Live configuration is loaded from the pinned snapshot, not the contract.
    with pytest.raises(ResearchModelError):
        _resolve(model, configuration=configuration)


@pytest.mark.parametrize(
    "fields", [{"interval": "1min"}, {"data": {"bar_interval": "1d"}}]
)
def test_tick_only_rejects_unused_interval(fields: dict[str, object]) -> None:
    model = _model([{"data_type": "quote", "symbols": ["SPY"]}])
    with pytest.raises(ResearchModelError, match="interval"):
        replace(model, harness_configuration={**model.harness_configuration, **fields})


def test_global_symbols_preserve_exact_echo_and_broader_universe() -> None:
    subscriptions = [
        {"data_type": "bars", "symbols": ["SPY"], "interval": "1min"},
        {"data_type": "quote", "symbols": ["BTC-USD", "BTC/USD"]},
    ]
    model = _model(subscriptions)
    model = replace(
        model,
        harness_configuration={
            **model.harness_configuration,
            "symbols": ["BTC/USD", "SPY", "BTC-USD"],
            "data": {"symbols": ["SPY", "BTC-USD", "BTC/USD"]},
        },
    )
    universe = ["SPY", "BTC-USD", "BTC/USD", "QQQ"]
    payload = {**_payload(model), "universe": universe}
    assert hypothesis_payload_errors(payload) == []
    assert _compose_universe(universe, model) == tuple(universe)
    resolved = _resolve(model)
    assert resolved.harness_configuration == model.harness_configuration
    assert _compose_universe(universe, resolved) == tuple(universe)
    configuration = study_configuration(model, run_id="mixed", universe=universe)
    data = cast(Mapping[str, object], configuration["data"])
    assert data["subscriptions"] == subscriptions
    assert data["symbols"] == ["SPY", "BTC-USD", "BTC/USD"]
    assert "bar_interval" not in data
    assert configuration["start"] == _WINDOW["start"]
    assert configuration["end"] == _WINDOW["end"]


def test_contract_rejects_multiple_bar_intervals() -> None:
    model = _model([{"data_type": "bars", "symbols": ["SPY"], "interval": "1min"}])
    document = model.canonical_document
    configuration = cast(dict[str, object], document["harnessConfiguration"])
    configuration["subscriptions"] = [
        {"data_type": "bars", "symbols": ["SPY"], "interval": "1min"},
        {"data_type": "bars", "symbols": ["SPY"], "interval": "1d"},
    ]
    with pytest.raises(ResearchModelError, match="single bars interval"):
        _resolve(model, configuration=configuration)
    with pytest.raises(FalsificationComposeError, match="single bars interval"):
        decode_persisted_model(document)


def test_compiler_revalidates_mutated_nested_fetch_fields() -> None:
    data: dict[str, object] = {"symbols": ["SPY"]}
    model = _model([{"data_type": "quote", "symbols": ["SPY"]}])
    model = replace(
        model, harness_configuration={**model.harness_configuration, "data": data}
    )
    data["symbols"] = ["SPY", "QQQ"]
    with pytest.raises(ResearchModelError, match="exact subscription symbol union"):
        study_configuration(model, run_id="mutated")


def test_crlf_runtime_source_hash_and_fractional_window_parity(tmp_path: Path) -> None:
    source = _SOURCE.replace("\n", "\r\n")
    window = {
        "start": "2026-09-21T13:30:00.123456789Z",
        "end": "2026-09-28T13:30:00.987654321Z",
    }
    model = replace(
        _model([{"data_type": "quote", "symbols": ["SPY"]}]),
        source=source,
        harness_configuration={
            **window,
            "subscriptions": [{"data_type": "quote", "symbols": ["SPY"]}],
        },
    )
    store = WorkspaceResearchModelStore(_WorkspacePaths(tmp_path))
    reference = store.publish("workspace-market", model)
    loaded = store.load("workspace-market", reference)
    assert loaded.source == source
    assert loaded.canonical_document["source"] == source
    backtests = _Backtests()
    BacktestResearchModelRunner(backtests, models=store).run(
        ResearchModelRunRequest(
            "crlf", "workspace-market", loaded, "workflow", "experiment"
        )
    )
    assert backtests.request is not None
    configuration = backtests.request.bundle.canonical_document["harnessConfiguration"]
    assert isinstance(configuration, dict)
    instance = configuration["strategy_instances"][0]
    assert instance["parameters"]["source"] == _SOURCE
    assert (
        instance["bundle_hash"] == hashlib.sha256(_SOURCE.encode("utf-8")).hexdigest()
    )
    assert (
        instance["bundle_hash"]
        == hashlib.sha256(instance["parameters"]["source"].encode("utf-8")).hexdigest()
    )
    assert configuration["start"] == window["start"]
    assert configuration["end"] == window["end"]


def test_nested_subscriptions_remain_exact_without_spy_widening() -> None:
    subscriptions = [
        {"data_type": "bars", "symbols": ["QQQ"], "interval": "1d"},
        {"data_type": "quote", "symbols": ["QQQ", "BTC-USD"]},
    ]
    model = replace(
        _model(subscriptions),
        harness_configuration={
            **_WINDOW,
            "data": {"subscriptions": subscriptions},
        },
    )
    configuration = study_configuration(
        model, run_id="nested", universe=("SPY", "QQQ", "BTC-USD")
    )
    data = cast(Mapping[str, object], configuration["data"])
    assert data["subscriptions"] == subscriptions
    assert data["symbols"] == ["QQQ", "BTC-USD"]
    assert "bar_interval" not in data
    instances = cast(list[dict[str, object]], configuration["strategy_instances"])
    assert instances[0]["symbols"] == ["QQQ", "BTC-USD"]


@pytest.mark.parametrize("kind", ["quote", "bars"])
def test_nested_subscription_echo_is_supported_without_legacy_interval(
    kind: str,
) -> None:
    subscription: dict[str, object] = {"data_type": kind, "symbols": ["QQQ"]}
    if kind == "bars":
        subscription["interval"] = "1min"
    model = replace(
        _model([subscription]),
        harness_configuration={**_WINDOW, "data": {"subscriptions": [subscription]}},
    )
    assert model.bar_interval == ("1min" if kind == "bars" else None)
    payload = {**_payload(model), "universe": ["QQQ"]}
    assert hypothesis_payload_errors(payload) == []
    resolved = _resolve(model, _compose_research_model(payload["model"]))
    assert resolved.harness_configuration == model.harness_configuration
    persisted = decode_persisted_model(model.canonical_document)
    assert isinstance(persisted, ResearchModel)
    assert persisted.canonical_document == model.canonical_document
    assert _compose_universe(None, model) == ("QQQ",)
    with pytest.raises(FalsificationComposeError, match="universe"):
        _compose_universe(["SPY"], model)
    configuration = study_configuration(model, run_id="nested", universe=("SPY", "QQQ"))
    data = cast(Mapping[str, object], configuration["data"])
    assert data["subscriptions"] == [subscription]
    assert "bar_interval" not in data
    assert data["symbols"] == ["QQQ"]


def _strategy(configuration: Mapping[str, object]) -> dict[str, object]:
    return {
        "strategyId": "market-strategy",
        "version": "1.0.0",
        "entrypoint": "src/lib.rs",
        "files": {"src/lib.rs": "// Rust source"},
        "tests": {"tests/model.rs": "#[test] fn publication() {}"},
        "harnessConfiguration": configuration,
        "exitCriteria": [
            {
                "falsifierId": "f1",
                "observable": "data_ok",
                "condition": "lt",
                "threshold": 1.0,
                "description": "Missing data",
            }
        ],
        "limitations": [],
    }


@pytest.mark.parametrize("nested", [True, False])
def test_strategy_echo_preserves_exact_inputs(nested: bool) -> None:
    subscriptions = [{"data_type": "quote", "symbols": ["SPY"]}]
    configuration = (
        {"data": {"subscriptions": subscriptions}}
        if nested
        else {"subscriptions": subscriptions}
    )
    design = StrategyDesign.from_mapping(_strategy(configuration))
    persisted = design.bundle.canonical_document["harnessConfiguration"]
    assert isinstance(persisted, dict)
    assert persisted["data"]["subscriptions"] == subscriptions


def test_strategy_rejects_invalid_echo() -> None:
    with pytest.raises(StrategyBundleError):
        StrategyDesign.from_mapping(
            _strategy(
                {
                    "data": {
                        "subscriptions": [
                            {
                                "data_type": "quote",
                                "symbols": ["SPY"],
                                "interval": "1min",
                            }
                        ]
                    }
                }
            )
        )


@pytest.mark.parametrize(
    "fields",
    [{"symbols": ["SPY", "QQQ"]}, {"interval": "1d"}, {"data": {"bar_interval": "1d"}}],
)
def test_strategy_rejects_contradictory_fetch_fields(fields: dict[str, object]) -> None:
    with pytest.raises(StrategyBundleError):
        StrategyDesign.from_mapping(
            _strategy(
                {
                    "subscriptions": [
                        {"data_type": "bars", "symbols": ["SPY"], "interval": "1min"}
                    ],
                    **fields,
                }
            )
        )


def test_prompts_use_unified_callbacks_and_explicit_mixed_inputs() -> None:
    for prompt in (_model_creation_instructions(), _strategy_design_instructions()):
        assert "read_sdk_reference" in prompt
        assert "consumer, program, events, exports or indicator_api" in prompt
        assert "never guess compiler APIs, callback signatures" in prompt
        assert "configuration.subscriptions" in prompt
        assert "indicator_ids" in prompt
        assert "on_bar_frame" not in prompt
    from thesis.workspaces.programs import WorkspacePrograms

    programs = WorkspacePrograms(cast(Any, None))
    consumer = programs.read_sdk_reference("consumer")["source"]
    assert isinstance(consumer, str)
    assert "MarketConsumer" in consumer
    assert "on_bar" in consumer
    strategy = programs.read_sdk_reference("strategy")["source"]
    assert isinstance(strategy, str)
    assert "Subscription::Bars" in strategy
