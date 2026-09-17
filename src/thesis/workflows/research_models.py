"""Execute workspace research models over deterministic harness replay.

The public domain objects stay separate: this port accepts a
:class:`~thesis.research.models.ResearchModel`, publishes it and its workspace
indicators, and returns named observations. The current harness implementation
reuses market-event replay through a platform-owned no-order adapter; workspace
source is passed as data to that adapter and is never installed as a strategy
or global indicator.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from harness.indicators import IndicatorSpec
from thesis.research.indicators import (
    IndicatorRef,
    IndicatorScope,
    IndicatorStore,
    InMemoryIndicatorStore,
    ScopedIndicatorResolver,
    WorkspaceIndicator,
)
from thesis.research.models import (
    FittedModelRef,
    InMemoryResearchModelStore,
    ResearchModel,
    ResearchModelRef,
    ResearchModelStore,
)
from thesis.strategies import StrategyBundle
from thesis.workflows.backtests import (
    BacktestRunner,
    BacktestRunRequest,
    BacktestRunResult,
)

_SAFE_STRATEGY_ID = re.compile(r"[^A-Za-z0-9._:@-]+")


@dataclass(frozen=True, slots=True)
class ResearchModelRunRequest:
    """One workspace-bound historical model evaluation."""

    run_id: str
    workspace_id: str
    model: ResearchModel
    workflow_execution_id: str
    experiment_id: str
    universe: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ResearchModelRunResult:
    """A model publication plus its harness-backed observations."""

    model_reference: ResearchModelRef
    indicator_references: tuple[IndicatorRef, ...]
    backtest: BacktestRunResult
    workspace_artifact: str
    runtime_configuration: Mapping[str, object]
    fitted_reference: FittedModelRef | None
    fitted_parameters: Mapping[str, float] = field(default_factory=dict)


class ResearchModelRunner(Protocol):
    """Execution port for a workspace research model."""

    def run(self, request: ResearchModelRunRequest) -> ResearchModelRunResult:
        """Publish, evaluate, and return one model's named observations."""


class BacktestResearchModelRunner:
    """Run research source through the platform's no-order replay adapter."""

    def __init__(
        self,
        backtests: BacktestRunner,
        *,
        models: ResearchModelStore | None = None,
        indicators: IndicatorStore | None = None,
    ) -> None:
        if not callable(getattr(backtests, "run", None)):
            raise TypeError("backtests must implement BacktestRunner")
        self._backtests = backtests
        self._models = models or InMemoryResearchModelStore()
        self._indicators = indicators or InMemoryIndicatorStore()
        self._resolver = ScopedIndicatorResolver(self._indicators)

    def run(self, request: ResearchModelRunRequest) -> ResearchModelRunResult:
        """Publish the model/dependencies and execute one deterministic replay."""
        if not isinstance(request, ResearchModelRunRequest):
            raise TypeError("request must be a ResearchModelRunRequest")
        model_reference = self._models.publish(request.workspace_id, request.model)
        indicator_references: list[IndicatorRef] = []
        bindings: list[dict[str, object]] = []
        for binding in request.model.indicators:
            reference = binding.resolve(request.workspace_id, self._indicators)
            resolved = self._resolver.resolve(
                reference, workspace_id=request.workspace_id
            )
            indicator_references.append(reference)
            document: dict[str, object] = {
                "alias": binding.alias,
                "name": binding.name,
                "parameters": dict(binding.parameters),
                "reference": reference.key,
                "scope": binding.scope.value,
                "symbol": binding.symbol,
            }
            if binding.scope is IndicatorScope.WORKSPACE:
                if not isinstance(resolved, WorkspaceIndicator):
                    raise TypeError("workspace indicator resolved to a global spec")
                document.update(
                    {
                        "entrypoint": resolved.entrypoint,
                        "source": resolved.source,
                    }
                )
            elif not isinstance(resolved, IndicatorSpec):
                raise TypeError("global indicator resolved to workspace source")
            bindings.append(document)

        runtime_configuration: dict[str, object] = {
            **monitor_configuration(request.model, {}),
            "indicators": bindings,
        }
        runtime_configuration.pop("fit", None)
        bundle = _adapter_bundle(
            request,
            model_reference,
            tuple(indicator_references),
            runtime_configuration,
        )
        backtest = self._backtests.run(
            BacktestRunRequest(
                run_id=request.run_id,
                workspace_id=request.workspace_id,
                bundle=bundle,
                workflow_execution_id=request.workflow_execution_id,
                experiment_id=request.experiment_id,
            )
        )
        fit: dict[str, float] = {}
        fitted_reference: FittedModelRef | None = None
        if backtest.succeeded:
            fit = _extract_fit(backtest.metrics)
            fitted_reference = self._models.publish_fit(
                request.workspace_id, model_reference, fit
            )
        evidence = {
            **backtest.as_dict(),
            "fittedModelReference": (
                None if fitted_reference is None else fitted_reference.key
            ),
        }
        references = tuple(indicator_references)
        workspace_artifact = self._models.record_run(
            request.workspace_id,
            model_reference,
            evidence,
            references,
        )
        return ResearchModelRunResult(
            model_reference,
            references,
            backtest,
            workspace_artifact,
            {
                **runtime_configuration,
                "fit": {name: str(value) for name, value in fit.items()},
            },
            fitted_reference,
            fit,
        )


def _adapter_bundle(
    request: ResearchModelRunRequest,
    model_reference: ResearchModelRef,
    indicator_references: tuple[IndicatorRef, ...],
    runtime_configuration: Mapping[str, object],
) -> StrategyBundle:
    model = request.model
    configuration = _jsonable_mapping(model.harness_configuration)
    shorthand_symbols = (
        configuration.pop("symbols", None)
        or configuration.pop("requiredSymbols", None)
        or configuration.pop("required_symbols", None)
    )
    data = configuration.get("data")
    if not isinstance(data, dict):
        data = {}
        configuration["data"] = data
    symbols = data.get("symbols")
    if not isinstance(symbols, list) or not symbols:
        candidate_symbols: object = None
        if isinstance(shorthand_symbols, list | tuple) and shorthand_symbols:
            candidate_symbols = shorthand_symbols
        elif request.universe:
            candidate_symbols = request.universe
        elif model.indicators:
            candidate_symbols = list(
                dict.fromkeys(item.symbol for item in model.indicators)
            )
        if isinstance(candidate_symbols, Sequence) and not isinstance(
            candidate_symbols, str | bytes
        ):
            symbols = [str(item) for item in candidate_symbols if str(item).strip()]
        if symbols:
            data["symbols"] = list(dict.fromkeys(symbols))
    if not isinstance(symbols, list) or not symbols:
        symbols = ["SPY"]
        data["symbols"] = symbols
    configuration["symbols"] = list(data["symbols"])
    configuration["shell"] = "backtest"
    configuration["run_id"] = request.run_id
    configuration["strategy_instances"] = [
        {
            "bundle_hash": model.content_sha256,
            "capital_fraction": "1.0",
            "implementation": "harness.research.adapter:ResearchModelAdapter",
            "instance_id": _instance_id(model.model_id),
            "parameters": dict(runtime_configuration),
            "symbols": list(data["symbols"]),
        }
    ]
    dependencies = [model_reference.key, *(item.key for item in indicator_references)]
    return StrategyBundle.create(
        strategy_id=_instance_id(f"research-{model.model_id}"),
        version=model.version,
        source={
            "adapter.txt": (
                "Platform-owned harness.research.adapter; workspace model source "
                "is an isolated input, not a strategy.\n"
            )
        },
        tests={"test_adapter.txt": "Covered by harness research contract tests.\n"},
        dependencies=dependencies,
        harness_configuration=configuration,
        limitations=("No order capability is exposed to research source.",),
    )


def monitor_configuration(
    model: ResearchModel, fit: Mapping[str, float]
) -> dict[str, object]:
    """Return the frozen model configuration consumed by Layer B strategies."""
    bindings: list[dict[str, object]] = []
    for binding in model.indicators:
        document: dict[str, object] = {
            "alias": binding.alias,
            "name": binding.name,
            "parameters": dict(binding.parameters),
            "scope": binding.scope.value,
            "symbol": binding.symbol,
        }
        if binding.scope is IndicatorScope.WORKSPACE and binding.definition is not None:
            document.update(
                {
                    "entrypoint": binding.definition.entrypoint,
                    "source": binding.definition.source,
                }
            )
        bindings.append(document)
    return {
        # Configuration hashes reject binary floats; the monitor parses these
        # canonical decimal strings back to numbers at execution time.
        "fit": {name: str(value) for name, value in fit.items()},
        "indicators": bindings,
        "model_fit_entrypoint": model.fit_entrypoint,
        "model_observe_entrypoint": model.observe_entrypoint,
        "model_source": model.source,
    }


def _extract_fit(metrics: object | None) -> dict[str, float]:
    if not isinstance(metrics, Mapping):
        raise ValueError("research model run returned no metrics")
    custom = metrics.get("custom")
    source = custom if isinstance(custom, Mapping) else metrics
    fit: dict[str, float] = {}
    for name, value in source.items():
        if not isinstance(name, str) or not name.startswith("fit."):
            continue
        parameter = name.removeprefix("fit.")
        if not parameter.isidentifier():
            raise ValueError(f"invalid fitted parameter name {parameter!r}")
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ValueError(f"fitted parameter {parameter!r} is not numeric")
        try:
            number = float(value)
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"fitted parameter {parameter!r} is not numeric"
            ) from error
        if not math.isfinite(number):
            raise ValueError(f"fitted parameter {parameter!r} is not finite")
        fit[parameter] = number
    if not fit:
        raise ValueError("research model run returned no fit parameters")
    return fit


def _instance_id(value: str) -> str:
    normalized = _SAFE_STRATEGY_ID.sub("-", value).strip("-")
    return (normalized or "research-model")[:128]


def _jsonable_mapping(value: Mapping[str, object]) -> dict[str, object]:
    return {str(key): _jsonable(item) for key, item in value.items()}


def _jsonable(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_jsonable(item) for item in value]
    return value


__all__ = [
    "BacktestResearchModelRunner",
    "ResearchModelRunRequest",
    "ResearchModelRunResult",
    "ResearchModelRunner",
    "monitor_configuration",
]
