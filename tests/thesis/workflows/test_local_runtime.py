"""Coverage for the local workflow-runtime composition.

The local runtime binds a single OpenRouter profile, the harness tool catalog,
and a backtest runner.  These tests exercise runtime construction with an
injected fake runner and the MCP runner's mapping from a harness tool result to
the workflow ``BacktestRunResult``.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import cast

import pytest

from thesis.agent.context import PromptContext
from thesis.agent.harness_tools import (
    HARNESS_TOOL_NAMES,
    HarnessToolFailure,
    HarnessToolProvider,
    HarnessToolResult,
)
from thesis.agent.router import ModelProfile, ModelResponse
from thesis.integrations.mcp import HarnessMcpClient, InProcessMcpTransport
from thesis.strategies import StrategyBundle
from thesis.workflows import (
    BacktestRunRequest,
    BacktestRunResult,
    McpBacktestRunner,
    WorkflowRuntime,
    WorkflowRuntimeError,
    build_local_workflow_runtime,
)
from thesis.workflows.backtests import BacktestRunError, BacktestRunTimeout
from thesis.workflows.local import McpStudyRunner


class FakeModelProvider:
    """Deterministic fake model provider behind the policy boundary."""

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def invoke(
        self,
        *,
        profile: ModelProfile,
        prompt: PromptContext,
        allowed_tools: tuple[str, ...],
    ) -> ModelResponse:
        self.prompts.append(prompt.rendered)
        return ModelResponse(output="{}")


class FakeRunner:
    """Deterministic fake backtest runner behind the runner port."""

    def __init__(self) -> None:
        self.requests: list[BacktestRunRequest] = []

    def run(self, request: BacktestRunRequest) -> BacktestRunResult:
        self.requests.append(request)
        return BacktestRunResult(job_id="job-1", state="completed", mode="local")


class FakeHarnessTools:
    """Minimal harness tool provider returning one canned structured result.

    The runner probes ``get_backtest_status`` first to join an existing run, so
    the probe answers "not found" unless a status result is supplied.
    """

    def __init__(
        self, result: HarnessToolResult, *, status: HarnessToolResult | None = None
    ) -> None:
        self._result = result
        self._status = status or HarnessToolResult(
            tool="get_backtest_status",
            ok=False,
            failure=HarnessToolFailure(
                kind="tool",
                message="backtest job 'run-model' does not exist",
                tool="get_backtest_status",
            ),
        )
        self.calls: list[tuple[str, dict[str, object]]] = []

    def call_tool(
        self, name: str, arguments: Mapping[str, object] | None = None
    ) -> HarnessToolResult:
        self.calls.append((name, dict(arguments or {})))
        if name == "get_backtest_status":
            return self._status
        return self._result


class SequencedHarnessTools:
    """Return one result per expected MCP operation."""

    def __init__(self, *, existing: str = "missing") -> None:
        self.calls: list[str] = []
        self.status_calls = 0
        self._existing = existing

    def call_tool(
        self, name: str, arguments: Mapping[str, object] | None = None
    ) -> HarnessToolResult:
        del arguments
        self.calls.append(name)
        if name == "get_backtest_status":
            self.status_calls += 1
            if self.status_calls == 1:
                if self._existing == "missing":
                    return HarnessToolResult(
                        tool=name,
                        ok=False,
                        failure=HarnessToolFailure(
                            kind="tool",
                            message="backtest job 'run-model' does not exist",
                            tool=name,
                        ),
                    )
                return HarnessToolResult(
                    tool=name,
                    ok=True,
                    structured={"job_id": "run-model", "state": self._existing},
                )
            return HarnessToolResult(
                tool=name,
                ok=True,
                structured={
                    "job_id": "run-model",
                    "state": "running" if self.status_calls == 2 else "succeeded",
                },
            )
        if name == "run_backtest":
            return HarnessToolResult(
                tool=name,
                ok=True,
                structured={"run_id": "run-model", "state": "running"},
            )
        if name == "get_run":
            return HarnessToolResult(
                tool=name,
                ok=True,
                structured={
                    "run_id": "run-model",
                    "status": "complete",
                    "metrics": {"rolling_corr_60d_median": "0.73"},
                },
            )
        if name == "list_artifacts":
            return HarnessToolResult(
                tool=name,
                ok=True,
                structured={"artifacts": [{"name": "metrics.json"}]},
            )
        raise AssertionError(name)


class AlwaysRunningHarnessTools:
    """Report one job that never reaches a terminal state."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def call_tool(
        self, name: str, arguments: Mapping[str, object] | None = None
    ) -> HarnessToolResult:
        del arguments
        self.calls.append(name)
        if name == "run_backtest":
            return HarnessToolResult(
                tool=name,
                ok=True,
                structured={"run_id": "run-model", "state": "running"},
            )
        if name == "get_backtest_status":
            return HarnessToolResult(
                tool=name,
                ok=True,
                structured={"job_id": "run-model", "state": "running"},
            )
        raise AssertionError(name)


def _bundle() -> StrategyBundle:
    return StrategyBundle.create(
        strategy_id="momentum",
        version="1.0.0",
        source={"strategy.py": "class Strategy:\n    pass\n"},
        tests={"test_strategy.py": "def test_strategy():\n    assert True\n"},
        harness_configuration={"seed": 7, "symbols": ["SPY"]},
    )


def test_build_local_workflow_runtime_uses_injected_runner() -> None:
    runner = FakeRunner()
    provider = FakeModelProvider()

    runtime = build_local_workflow_runtime(
        model_provider=provider,
        model="test/model",
        runner=runner,
    )

    assert isinstance(runtime, WorkflowRuntime)
    assert runtime.runner is runner
    for role in ("primary", "coding", "critique", "research"):
        decision = runtime.router.resolve(role)
        assert decision.profile.provider == "openrouter"
        assert decision.profile.model == "test/model"


def test_build_local_workflow_runtime_requires_runner_or_harness() -> None:
    with pytest.raises(WorkflowRuntimeError, match="runner"):
        build_local_workflow_runtime(
            model_provider=FakeModelProvider(), model="test/model"
        )


class _CatalogHandler:
    """In-process MCP handler publishing the harness tool catalog."""

    def handle(self, request: object) -> dict[str, object] | None:
        if not isinstance(request, Mapping):
            raise TypeError("request must be a mapping")
        message = dict(request)
        if "id" not in message:
            return None
        if message.get("method") == "tools/list":
            result: dict[str, object] = {
                "tools": [
                    {"name": name, "description": "", "inputSchema": {"type": "object"}}
                    for name in HARNESS_TOOL_NAMES
                ]
            }
        else:
            result = {}
        return {"jsonrpc": "2.0", "id": message.get("id"), "result": result}


def test_build_local_workflow_runtime_adapts_a_shared_client() -> None:
    client = HarnessMcpClient(InProcessMcpTransport(_CatalogHandler()))

    runtime = build_local_workflow_runtime(
        model_provider=FakeModelProvider(),
        model="test/model",
        harness_client=client,
    )

    assert runtime.harness_tools is not None
    assert runtime.harness_tools.client is client
    assert runtime.runner is not None


def test_mcp_backtest_runner_maps_structured_result() -> None:
    result = HarnessToolResult(
        tool="run_backtest",
        ok=True,
        structured={
            "run_id": "run-9",
            "status": "completed",
            "metrics": {"sharpe": 1.2},
            "manifest": {"manifestId": "manifest-1"},
            "result_artifacts": [{"uri": "runs/run-9/fills.csv"}],
        },
    )
    tools = FakeHarnessTools(result)
    runner = McpBacktestRunner(cast(HarnessToolProvider, tools))
    request = BacktestRunRequest(
        run_id="run-fallback",
        workspace_id="workspace-demo",
        bundle=_bundle(),
        workflow_execution_id="execution-1",
        experiment_id="experiment-1",
    )

    mapped = runner.run(request)

    assert mapped.job_id == "run-9"
    assert mapped.state == "completed"
    assert mapped.metrics == {"sharpe": 1.2}
    assert mapped.manifest == {"manifestId": "manifest-1"}
    assert mapped.result_artifacts == [{"uri": "runs/run-9/fills.csv"}]
    assert tools.calls[0][0] == "get_backtest_status"
    assert tools.calls[1][0] == "run_backtest"
    config_path = tools.calls[1][1]["config_path"]
    assert isinstance(config_path, str) and config_path
    with open(config_path, encoding="utf-8") as handle:
        assert json.load(handle) == {"seed": 7, "symbols": ["SPY"]}


def test_mcp_study_runner_dispatches_the_study_tool() -> None:
    result = HarnessToolResult(
        tool="run_study",
        ok=True,
        structured={
            "run_id": "run-study",
            "status": "completed",
            "metrics": {"custom": {"fit.beta": "0.5"}},
        },
    )
    tools = FakeHarnessTools(result)
    runner = McpStudyRunner(cast(HarnessToolProvider, tools))

    mapped = runner.run(
        BacktestRunRequest(
            run_id="run-study",
            workspace_id="workspace-demo",
            bundle=_bundle(),
            workflow_execution_id="execution-1",
            experiment_id="experiment-1",
        )
    )

    assert mapped.job_id == "run-study"
    assert mapped.state == "completed"
    assert tools.calls[0][0] == "get_backtest_status"
    assert tools.calls[1][0] == "run_study"


def test_local_api_composes_a_study_backed_research_runner() -> None:
    from pathlib import Path

    script = (
        Path(__file__).resolve().parents[3]
        / "scripts"
        / "local"
        / "thesis_local_api.py"
    )
    source = script.read_text(encoding="utf-8")

    assert "McpStudyRunner(" in source
    assert "BacktestResearchModelRunner(" in source
    assert "research_runner=research_runner" in source


def test_local_api_grants_the_coordinator_control_tools() -> None:
    from pathlib import Path

    script = (
        Path(__file__).resolve().parents[3]
        / "scripts"
        / "local"
        / "thesis_local_api.py"
    )
    source = script.read_text(encoding="utf-8")

    allowed = source.split("_AGENT_ALLOWED_TOOLS", 1)[1].split(")", 1)[0]
    assert "HARNESS_TOOL_NAMES" in allowed
    assert "THESIS_TOOL_NAMES" in allowed


def test_local_api_spawns_phase_agents_from_the_workflow_state_machine() -> None:
    from pathlib import Path

    script = (
        Path(__file__).resolve().parents[3]
        / "scripts"
        / "local"
        / "thesis_local_api.py"
    )
    source = script.read_text(encoding="utf-8")

    assert "subagent_lifecycle=subagent_lifecycle" in source
    assert (
        "_development_runtime(\n            model_provider, harness_tools, lifecycle\n        )"
        in source
    )


def test_mcp_backtest_runner_defaults_state_from_exit_code() -> None:
    result = HarnessToolResult(
        tool="run_backtest",
        ok=True,
        structured={"job_id": "job-2", "exit_code": 0},
    )
    tools = FakeHarnessTools(result)
    runner = McpBacktestRunner(cast(HarnessToolProvider, tools))

    mapped = runner.run(
        BacktestRunRequest(
            run_id="run-fallback",
            workspace_id="workspace-demo",
            bundle=_bundle(),
            workflow_execution_id="execution-1",
            experiment_id="experiment-1",
        )
    )

    assert mapped.job_id == "job-2"
    assert mapped.state == "succeeded"


def test_mcp_backtest_runner_surfaces_launch_failure() -> None:
    result = HarnessToolResult(
        tool="run_backtest",
        ok=False,
        failure=HarnessToolFailure(
            kind="tool",
            message="research adapter could not load model source",
            tool="run_backtest",
        ),
    )
    runner = McpBacktestRunner(cast(HarnessToolProvider, FakeHarnessTools(result)))

    with pytest.raises(BacktestRunError, match="could not load model source"):
        runner.run(
            BacktestRunRequest(
                run_id="run-model",
                workspace_id="workspace-demo",
                bundle=_bundle(),
                workflow_execution_id="execution-1",
                experiment_id="experiment-1",
            )
        )


def test_mcp_runner_polls_then_reads_custom_metrics_and_artifacts() -> None:
    tools = SequencedHarnessTools()
    runner = McpBacktestRunner(
        cast(HarnessToolProvider, tools),
        poll_interval_seconds=0,
        sleep=lambda _: None,
    )

    result = runner.run(
        BacktestRunRequest(
            run_id="run-model",
            workspace_id="workspace-demo",
            bundle=_bundle(),
            workflow_execution_id="execution-1",
            experiment_id="experiment-1",
        )
    )

    assert result.state == "complete"
    assert result.metrics == {"rolling_corr_60d_median": "0.73"}
    assert result.result_artifacts == [{"name": "metrics.json"}]
    assert tools.calls == [
        "get_backtest_status",
        "run_backtest",
        "get_backtest_status",
        "get_backtest_status",
        "get_run",
        "list_artifacts",
    ]


def test_mcp_runner_joins_an_existing_pending_job_without_launching() -> None:
    tools = SequencedHarnessTools(existing="running")
    runner = McpBacktestRunner(
        cast(HarnessToolProvider, tools),
        poll_interval_seconds=0,
        sleep=lambda _: None,
    )

    result = runner.run(
        BacktestRunRequest(
            run_id="run-model",
            workspace_id="workspace-demo",
            bundle=_bundle(),
            workflow_execution_id="execution-1",
            experiment_id="experiment-1",
        )
    )

    assert result.state == "complete"
    assert "run_backtest" not in tools.calls
    assert tools.calls[0] == "get_backtest_status"


def test_mcp_runner_relaunches_a_terminal_job_so_revisions_re_execute() -> None:
    tools = SequencedHarnessTools(existing="succeeded")
    runner = McpBacktestRunner(
        cast(HarnessToolProvider, tools),
        poll_interval_seconds=0,
        sleep=lambda _: None,
    )

    result = runner.run(
        BacktestRunRequest(
            run_id="run-model",
            workspace_id="workspace-demo",
            bundle=_bundle(),
            workflow_execution_id="execution-1",
            experiment_id="experiment-1",
        )
    )

    # A succeeded job may describe a superseded artifact, so it is relaunched
    # rather than adopted.
    assert result.state == "complete"
    assert "run_backtest" in tools.calls


def test_mcp_runner_raises_a_timeout_for_a_still_running_job() -> None:
    tools = AlwaysRunningHarnessTools()
    runner = McpBacktestRunner(
        cast(HarnessToolProvider, tools),
        poll_deadline_seconds=0.001,
        poll_interval_seconds=0,
        sleep=lambda _: None,
    )

    with pytest.raises(BacktestRunTimeout, match="did not reach a terminal state"):
        runner.run(
            BacktestRunRequest(
                run_id="run-model",
                workspace_id="workspace-demo",
                bundle=_bundle(),
                workflow_execution_id="execution-1",
                experiment_id="experiment-1",
            )
        )
