"""Execute workspace research models over deterministic harness replay.

The public domain objects stay separate: this runner accepts a
:class:`~thesis.research.models.ResearchModel` whose ``source`` is an authored
Rust study program (``harness-strategy-sdk``), publishes it in the workspace,
and returns named observations. The harness JIT-compiles the program into a
no-order study at run time; workspace source is data to that compile step and
is never installed as a strategy.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Protocol, cast

from thesis.agent.runtime.schema import render_violations, violations
from thesis.research.models import (
    FittedModelRef,
    InMemoryResearchModelStore,
    ResearchModel,
    ResearchModelError,
    ResearchModelRef,
    ResearchModelStore,
    validate_market_configuration,
)
from thesis.strategies import StrategyBundle
from thesis.workflows.backtests import (
    BacktestRunner,
    BacktestRunRequest,
    BacktestRunResult,
)
from thesis.workspaces.programs import PROGRAM_ID_PATTERN, ProgramKind

PROGRAM_REFERENCE_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["workspaceId", "kind", "programId", "gitCommit", "version"],
    "properties": {
        "workspaceId": {"type": "string", "minLength": 1},
        "kind": {"type": "string", "enum": ["study", "strategy", "indicator"]},
        "programId": {"type": "string", "pattern": PROGRAM_ID_PATTERN},
        "gitCommit": {"type": "string", "pattern": r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$"},
        "version": {"type": "string", "pattern": r"^[0-9a-f]{64}$"},
    },
}


class ProgramSnapshots(Protocol):
    """Public committed snapshot port, implemented by WorkspacePrograms."""

    def load(
        self,
        workspace_id: str,
        kind: ProgramKind,
        program_id: str,
        commit: str | None = None,
    ) -> Mapping[str, object]:
        """Return receipt identity, assembled source and committed configuration.

        Latest loads also include ``validation`` (the stored validation.json
        receipt, or None), current ``sdkIdentity``, and fresh serving compiler
        ``validationIdentity`` for completion acceptance.
        Pinned loads assemble every dependency at the selected commit.
        """

    def job_result(self, job_id: str) -> Mapping[str, object]:
        """Return the harness's authoritative result payload for one job id.

        Forward validation reads it to prove that reported metrics and states
        are the harness's own, not values typed into a completion.
        """

    def verify_strategy_sketch(
        self, workspace_id: str, reference: Mapping[str, object]
    ) -> dict[str, object]:
        """Resolve a frozen sketch revision from authoritative workspace Git."""


@dataclass(frozen=True, slots=True)
class StudyProgramReference:
    """Accepted study identity and research metadata, never inline Rust source."""

    workspace_id: str
    program_id: str
    git_commit: str
    version: str
    description: str
    outputs: tuple[str, ...]
    tests: Mapping[str, str]

    @classmethod
    def from_document(cls, value: Mapping[str, object]) -> StudyProgramReference:
        reference = {
            key: value.get(key)
            for key in ("workspaceId", "kind", "programId", "gitCommit", "version")
        }
        misses = violations(PROGRAM_REFERENCE_SCHEMA, reference, "model")
        if misses:
            raise ResearchModelError(render_violations(misses))
        if value["kind"] != "study":
            raise ResearchModelError("research model reference must name a study")
        return cls(
            cast(str, value["workspaceId"]),
            cast(str, value["programId"]),
            cast(str, value["gitCommit"]),
            cast(str, value["version"]),
            cast(str, value["description"]),
            tuple(cast(Sequence[str], value["outputs"])),
            dict(cast(Mapping[str, str], value["tests"])),
        )


def resolve_study_reference(
    programs: ProgramSnapshots,
    workspace_id: str,
    reference: StudyProgramReference,
) -> ResearchModel:
    """Resolve the accepted immutable commit, never today's program version."""
    if reference.workspace_id != workspace_id:
        raise ResearchModelError("study reference belongs to another workspace")
    snapshot = programs.load(
        workspace_id, "study", reference.program_id, reference.git_commit
    )
    for key, expected in (
        ("workspaceId", workspace_id),
        ("kind", "study"),
        ("programId", reference.program_id),
        ("gitCommit", reference.git_commit),
        ("version", reference.version),
    ):
        if snapshot.get(key) != expected:
            raise ResearchModelError(
                f"committed study {key} does not match accepted reference"
            )
    configuration = cast(Mapping[str, object], snapshot["configuration"])
    study_window(configuration)
    return ResearchModel(
        model_id=reference.program_id,
        version=reference.version,
        description=reference.description,
        source=cast(str, snapshot["source"]),
        outputs=reference.outputs,
        tests=reference.tests,
        harness_configuration=configuration,
    )


_SAFE_STRATEGY_ID = re.compile(r"[^A-Za-z0-9._:@-]+")


@dataclass(frozen=True, slots=True)
class ResearchModelRunRequest:
    """One workspace-bound historical model evaluation."""

    job_id: str
    workspace_id: str
    model: ResearchModel
    workflow_execution_id: str
    experiment_id: str
    universe: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ResearchModelRunResult:
    """A model publication plus its harness-backed observations."""

    model_reference: ResearchModelRef
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
    """Run authored Rust study programs through the harness JIT seam."""

    def __init__(
        self,
        backtests: BacktestRunner,
        *,
        models: ResearchModelStore | None = None,
    ) -> None:
        if not callable(getattr(backtests, "run", None)):
            raise TypeError("backtests must implement BacktestRunner")
        self._backtests = backtests
        self._models = models or InMemoryResearchModelStore()

    def run(self, request: ResearchModelRunRequest) -> ResearchModelRunResult:
        """Publish the model and execute one deterministic replay."""
        if not isinstance(request, ResearchModelRunRequest):
            raise TypeError("request must be a ResearchModelRunRequest")
        model_reference = self._models.publish(request.workspace_id, request.model)
        runtime_configuration = {
            "sourceSha256": hashlib.sha256(request.model.source.encode()).hexdigest()
        }
        bundle = _adapter_bundle(request, model_reference)
        backtest = self._backtests.run(
            BacktestRunRequest(
                job_id=request.job_id,
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
        workspace_artifact = self._models.record_run(
            request.workspace_id,
            model_reference,
            evidence,
        )
        return ResearchModelRunResult(
            model_reference,
            backtest,
            workspace_artifact,
            {
                **runtime_configuration,
                "fit": {name: str(value) for name, value in fit.items()},
            },
            fitted_reference,
            fit,
        )


#: Keys that name the model's sample window in its harness configuration.
_START_WINDOW_KEYS = ("start", "start_date", "startDate", "dataStart")
_END_WINDOW_KEYS = ("end", "end_date", "endDate", "dataEnd")


def study_window(configuration: Mapping[str, object]) -> tuple[str, str]:
    """Return the inclusive-start, exclusive-end replay window for one study.

    The window is the model's to declare and is piped through to the fetch
    exactly as declared: canonical RFC3339 ``start``/``end`` values are honoured
    verbatim, and alternate date keys are normalized to UTC midnights with an
    exclusive end.  A bound the model never declares is a contract violation,
    not an invitation to substitute one — silently replaying a default range is
    how a 42-session model ends up fetching years of history it never reads.
    """
    start = _window_bound(configuration, _START_WINDOW_KEYS)
    end = _window_bound(configuration, _END_WINDOW_KEYS)
    if start is None or end is None:
        missing = [
            name for name, bound in (("start", start), ("end", end)) if bound is None
        ]
        raise ResearchModelError(
            "model harnessConfiguration must declare its sample window; "
            f"missing {', '.join(missing)}. Set harnessConfiguration.start and "
            "harnessConfiguration.end to the period the hypothesis needs."
        )
    return start, end


def _window_bound(
    configuration: Mapping[str, object], keys: Sequence[str]
) -> str | None:
    for key in keys:
        value = configuration.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        text = value.strip()
        if "T" in text:
            try:
                datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                continue
            return text
        try:
            parsed = date.fromisoformat(text)
        except ValueError:
            continue
        if key in _END_WINDOW_KEYS:
            parsed += timedelta(days=1)
        return f"{parsed.isoformat()}T00:00:00Z"
    return None


def study_configuration(
    model: ResearchModel,
    *,
    job_id: str,
    universe: Sequence[str] = (),
) -> dict[str, object]:
    """Materialize the harness study configuration for one research model.

    The model's harness configuration is piped through unchanged except for the
    fields the run owns: the study shell, job id, resolved symbol set, and the
    authored program instance.  ``start``/``end`` keep exactly the bounds the
    model declared; the same function backs both validation and execution, so
    the validated document is the one that runs.
    """
    configuration = _jsonable_mapping(model.harness_configuration)
    validate_market_configuration(configuration)
    configuration.pop("interval", None)
    subscriptions = configuration.pop("subscriptions", None)
    configuration.pop("symbols", None)
    configuration.pop("requiredSymbols", None)
    configuration.pop("required_symbols", None)
    data = configuration.get("data")
    if not isinstance(data, dict):
        data = {}
        configuration["data"] = data
    if subscriptions is None:
        subscriptions = cast(object, data.pop("subscriptions", None))
    if not isinstance(subscriptions, list) or not subscriptions:
        raise ResearchModelError("study configuration must declare subscriptions")
    symbols = list(
        dict.fromkeys(symbol for item in subscriptions for symbol in item["symbols"])
    )
    if universe and set(symbols) - set(universe):
        raise ResearchModelError(
            "subscription symbols must be present in the declared universe"
        )
    data["subscriptions"] = subscriptions
    data.pop("bar_interval", None)
    data["symbols"] = symbols
    resolved_symbols = data["symbols"]
    assert isinstance(resolved_symbols, list)
    window_start, window_end = study_window(configuration)
    configuration["start"] = window_start
    configuration["end"] = window_end
    configuration["shell"] = "study"
    configuration["job_id"] = job_id
    source = model.source.replace("\r\n", "\n")
    configuration["strategy_instances"] = [
        {
            # The JIT seam rejects a bundle_hash that is not the SHA-256 of
            # the LF-normalized source, matching the API and JIT compiler.
            "bundle_hash": hashlib.sha256(source.encode("utf-8")).hexdigest(),
            "capital_fraction": "1.0",
            "implementation": "jit",
            "instance_id": _instance_id(model.model_id),
            "parameters": {"source": source},
            "symbols": list(resolved_symbols),
        }
    ]
    return configuration


def _adapter_bundle(
    request: ResearchModelRunRequest,
    model_reference: ResearchModelRef,
) -> StrategyBundle:
    model = request.model
    configuration = study_configuration(
        model, job_id=request.job_id, universe=request.universe
    )
    return StrategyBundle.create(
        strategy_id=_instance_id(f"research-{model.model_id}"),
        # StrategyBundle is only the historical harness adapter, not program identity.
        version="1.0.0"
        if re.fullmatch(r"[0-9a-f]{64}", model.version)
        else model.version,
        source={"program.rs": model.source},
        tests=dict(model.tests),
        dependencies=[model_reference.key],
        harness_configuration=configuration,
        limitations=("No order capability is exposed to a study program.",),
    )


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
        published = sorted(name for name in source if isinstance(name, str))
        raise ValueError(
            "research model run published no fit parameters; publish fitted "
            "parameters as fit.<parameter> metrics (published metrics: "
            f"{', '.join(published) or 'none'})"
        )
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
    "PROGRAM_REFERENCE_SCHEMA",
    "ProgramSnapshots",
    "StudyProgramReference",
    "resolve_study_reference",
    "BacktestResearchModelRunner",
    "ResearchModelRunRequest",
    "ResearchModelRunResult",
    "ResearchModelRunner",
    "study_configuration",
    "study_window",
]
