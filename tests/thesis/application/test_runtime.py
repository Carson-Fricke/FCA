from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from itertools import count
from typing import cast

import pytest

from thesis.agent.context import PromptContext
from thesis.agent.router import (
    ModelProfile,
    ModelResponse,
    PolicyBundle,
    Role,
    RolePolicy,
)
from thesis.agent.skills import SkillRegistry, ThesisSkill
from thesis.application.workflows import (
    WorkflowRuntime,
    WorkflowRuntimeError,
    build_model_router,
    build_workflow_runtime,
)
from thesis.workflows.backtests import (
    BacktestRunner,
    BacktestRunRequest,
    BacktestRunResult,
)

WORKSPACE = "workspace-demo"
OWNER = "owner-1"
NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)
CLAIM = (
    "Five-day momentum in SPY predicts positive five-day forward returns; "
    "a momentum probe strategy should realize a Sharpe of at least 1.0."
)


def _clock() -> Callable[[], datetime]:
    ticks = count(1)

    def clock() -> datetime:
        return NOW.replace(microsecond=next(ticks))

    return clock


def _id_factory() -> Callable[[str], str]:
    counter = count(1)
    return lambda prefix: f"{prefix}-{next(counter)}"


def _hypothesis_envelope() -> dict[str, object]:
    return {
        "claimType": "predictive",
        "claim": CLAIM,
        "variables": ["momentum_5d", "forward_return_5d"],
        "nullHypothesis": (
            "Five-day momentum has no effect on five-day forward returns."
        ),
        "alternativeHypothesis": (
            "Five-day momentum increases five-day forward returns."
        ),
        "estimand": "mean five-day forward return conditional on five-day momentum",
        "endpoints": ["forward_return_5d at t+5 close"],
        "dataRequirements": {"symbols": ["SPY"], "minObservations": 60},
        "criteria": [
            {"metric": "sharpe", "operator": "gte", "threshold": 1.0},
            {"metric": "hitRate", "operator": "gte", "threshold": 0.55},
        ],
        "strategy": {
            "strategyId": "momentum-probe",
            "version": "1.0.0",
            "files": {
                "strategy.py": (
                    "from harness.strategy import Strategy\n\n"
                    "class MomentumProbe(Strategy):\n    pass\n"
                )
            },
            "tests": {"test_strategy.py": "def test_strategy():\n    assert True\n"},
            "harnessConfiguration": {"seed": 7, "symbols": ["SPY"]},
        },
    }


class FakeProvider:
    """Deterministic fake model provider behind the policy boundary."""

    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.prompts: list[str] = []

    def invoke(
        self,
        *,
        profile: ModelProfile,
        prompt: PromptContext,
        allowed_tools: tuple[str, ...],
        response_format: Mapping[str, object] | None = None,
    ) -> ModelResponse:
        self.prompts.append(prompt.rendered)
        return ModelResponse(output=json.dumps(self.payload))


class FakeRunner:
    """Deterministic fake backtest runner behind the runner port."""

    def __init__(
        self,
        *,
        metrics: object | None = None,
        state: str = "succeeded",
        manifest: object | None = None,
        result_artifacts: object | None = None,
    ) -> None:
        self.requests: list[BacktestRunRequest] = []
        self._metrics = metrics
        self._state = state
        self._manifest = manifest
        self._result_artifacts = result_artifacts

    def run(self, request: BacktestRunRequest) -> BacktestRunResult:
        self.requests.append(request)
        return BacktestRunResult(
            job_id="job-1",
            state=self._state,
            mode="local",
            manifest=self._manifest,
            metrics=self._metrics,
            result_artifacts=self._result_artifacts,
        )


def _runtime(
    *,
    provider: FakeProvider,
    clock: Callable[[], datetime],
    id_factory: Callable[[str], str],
    runner: BacktestRunner | None = None,
) -> WorkflowRuntime:
    skills = SkillRegistry(
        (
            ThesisSkill(
                "research",
                "1.0.0",
                "Turn claims into falsifiable, point-in-time analyses.",
                ("read_workspace", "run_backtest"),
            ),
            ThesisSkill(
                "coding",
                "1.0.0",
                "Inspect tests before changing implementation.",
                ("read_workspace", "write_patch"),
            ),
        )
    )
    profiles = {
        "research-profile": ModelProfile(
            profile_id="research-profile",
            version="1.0.0",
            provider="fake",
            model="research-model",
        ),
        "coding-profile": ModelProfile(
            profile_id="coding-profile",
            version="1.0.0",
            provider="fake",
            model="coding-model",
        ),
    }
    policies: dict[Role, RolePolicy] = {
        "research": RolePolicy(
            role="research",
            version="1.0.0",
            model_profile="research-profile",
            skills=("research",),
            allowed_tools=("read_workspace", "run_backtest"),
        ),
        "coding": RolePolicy(
            role="coding",
            version="1.0.0",
            model_profile="coding-profile",
            skills=("coding",),
            allowed_tools=("read_workspace", "write_patch"),
        ),
    }
    return build_workflow_runtime(
        role_policies=policies,
        model_profiles=profiles,
        providers={"fake": provider},
        skills=skills,
        runner=runner,
        bundle=PolicyBundle(
            bundle_id="thesis-platform",
            version="1.0.0",
            bundle_sha256="a" * 64,
            policy_sha256="b" * 64,
        ),
        clock=clock,
        id_factory=id_factory,
    )


def test_build_workflow_runtime_requires_runner() -> None:
    with pytest.raises(WorkflowRuntimeError, match="backtest runner"):
        _runtime(
            provider=FakeProvider(_hypothesis_envelope()),
            clock=_clock(),
            id_factory=_id_factory(),
        )


def test_workflow_runtime_rejects_runner_without_run_port() -> None:
    router = build_model_router(
        role_policies={},
        model_profiles={},
        providers={},
        skills=SkillRegistry(),
    )
    with pytest.raises(WorkflowRuntimeError, match="BacktestRunner port"):
        WorkflowRuntime(router=router, runner=cast("BacktestRunner", object()))
