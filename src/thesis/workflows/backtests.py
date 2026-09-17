"""Backtest execution ports for Thesis workflows.

The workflow core only knows the :class:`BacktestRunner` port.  Two concrete
seams exist:

- local mode goes through the published Thesis-to-harness client boundary
  (:class:`thesis.integrations.harness.ThesisHarnessClient`), whose local
  process adapter drives the harness ``run_backtest`` service;
- remote mode uses the same published client over the authenticated remote
  harness API, behind the identical port.

Both return opaque JSON-shaped values; Thesis never imports engine objects.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal, Protocol

from thesis.integrations import (
    BacktestRequest,
    HarnessBacktestResult,
    HarnessError,
    ThesisHarnessClient,
)
from thesis.strategies.bundles import StrategyBundle

BacktestMode = Literal["local", "remote"]

_SUCCESS_STATES = frozenset({"succeeded", "completed", "success", "complete"})
_PENDING_STATES = frozenset({"queued", "submitted", "pending", "running"})


class BacktestRunError(RuntimeError):
    """Raised when a backtest run request or result is not usable."""


class BacktestRunTimeout(BacktestRunError):
    """Raised when a run has not reached a terminal state before the deadline.

    The run may still be executing - for example a first-time market-data
    backfill - so callers can re-check the deterministic run id instead of
    recording a false failure.
    """


@dataclass(frozen=True, slots=True)
class BacktestRunRequest:
    """The deterministic input handed to an injected backtest runner."""

    run_id: str
    workspace_id: str
    bundle: StrategyBundle
    workflow_execution_id: str
    experiment_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id.strip():
            raise BacktestRunError("run_id must be a non-empty string")
        object.__setattr__(self, "run_id", self.run_id.strip())
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
            "runId": self.run_id,
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


class HarnessBacktestRunner:
    """Adapter over the published Thesis-to-harness client boundary."""

    def __init__(
        self,
        client: ThesisHarnessClient,
        *,
        mode: BacktestMode = "local",
        metadata: Mapping[str, object] | None = None,
        poll_attempts: int = 200,
        poll_interval_seconds: float = 0.05,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not isinstance(client, ThesisHarnessClient):
            raise TypeError("client must be a ThesisHarnessClient")
        if mode not in ("local", "remote"):
            raise ValueError("mode must be 'local' or 'remote'")
        self._client = client
        self._mode: BacktestMode = mode
        self._metadata = dict(metadata or {})
        if poll_attempts < 1:
            raise ValueError("poll_attempts must be positive")
        if poll_interval_seconds < 0:
            raise ValueError("poll_interval_seconds must be non-negative")
        self._poll_attempts = poll_attempts
        self._poll_interval_seconds = poll_interval_seconds
        self._sleep = sleep

    def run(self, request: BacktestRunRequest) -> BacktestRunResult:
        """Launch one backtest and resolve pending jobs with one result query."""
        payload = BacktestRequest(
            run_id=request.run_id,
            configuration=request.bundle.harness_configuration,
            strategy_bundle=request.bundle.canonical_document,
            metadata={
                **self._metadata,
                "experimentId": request.experiment_id,
                "strategyId": request.bundle.strategy_id,
                "workspaceId": request.workspace_id,
                "workflowExecutionId": request.workflow_execution_id,
            },
        )
        launched = self._client.launch_backtest(payload)
        root = _result_root(launched)
        result = launched
        if launched.state.casefold() in _PENDING_STATES:
            for _ in range(self._poll_attempts):
                result = self._client.query_backtest(launched.job_id)
                root = _result_root(result) or root
                if result.state.casefold() not in _PENDING_STATES:
                    break
                self._sleep(self._poll_interval_seconds)
            else:
                raise BacktestRunError(
                    f"backtest {launched.job_id!r} did not reach a terminal state"
                )
        elif launched.metrics is None:
            result = self._client.query_backtest(launched.job_id)
            root = _result_root(result) or root
        return _result(result, self._mode, root)


def _result(
    result: HarnessBacktestResult, mode: BacktestMode, root: str | None
) -> BacktestRunResult:
    return BacktestRunResult(
        job_id=result.job_id,
        state=result.state,
        mode=mode,
        manifest=result.manifest,
        metrics=result.metrics,
        result_artifacts=result.result_artifacts,
        result_root=root,
        error=_error_text(result.raw),
    )


def _error_text(raw: Mapping[str, object]) -> str | None:
    """Return the harness failure message carried by a raw result mapping."""
    for key in ("error", "error_message", "errorMessage", "reason"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _result_root(result: HarnessBacktestResult) -> str | None:
    root = result.raw.get("resultRoot", result.raw.get("result_root"))
    return root if isinstance(root, str) else None


__all__ = [
    "BacktestMode",
    "BacktestRunError",
    "BacktestRunRequest",
    "BacktestRunResult",
    "BacktestRunTimeout",
    "BacktestRunner",
    "HarnessBacktestRunner",
    "HarnessError",
]
