"""Portable harness launch, safe lookup, and bounded job polling."""

from collections.abc import Mapping
from typing import cast

import pytest

from thesis.agent.harness_tools import (
    HarnessToolFailure,
    HarnessToolProvider,
    HarnessToolResult,
)
from thesis.strategies.bundles import StrategyBundle, StrategyFile
from thesis.workflows.backtests import (
    BacktestRunError,
    BacktestRunRequest,
)
from thesis.workflows.local import McpBacktestRunner, McpStudyRunner


class _Tools:
    def __init__(self, *results: HarnessToolResult | Exception) -> None:
        self.results = list(results)
        self.calls: list[tuple[str, Mapping[str, object]]] = []

    def call_tool(
        self, name: str, arguments: Mapping[str, object]
    ) -> HarnessToolResult:
        self.calls.append((name, arguments))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _ok(**payload: object) -> HarnessToolResult:
    return HarnessToolResult(tool="stub", ok=True, structured=payload)


def _absent() -> HarnessToolResult:
    return HarnessToolResult(
        tool="get_job_status",
        ok=False,
        failure=HarnessToolFailure(
            kind="tool",
            message="job 'run-1' does not exist",
            execution_certainty="observed_failure",
        ),
    )


def _request(configuration: object = None) -> BacktestRunRequest:
    bundle = StrategyBundle(
        strategy_id="strategy",
        version="1.0.0",
        source=(StrategyFile("src/lib.rs", b"source"),),
        tests=(StrategyFile("tests/test.rs", b"test"),),
        harness_configuration={"run_id": "run-1", "symbols": ["SPY"]}
        if configuration is None
        else configuration,
    )
    return BacktestRunRequest(
        run_id="run-1",
        workspace_id="workspace-demo",
        bundle=bundle,
        workflow_execution_id="execution-1",
        experiment_id="experiment-1",
    )


@pytest.mark.parametrize("runner_type", [McpBacktestRunner, McpStudyRunner])
def test_configuration_mapping_is_sent_inline(runner_type) -> None:
    tools = _Tools(_absent(), _ok(state="complete", metrics={"edge": 1}, artifacts=[]))
    result = runner_type(cast(HarnessToolProvider, tools)).run(_request())
    assert result.succeeded
    assert tools.calls[1] == (
        runner_type._launch_tool,
        {"config": {"run_id": "run-1", "symbols": ["SPY"]}},
    )


def test_local_runtime_forwards_program_snapshot_port() -> None:
    from tests.thesis.workflows.registry.hypothesis_development._support import (
        FakePrograms,
    )

    from thesis.agent.router import ModelProvider
    from thesis.workflows.local import build_local_workflow_runtime

    programs = FakePrograms()
    runtime = build_local_workflow_runtime(
        model_provider=cast(ModelProvider, object()),
        model="test/model",
        runner=McpBacktestRunner(cast(HarnessToolProvider, _Tools())),
        programs=programs,
    )
    assert runtime._programs is programs


def test_explicit_harness_host_path_is_preserved() -> None:
    tools = _Tools(_absent(), _ok(state="complete", metrics={}, artifacts=[]))
    McpBacktestRunner(cast(HarnessToolProvider, tools)).run(
        _request("/harness/config.json")
    )
    assert tools.calls[1][1] == {"config_path": "/harness/config.json"}


@pytest.mark.parametrize("kind", ["transport", "auth", "protocol", "tool", "harness"])
def test_lookup_failure_never_launches_a_duplicate(kind) -> None:
    tools = _Tools(
        HarnessToolResult(
            tool="get_job_status",
            ok=False,
            failure=HarnessToolFailure(
                kind=kind,
                message="lookup failed",
                execution_certainty="unknown_outcome",
            ),
        )
    )
    with pytest.raises(BacktestRunError, match="lookup failed"):
        McpBacktestRunner(cast(HarnessToolProvider, tools)).run(_request())
    assert len(tools.calls) == 1


def test_raised_lookup_failure_never_launches() -> None:
    tools = _Tools(OSError("connection lost"))
    with pytest.raises(BacktestRunError, match="connection lost"):
        McpBacktestRunner(cast(HarnessToolProvider, tools)).run(_request())
    assert len(tools.calls) == 1


@pytest.mark.parametrize("state", [None, "unknown"])
def test_unknown_lookup_state_never_launches(state) -> None:
    tools = _Tools(_ok(state=state))
    with pytest.raises(BacktestRunError, match="unknown state"):
        McpBacktestRunner(cast(HarnessToolProvider, tools)).run(_request())
    assert len(tools.calls) == 1


def test_pending_job_is_joined_and_terminal_state_overrides_stale_status() -> None:
    tools = _Tools(
        _ok(job_id="run-1", status="running"),
        _ok(job_id="run-1", state="complete"),
        _ok(status="complete", metrics={"edge": 1}),
        _ok(artifacts=[]),
    )
    result = McpBacktestRunner(cast(HarnessToolProvider, tools)).run(_request())
    assert result.state == "complete"
    assert result.succeeded
    assert not any(name.startswith("run_") for name, _ in tools.calls)


@pytest.mark.parametrize("state", ["failed", "cancelled"])
def test_terminal_failure_keeps_diagnostic_without_querying_missing_results(
    state,
) -> None:
    tools = _Tools(
        _ok(state="running"),
        _ok(state=state, error_message="study failed"),
    )
    result = McpStudyRunner(cast(HarnessToolProvider, tools)).run(_request())
    assert result.state == state
    assert result.error == "study failed"
    assert len(tools.calls) == 2


@pytest.mark.parametrize("state", ["complete", "succeeded", "failed", "cancelled"])
def test_confirmed_terminal_job_still_allows_revision_launch(state) -> None:
    tools = _Tools(_ok(state=state), _ok(state="complete", metrics={}, artifacts=[]))
    McpBacktestRunner(cast(HarnessToolProvider, tools)).run(_request())
    assert tools.calls[1][0] == "run_backtest"


def test_launch_unknown_outcome_is_not_retried() -> None:
    tools = _Tools(
        _absent(),
        HarnessToolResult(
            tool="run_study",
            ok=False,
            failure=HarnessToolFailure(
                kind="transport",
                message="response lost",
                execution_certainty="unknown_outcome",
            ),
        ),
    )
    with pytest.raises(BacktestRunError, match="response lost"):
        McpStudyRunner(cast(HarnessToolProvider, tools)).run(_request())
    assert len(tools.calls) == 2


def test_polling_obeys_overall_deadline() -> None:
    elapsed = [0.0]

    def sleep(seconds: float) -> None:
        elapsed[0] += seconds

    tools = _Tools(_ok(state="running"), *[_ok(state="running") for _ in range(5)])
    runner = McpStudyRunner(
        cast(HarnessToolProvider, tools),
        poll_timeout_seconds=1,
        poll_interval_seconds=0.4,
        poll_max_interval_seconds=0.8,
        monotonic=lambda: elapsed[0],
        sleep=sleep,
    )
    with pytest.raises(BacktestRunError, match="timed out"):
        runner.run(_request())
    assert elapsed[0] == 1
    assert len(tools.calls) == 3


def test_polling_can_be_cancelled_without_launch_or_result_fetch() -> None:
    tools = _Tools(_ok(state="running"), _ok(state="running"))
    runner = McpStudyRunner(
        cast(HarnessToolProvider, tools),
        cancelled=lambda request: len(tools.calls) == 2,
    )
    with pytest.raises(BacktestRunError, match="cancelled"):
        runner.run(_request())
    assert len(tools.calls) == 2


@pytest.mark.parametrize(
    "failure", [OSError("status unavailable"), _ok(state="unknown")]
)
def test_poll_failure_does_not_retry_launch(failure) -> None:
    tools = _Tools(_absent(), _ok(state="queued"), failure)
    with pytest.raises(BacktestRunError, match="status unavailable|unknown state"):
        McpStudyRunner(cast(HarnessToolProvider, tools)).run(_request())
    assert [name for name, _ in tools.calls].count("run_study") == 1


def test_launch_without_state_is_unknown_not_a_domain_failure() -> None:
    tools = _Tools(_absent(), _ok(job_id="run-1"))
    with pytest.raises(BacktestRunError, match="unknown state"):
        McpStudyRunner(cast(HarnessToolProvider, tools)).run(_request())
    assert len(tools.calls) == 2
