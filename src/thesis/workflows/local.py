"""Local workflow-runtime composition for the no-Azure Thesis stack.

The local stack needs the structured chat trigger to execute real workflows
without a full platform deployment.  :func:`build_local_workflow_runtime`
composes the same :class:`~thesis.application.workflows.WorkflowRuntime` used in
production over a single OpenRouter-backed model profile and a backtest runner
that drives the harness ``run_backtest`` MCP tool.

The runner is resolved from the caller's seam: an explicit
:class:`~thesis.workflows.backtests.BacktestRunner` wins, otherwise a harness
tool provider is adapted by :class:`McpBacktestRunner`.  Supplying neither is a
composition error (:class:`~thesis.application.workflows.WorkflowRuntimeError`).
"""

from __future__ import annotations

import math
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
from thesis.application.workflows import (
    WorkflowRuntime,
    WorkflowRuntimeError,
    build_workflow_runtime,
)
from thesis.integrations.mcp import HarnessMcpClient
from thesis.persistence.submissions import SqliteSubmissionStore, SubmissionStore
from thesis.research.models import ResearchModelStore
from thesis.workflows.backtests import (
    BacktestMode,
    BacktestRunError,
    BacktestRunner,
    BacktestRunRequest,
    BacktestRunResult,
)
from thesis.workflows.research_models import (
    BacktestResearchModelRunner,
    ProgramSnapshots,
    ResearchModelRunner,
)
from thesis.workspaces.local import default_workspace_root
from thesis_runtime.conversations.conversation import Conversations
from thesis_runtime.storage.ownership import assert_owned

_LOCAL_PROFILE_ID = "local-openrouter"
_LOCAL_POLICY_VERSION = "1.0.0"
_ROLES: tuple[Role, ...] = ("primary", "coding", "critique", "research")
_PENDING_STATES = frozenset({"queued", "running"})
_SUCCESS_STATES = frozenset({"succeeded"})
_FAILURE_STATES = frozenset({"failed", "cancelled"})
_TERMINAL_STATES = _SUCCESS_STATES | _FAILURE_STATES

#: Allow market-data ensure to finish, but never poll indefinitely.
_DEFAULT_POLL_INITIAL_INTERVAL_SECONDS = 0.25
_DEFAULT_POLL_MAX_INTERVAL_SECONDS = 5.0
_DEFAULT_POLL_TIMEOUT_SECONDS = 1800.0


class McpBacktestRunner:
    """Adapt the harness ``run_backtest`` MCP tool to the backtest port.

    Configuration mappings travel inline across hosts; string configurations
    name existing paths on the harness host.

    The submitted job id is allocated by the caller and carried unchanged: a
    pending job is joined and polled, and a terminal job's stored result is
    returned instead of relaunching the same id. A launch acknowledgement whose
    job id differs from the submitted one fails closed. Polling uses wall-clock
    backoff until the harness reports the job terminal, within a deadline; a
    status failure raises
    :class:`~thesis.workflows.backtests.BacktestRunError`.
    """

    _runner_label = "backtest"
    _launch_tool = "run_backtest"

    def __init__(
        self,
        harness_tools: HarnessToolProvider,
        *,
        mode: BacktestMode = "local",
        submissions: SubmissionStore | None = None,
        metadata: Mapping[str, object] | None = None,
        poll_interval_seconds: float = _DEFAULT_POLL_INITIAL_INTERVAL_SECONDS,
        poll_max_interval_seconds: float = _DEFAULT_POLL_MAX_INTERVAL_SECONDS,
        poll_timeout_seconds: float = _DEFAULT_POLL_TIMEOUT_SECONDS,
        cancelled: Callable[[BacktestRunRequest], bool] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if mode not in ("local", "remote"):
            raise ValueError("mode must be 'local' or 'remote'")
        self._harness_tools = harness_tools
        self._submissions = (
            submissions
            if submissions is not None
            else SqliteSubmissionStore(default_workspace_root() / ".submissions.sqlite")
        )
        self._mode: BacktestMode = mode
        self._metadata = dict(metadata or {})
        if not math.isfinite(poll_interval_seconds) or poll_interval_seconds < 0:
            raise ValueError("poll_interval_seconds must be non-negative")
        if (
            not math.isfinite(poll_max_interval_seconds)
            or poll_max_interval_seconds < poll_interval_seconds
        ):
            raise ValueError(
                "poll_max_interval_seconds must be at least poll_interval_seconds"
            )
        if not callable(sleep):
            raise TypeError("sleep must be callable")
        self._poll_interval_seconds = poll_interval_seconds
        self._poll_max_interval_seconds = poll_max_interval_seconds
        if not math.isfinite(poll_timeout_seconds) or poll_timeout_seconds <= 0:
            raise ValueError("poll_timeout_seconds must be finite and positive")
        self._poll_timeout_seconds = poll_timeout_seconds
        self._cancelled = cancelled
        self._monotonic = monotonic
        self._sleep = sleep

    def run(self, request: BacktestRunRequest) -> BacktestRunResult:
        """Join or launch one harness job and map its structured result."""
        self._check_cancelled(request)
        existing = self._existing_job(request.job_id)
        if existing is not None:
            return self._resolve(request, existing)
        arguments = self._launch_arguments(request)
        retained = self._submissions.get(request.workspace_id, request.job_id)
        if retained is not None:
            raise BacktestRunError(
                f"retained job {request.job_id!r} has no visible harness outcome; inspect it without replaying"
            )
        self._submissions.retain(
            request.workspace_id,
            request.job_id,
            self._launch_tool,
            {
                "arguments": arguments,
                "workflow_execution_id": request.workflow_execution_id,
            },
        )
        self._check_cancelled(request)
        launched = self._harness_tools.call_tool(self._launch_tool, arguments)
        if not launched.ok:
            detail = (
                launched.text
                or ("" if launched.failure is None else launched.failure.message)
                or f"harness {self._runner_label} launch failed"
            )
            raise BacktestRunError(detail)
        return self._resolve(request, launched)

    def _launch_arguments(self, request: BacktestRunRequest) -> dict[str, object]:
        """Render the harness launch arguments carrying the submitted job id."""
        configuration = request.bundle.harness_configuration
        if isinstance(configuration, Mapping):
            document = {
                str(key): _jsonable(value) for key, value in configuration.items()
            }
            document["job_id"] = request.job_id
            return {"config": document}
        raise BacktestRunError(
            "harness configuration must be an immutable inline mapping"
        )

    def _existing_job(self, job_id: str) -> HarnessToolResult | None:
        """Return the harness job for one job id, pending or terminal.

        A pending job is joined and polled; a terminal job's stored result is
        returned instead of relaunching the same job id. Only an exact
        not-found response is absence.
        """
        try:
            status = self._harness_tools.call_tool("get_job_status", {"job_id": job_id})
        except Exception as error:  # noqa: BLE001 - unknown lookup is not absence
            raise BacktestRunError(
                f"{self._runner_label} status lookup failed: {error}"
            ) from error
        if not status.ok:
            failure = status.failure
            # This is the harness's exact domain not-found response, not an
            # arbitrary transport/auth/protocol failure or a guessed substring.
            absent = f"job '{job_id}' does not exist"
            if (
                failure is not None
                and failure.kind in {"tool", "harness"}
                and (failure.message == absent or status.text == absent)
            ):
                return None
            raise BacktestRunError(
                status.text or (failure.message if failure else "status lookup failed")
            )
        state = (_text(status.structured.get("state")) or "").casefold()
        if state in _PENDING_STATES or state in _TERMINAL_STATES:
            return status
        raise BacktestRunError(
            f"{self._runner_label} status lookup returned unknown state {state!r}"
        )

    def _resolve(
        self, request: BacktestRunRequest, launched: HarnessToolResult
    ) -> BacktestRunResult:
        structured = dict(launched.structured)
        returned = _text(structured.get("job_id"))
        if returned is None:
            raise BacktestRunError(
                f"{self._runner_label} did not acknowledge submitted job "
                f"{request.job_id!r}"
            )
        if returned != request.job_id:
            raise BacktestRunError(
                f"{self._runner_label} acknowledged job_id {returned!r} for "
                f"submitted job {request.job_id!r}"
            )
        job_id = request.job_id
        state = (_text(structured.get("state")) or "").casefold()
        if state in _PENDING_STATES:
            terminal = self._poll_terminal(request, job_id)
            structured.update(terminal.structured)
            state = (_text(structured.get("state")) or "").casefold()
        if state not in _TERMINAL_STATES:
            raise BacktestRunError(
                f"{self._runner_label} returned unknown state {state!r}"
            )
        # Failed jobs need not have a result row. Keep their domain state and
        # diagnostic instead of replacing it with a result-query failure.
        if state not in _SUCCESS_STATES:
            structured["state"] = state
            return self._result(
                job_id,
                HarnessToolResult(tool=launched.tool, ok=True, structured=structured),
            )
        if structured.get("metrics") is None:
            job = self._harness_tools.call_tool("get_job_result", {"job_id": job_id})
            if not job.ok:
                raise BacktestRunError(
                    job.text or f"{self._runner_label} result query failed"
                )
            resolved_id = _text(job.structured.get("job_id"))
            if resolved_id is not None and resolved_id != job_id:
                raise BacktestRunError(
                    f"{self._runner_label} result query returned job_id "
                    f"{resolved_id!r} for submitted job {job_id!r}"
                )
            structured.update(job.structured)
        if structured.get("result_artifacts", structured.get("artifacts")) is None:
            artifacts = self._harness_tools.call_tool(
                "list_artifacts", {"job_id": job_id}
            )
            if artifacts.ok:
                structured["result_artifacts"] = artifacts.structured.get("artifacts")
        structured["state"] = state
        resolved = HarnessToolResult(
            tool=launched.tool,
            ok=True,
            structured=structured,
            text=launched.text,
        )
        return self._result(job_id, resolved)

    def _check_cancelled(self, request: BacktestRunRequest) -> None:
        assert_owned()
        if self._cancelled is not None and self._cancelled(request):
            raise BacktestRunError(f"{self._runner_label} polling cancelled")

    def _poll_terminal(
        self, request: BacktestRunRequest, job_id: str
    ) -> HarnessToolResult:
        """Poll until terminal, cancellation, or the overall polling deadline."""
        interval = self._poll_interval_seconds
        deadline = self._monotonic() + self._poll_timeout_seconds
        while True:
            self._check_cancelled(request)
            if self._monotonic() >= deadline:
                raise BacktestRunError(
                    f"{self._runner_label} {job_id!r} polling timed out"
                )
            try:
                status = self._harness_tools.call_tool(
                    "get_job_status", {"job_id": job_id}
                )
            except Exception as error:  # noqa: BLE001 - polling faults are terminal
                raise BacktestRunError(
                    f"{self._runner_label} status query failed: {error}"
                ) from error
            if not status.ok:
                raise BacktestRunError(
                    status.text
                    or (status.failure.message if status.failure else "")
                    or f"{self._runner_label} status query failed"
                )
            returned = _text(status.structured.get("job_id"))
            if returned is None:
                raise BacktestRunError(
                    f"{self._runner_label} status query did not acknowledge "
                    f"submitted job {job_id!r}"
                )
            if returned != job_id:
                raise BacktestRunError(
                    f"{self._runner_label} status query returned job_id "
                    f"{returned!r} for submitted job {job_id!r}"
                )
            self._check_cancelled(request)
            if self._monotonic() >= deadline:
                raise BacktestRunError(
                    f"{self._runner_label} {job_id!r} polling timed out"
                )
            state = (_text(status.structured.get("state")) or "").casefold()
            if state in _TERMINAL_STATES:
                return status
            if state not in _PENDING_STATES:
                raise BacktestRunError(
                    f"{self._runner_label} status query returned unknown state {state!r}"
                )
            self._sleep(min(interval, max(0.0, deadline - self._monotonic())))
            if interval > 0:
                interval = min(interval * 2, self._poll_max_interval_seconds)

    def _result(self, job_id: str, result: HarnessToolResult) -> BacktestRunResult:
        structured = dict(result.structured)
        state = _text(structured.get("state")) or "failed"
        return BacktestRunResult(
            job_id=job_id,
            state=state,
            mode=self._mode,
            manifest=structured.get("manifest"),
            metrics=structured.get("metrics"),
            result_artifacts=structured.get(
                "result_artifacts", structured.get("artifacts")
            ),
            error=_error_text(structured),
        )


class McpStudyRunner(McpBacktestRunner):
    """Adapt the harness ``run_study`` MCP tool to the backtest port.

    A study is a deterministic replay with no order execution, used to run
    research models and indicators. It is recorded as a study, never as a
    backtest.
    """

    _runner_label = "study"
    _launch_tool = "run_study"


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
    conversations: Conversations | None = None,
    programs: ProgramSnapshots | None = None,
) -> WorkflowRuntime:
    """Compose the local workflow runtime over one OpenRouter model profile.

    Every role resolves to the same ``local-openrouter`` profile and the
    published harness tool catalog.  A ``harness_client`` shared with the
    backtest boundary is adapted into a :class:`HarnessToolProvider` without
    opening or initializing a second MCP transport.  The backtest port is the
    injected ``runner`` when supplied, otherwise a :class:`McpBacktestRunner`
    over ``harness_tools``; providing neither raises
    :class:`~thesis.application.workflows.WorkflowRuntimeError`.
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
    resolved_research_runner: ResearchModelRunner | None = (
        None
        if harness_tools is None
        else BacktestResearchModelRunner(
            McpStudyRunner(harness_tools, mode=backtest_mode, metadata=metadata),
            models=research_model_store,
        )
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
        # When the study-backed research runner was composed above it already
        # owns the stores; forwarding them again beside the explicit runner is
        # a composition error, so they are only forwarded for the default
        # runner that ``build_workflow_runtime`` builds when no harness tools
        # were supplied.
        research_model_store=(
            research_model_store if resolved_research_runner is None else None
        ),
        research_runner=resolved_research_runner,
        conversations=conversations,
        programs=programs,
    )


def _jsonable(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    return value


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _error_text(values: Mapping[str, object]) -> str | None:
    for key in ("error", "error_message", "errorMessage", "reason"):
        candidate = values.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate
    return None


__all__ = [
    "McpBacktestRunner",
    "McpStudyRunner",
    "build_local_workflow_runtime",
]
