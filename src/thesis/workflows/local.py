"""Local workflow-runtime composition for the no-Azure Thesis stack.

The local stack needs the structured chat trigger to execute real workflows
without a full platform deployment.  :func:`build_local_workflow_runtime`
composes the same :class:`~thesis.workflows.runtime.WorkflowRuntime` used in
production over a single OpenRouter-backed model profile and a backtest runner
that drives the harness ``run_backtest`` MCP tool.

The runner is resolved from the caller's seam: an explicit
:class:`~thesis.workflows.backtests.BacktestRunner` wins, otherwise a harness
tool provider is adapted by :class:`McpBacktestRunner`.  Supplying neither is a
composition error (:class:`~thesis.workflows.runtime.WorkflowRuntimeError`).
"""

from __future__ import annotations

import json
import tempfile
import time
from collections.abc import Callable, Mapping

from thesis.agent.harness_tools import (
    HARNESS_TOOL_NAMES,
    HarnessToolProvider,
    HarnessToolResult,
    open_harness_tool_provider,
)
from thesis.agent.router import ModelProfile, ModelProvider, Role, RolePolicy
from thesis.agent.skills import SkillRegistry
from thesis.integrations.mcp import HarnessMcpClient
from thesis.research.indicators import IndicatorStore
from thesis.research.models import ResearchModelStore
from thesis.workflows.backtests import (
    BacktestMode,
    BacktestRunError,
    BacktestRunner,
    BacktestRunRequest,
    BacktestRunResult,
)
from thesis.workflows.runtime import (
    WorkflowRuntime,
    WorkflowRuntimeError,
    build_workflow_runtime,
)

_LOCAL_PROFILE_ID = "local-openrouter"
_LOCAL_POLICY_VERSION = "1.0.0"
_ROLES: tuple[Role, ...] = ("primary", "coding", "critique", "research")
_PENDING_STATES = frozenset({"queued", "submitted", "pending", "running"})
_SUCCESS_STATES = frozenset({"succeeded", "completed", "success", "complete"})


class McpBacktestRunner:
    """Adapt the harness ``run_backtest`` MCP tool to the backtest port.

    An inline ``harness_configuration`` mapping is materialized to a temporary
    JSON document before the call; a string configuration is treated as an
    existing on-disk path and passed through unchanged.
    """

    def __init__(
        self,
        harness_tools: HarnessToolProvider,
        *,
        mode: BacktestMode = "local",
        metadata: Mapping[str, object] | None = None,
        poll_attempts: int = 200,
        poll_interval_seconds: float = 0.05,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if mode not in ("local", "remote"):
            raise ValueError("mode must be 'local' or 'remote'")
        self._harness_tools = harness_tools
        self._mode: BacktestMode = mode
        self._metadata = dict(metadata or {})
        if poll_attempts < 1:
            raise ValueError("poll_attempts must be positive")
        if poll_interval_seconds < 0:
            raise ValueError("poll_interval_seconds must be non-negative")
        if not callable(sleep):
            raise TypeError("sleep must be callable")
        self._poll_attempts = poll_attempts
        self._poll_interval_seconds = poll_interval_seconds
        self._sleep = sleep

    def run(self, request: BacktestRunRequest) -> BacktestRunResult:
        """Launch one harness backtest and map its structured result."""
        path = _materialize_configuration(request.bundle.harness_configuration)
        launched = self._harness_tools.call_tool("run_backtest", {"config_path": path})
        if not launched.ok:
            detail = (
                launched.text
                or ("" if launched.failure is None else launched.failure.message)
                or "harness backtest launch failed"
            )
            raise BacktestRunError(detail)
        return self._resolve(request.run_id, launched)

    def _resolve(
        self, requested_run_id: str, launched: HarnessToolResult
    ) -> BacktestRunResult:
        structured = dict(launched.structured)
        run_id = _first_text(structured, "run_id", "job_id") or requested_run_id
        state = _first_text(structured, "status", "state") or (
            "succeeded" if structured.get("exit_code") == 0 else "failed"
        )
        if state.casefold() in _PENDING_STATES:
            for _ in range(self._poll_attempts):
                status = self._harness_tools.call_tool(
                    "get_backtest_status", {"job_id": run_id}
                )
                if not status.ok:
                    raise BacktestRunError(
                        status.text or "backtest status query failed"
                    )
                state = _first_text(status.structured, "status", "state") or state
                if state.casefold() not in _PENDING_STATES:
                    break
                self._sleep(self._poll_interval_seconds)
            else:
                raise BacktestRunError(
                    f"backtest {run_id!r} did not reach a terminal state"
                )
        if structured.get("metrics") is None:
            run = self._harness_tools.call_tool("get_run", {"run_id": run_id})
            if not run.ok:
                raise BacktestRunError(run.text or "backtest result query failed")
            structured.update(run.structured)
        if structured.get("result_artifacts", structured.get("artifacts")) is None:
            artifacts = self._harness_tools.call_tool(
                "list_artifacts", {"run_id": run_id}
            )
            if artifacts.ok:
                structured["result_artifacts"] = artifacts.structured.get("artifacts")
        if state.casefold() not in _SUCCESS_STATES and not _first_text(
            structured, "error", "error_message", "reason"
        ):
            status = self._harness_tools.call_tool(
                "get_backtest_status", {"job_id": run_id}
            )
            if status.ok:
                detail = _first_text(
                    status.structured, "error_message", "error", "reason"
                )
                if detail:
                    structured["error"] = detail
        structured["state"] = state
        resolved = HarnessToolResult(
            tool=launched.tool,
            ok=True,
            structured=structured,
            text=launched.text,
        )
        return self._result(run_id, resolved)

    def _result(self, run_id: str, result: HarnessToolResult) -> BacktestRunResult:
        structured = dict(result.structured)
        job_id = _first_text(structured, "run_id", "job_id") or run_id
        state = _first_text(structured, "status", "state") or (
            "succeeded" if structured.get("exit_code") == 0 else "failed"
        )
        return BacktestRunResult(
            job_id=job_id,
            state=state,
            mode=self._mode,
            manifest=structured.get("manifest"),
            metrics=structured.get("metrics"),
            result_artifacts=structured.get(
                "result_artifacts", structured.get("artifacts")
            ),
            error=_first_text(structured, "error", "error_message", "reason"),
        )


def build_local_workflow_runtime(
    *,
    model_provider: ModelProvider,
    model: str,
    harness_tools: HarnessToolProvider | None = None,
    harness_client: HarnessMcpClient | None = None,
    runner: BacktestRunner | None = None,
    backtest_mode: BacktestMode = "local",
    metadata: Mapping[str, object] | None = None,
    research_model_store: ResearchModelStore | None = None,
    indicator_store: IndicatorStore | None = None,
) -> WorkflowRuntime:
    """Compose the local workflow runtime over one OpenRouter model profile.

    Every role resolves to the same ``local-openrouter`` profile and the
    published harness tool catalog.  A ``harness_client`` shared with the
    backtest boundary is adapted into a :class:`HarnessToolProvider` without
    opening or initializing a second MCP transport.  The backtest port is the
    injected ``runner`` when supplied, otherwise a :class:`McpBacktestRunner`
    over ``harness_tools``; providing neither raises
    :class:`~thesis.workflows.runtime.WorkflowRuntimeError`.
    """
    if harness_tools is None and harness_client is not None:
        harness_tools = open_harness_tool_provider(client=harness_client)
    if runner is not None:
        resolved_runner: BacktestRunner = runner
    elif harness_tools is not None:
        resolved_runner = McpBacktestRunner(
            harness_tools, mode=backtest_mode, metadata=metadata
        )
    else:
        raise WorkflowRuntimeError(
            "provide a backtest runner or a harness tool provider"
        )
    model_profiles = {
        _LOCAL_PROFILE_ID: ModelProfile(
            profile_id=_LOCAL_PROFILE_ID,
            version=_LOCAL_POLICY_VERSION,
            provider="openrouter",
            model=model,
        )
    }
    role_policies = {
        role: RolePolicy(
            role=role,
            version=_LOCAL_POLICY_VERSION,
            model_profile=_LOCAL_PROFILE_ID,
            skills=(),
            allowed_tools=HARNESS_TOOL_NAMES,
        )
        for role in _ROLES
    }
    return build_workflow_runtime(
        role_policies=role_policies,
        model_profiles=model_profiles,
        providers={"openrouter": model_provider},
        skills=SkillRegistry(),
        runner=resolved_runner,
        harness_tools=harness_tools,
        backtest_mode=backtest_mode,
        metadata=metadata,
        research_model_store=research_model_store,
        indicator_store=indicator_store,
    )


def _materialize_configuration(configuration: object) -> str:
    """Return a configuration path, writing inline mappings to a temp file."""
    if isinstance(configuration, str) and configuration.strip():
        return configuration
    if not isinstance(configuration, Mapping):
        raise BacktestRunError("harness configuration must be a path or mapping")
    document = json.dumps(
        _jsonable(configuration),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as handle:
        handle.write(document)
        return handle.name


def _jsonable(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    return value


def _first_text(values: Mapping[str, object], *keys: str) -> str | None:
    for key in keys:
        candidate = values.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate
    return None


__all__ = [
    "McpBacktestRunner",
    "build_local_workflow_runtime",
]
