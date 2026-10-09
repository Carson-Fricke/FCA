"""Execution request/result records and injected backtest runner port."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal, Protocol

from thesis.strategies.bundles import StrategyBundle

BacktestMode = Literal["local", "remote"]

_SUCCESS_STATES = frozenset({"succeeded"})


class BacktestRunError(RuntimeError):
    """Raised when a backtest run request or result is not usable."""


@dataclass(frozen=True, slots=True)
class BacktestRunRequest:
    """The deterministic input handed to an injected backtest runner."""

    job_id: str
    workspace_id: str
    bundle: StrategyBundle
    workflow_execution_id: str
    experiment_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.job_id, str) or not self.job_id.strip():
            raise BacktestRunError("job_id must be a non-empty string")
        object.__setattr__(self, "job_id", self.job_id.strip())
        if not isinstance(self.workspace_id, str) or not self.workspace_id.strip():
            raise BacktestRunError("workspace_id must be a non-empty string")
        object.__setattr__(self, "workspace_id", self.workspace_id.strip())
        if not isinstance(self.bundle, StrategyBundle):
            raise BacktestRunError("bundle must be a StrategyBundle")
        if (
            not isinstance(self.workflow_execution_id, str)
            or not self.workflow_execution_id.strip()
        ):
            raise BacktestRunError("workflow_execution_id must be a non-empty string")
        if not isinstance(self.experiment_id, str) or not self.experiment_id.strip():
            raise BacktestRunError("experiment_id must be a non-empty string")

    @property
    def configuration(self) -> object:
        """Return the harness configuration declared by the bundle."""
        return self.bundle.harness_configuration

    def as_dict(self) -> dict[str, object]:
        """Return the portable request summary without bundle content."""
        return {
            "bundleSha256": self.bundle.bundle_sha256,
            "experimentId": self.experiment_id,
            "jobId": self.job_id,
            "strategyId": self.bundle.strategy_id,
            "workspaceId": self.workspace_id,
            "workflowExecutionId": self.workflow_execution_id,
        }


@dataclass(frozen=True, slots=True)
class BacktestRunResult:
    """The opaque outcome of one backtest run request."""

    job_id: str
    state: str
    mode: BacktestMode = "local"
    manifest: object | None = None
    metrics: object | None = None
    result_artifacts: object | None = None
    result_root: str | None = None
    error: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.job_id, str) or not self.job_id.strip():
            raise BacktestRunError("job_id must be a non-empty string")
        object.__setattr__(self, "job_id", self.job_id.strip())
        if not isinstance(self.state, str) or not self.state.strip():
            raise BacktestRunError("state must be a non-empty string")
        object.__setattr__(self, "state", self.state.strip())
        if self.mode not in ("local", "remote"):
            raise BacktestRunError("mode must be 'local' or 'remote'")
        if self.result_root is not None and (
            not isinstance(self.result_root, str) or not self.result_root.strip()
        ):
            raise BacktestRunError("result_root must be non-empty when set")
        if self.error is not None and (
            not isinstance(self.error, str) or not self.error.strip()
        ):
            raise BacktestRunError("error must be non-empty when set")

    @property
    def succeeded(self) -> bool:
        """Return whether the harness reported a successful terminal state."""
        return self.state.casefold() in _SUCCESS_STATES

    def as_dict(self) -> dict[str, object]:
        """Return the JSON-shaped summary retained by durable records."""
        return {
            "error": self.error,
            "jobId": self.job_id,
            "manifest": self.manifest,
            "metrics": self.metrics,
            "mode": self.mode,
            "resultArtifacts": self.result_artifacts,
            "state": self.state,
        }


class BacktestRunner(Protocol):
    """Injected boundary for local or remote harness backtest execution."""

    def run(self, request: BacktestRunRequest) -> BacktestRunResult:
        """Run one backtest and return its opaque result."""


__all__ = [
    "BacktestMode",
    "BacktestRunError",
    "BacktestRunRequest",
    "BacktestRunResult",
    "BacktestRunner",
]
