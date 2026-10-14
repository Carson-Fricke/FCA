"""Portable harness launch, safe lookup, and bounded job polling."""

from collections.abc import Mapping
from typing import cast

import pytest
from tests.thesis.submissions import MemorySubmissions

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
            message="job 'job-1' does not exist",
            execution_certainty="observed_failure",
        ),
    )


def _request(configuration: object = None) -> BacktestRunRequest:
    bundle = StrategyBundle(
        strategy_id="strategy",
        version="1.0.0",
        source=(StrategyFile("src/lib.rs", b"source"),),
        tests=(StrategyFile("tests/test.rs", b"test"),),
        harness_configuration={"seed": 7} if configuration is None else configuration,
    )
    return BacktestRunRequest(
        job_id="job-1",
        workspace_id="workspace-demo",
        bundle=bundle,
        workflow_execution_id="execution-1",
        experiment_id="experiment-1",
    )


@pytest.mark.parametrize("runner_type", [McpBacktestRunner, McpStudyRunner])
def test_configuration_mapping_is_sent_inline_with_the_submitted_job(
    runner_type,
) -> None:
    tools = _Tools(
        _absent(),
        _ok(job_id="job-1", state="succeeded", metrics={"edge": 1}, artifacts=[]),
    )
    result = runner_type(
        cast(HarnessToolProvider, tools), submissions=MemorySubmissions()
    ).run(_request())
    assert result.succeeded
    assert result.job_id == "job-1"
    assert tools.calls[0] == ("get_job_status", {"job_id": "job-1"})
    assert tools.calls[1] == (
        runner_type._launch_tool,
        {"config": {"seed": 7, "job_id": "job-1"}},
    )


def test_harness_host_path_cannot_bypass_immutable_submission() -> None:
    tools = _Tools(_absent())
    with pytest.raises(BacktestRunError, match="inline mapping"):
        McpBacktestRunner(
            cast(HarnessToolProvider, tools), submissions=MemorySubmissions()
        ).run(_request("/harness/config.json"))
    assert [name for name, _ in tools.calls] == ["get_job_status"]


def test_launch_acknowledgement_mismatch_fails_closed() -> None:
    tools = _Tools(
        _absent(),
        _ok(job_id="job-other", state="succeeded", metrics={}, artifacts=[]),
    )
    with pytest.raises(BacktestRunError, match="job-other"):
        McpBacktestRunner(
            cast(HarnessToolProvider, tools), submissions=MemorySubmissions()
        ).run(_request())


def test_poll_acknowledgement_mismatch_fails_closed() -> None:
    tools = _Tools(
        _ok(job_id="job-1", state="running"),
        _ok(job_id="job-other", state="succeeded"),
    )
    with pytest.raises(BacktestRunError, match="job-other"):
        McpBacktestRunner(
            cast(HarnessToolProvider, tools), submissions=MemorySubmissions()
        ).run(_request())


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
        McpBacktestRunner(
            cast(HarnessToolProvider, tools), submissions=MemorySubmissions()
        ).run(_request())
    assert len(tools.calls) == 1


def test_raised_lookup_failure_never_launches() -> None:
    tools = _Tools(OSError("connection lost"))
    with pytest.raises(BacktestRunError, match="connection lost"):
        McpBacktestRunner(
            cast(HarnessToolProvider, tools), submissions=MemorySubmissions()
        ).run(_request())
    assert len(tools.calls) == 1


@pytest.mark.parametrize("state", [None, "unknown"])
def test_unknown_lookup_state_never_launches(state) -> None:
    tools = _Tools(_ok(state=state))
    with pytest.raises(BacktestRunError, match="unknown state"):
        McpBacktestRunner(
            cast(HarnessToolProvider, tools), submissions=MemorySubmissions()
        ).run(_request())
    assert len(tools.calls) == 1


def test_pending_job_is_joined_and_polled_then_its_result_is_fetched() -> None:
    tools = _Tools(
        _ok(job_id="job-1", state="running"),
        _ok(job_id="job-1", state="succeeded"),
        _ok(job_id="job-1", state="succeeded", metrics={"edge": 1}),
        _ok(artifacts=[]),
    )
    result = McpBacktestRunner(
        cast(HarnessToolProvider, tools), submissions=MemorySubmissions()
    ).run(_request())
    assert result.state == "succeeded"
    assert result.succeeded
    assert tools.calls == [
        ("get_job_status", {"job_id": "job-1"}),
        ("get_job_status", {"job_id": "job-1"}),
        ("get_job_result", {"job_id": "job-1"}),
        ("list_artifacts", {"job_id": "job-1"}),
    ]


def test_terminal_success_job_is_joined_and_never_relaunched() -> None:
    tools = _Tools(
        _ok(
            job_id="job-1",
            state="succeeded",
            metrics={"edge": 1},
            result_artifacts=[],
        )
    )
    result = McpBacktestRunner(
        cast(HarnessToolProvider, tools), submissions=MemorySubmissions()
    ).run(_request())
    assert result.succeeded
    assert [name for name, _ in tools.calls] == ["get_job_status"]


@pytest.mark.parametrize("state", ["succeeded", "failed", "cancelled"])
def test_terminal_job_is_never_relaunched(state) -> None:
    tools = _Tools(_ok(job_id="job-1", state=state, metrics={}, result_artifacts=[]))
    McpBacktestRunner(
        cast(HarnessToolProvider, tools), submissions=MemorySubmissions()
    ).run(_request())
    assert all(name != "run_backtest" for name, _ in tools.calls)


@pytest.mark.parametrize("state", ["failed", "cancelled"])
def test_terminal_failure_keeps_diagnostic_without_querying_missing_results(
    state,
) -> None:
    tools = _Tools(
        _ok(job_id="job-1", state=state, error_message="study failed"),
    )
    result = McpStudyRunner(
        cast(HarnessToolProvider, tools), submissions=MemorySubmissions()
    ).run(_request())
    assert result.state == state
    assert result.error == "study failed"
    assert len(tools.calls) == 1


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
        McpStudyRunner(
            cast(HarnessToolProvider, tools), submissions=MemorySubmissions()
        ).run(_request())
    assert len(tools.calls) == 2


def test_polling_obeys_overall_deadline() -> None:
    elapsed = [0.0]

    def sleep(seconds: float) -> None:
        elapsed[0] += seconds

    tools = _Tools(
        _ok(job_id="job-1", state="running"),
        *[_ok(job_id="job-1", state="running") for _ in range(5)],
    )
    runner = McpStudyRunner(
        cast(HarnessToolProvider, tools),
        submissions=MemorySubmissions(),
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
    tools = _Tools(
        _ok(job_id="job-1", state="running"),
        _ok(job_id="job-1", state="running"),
    )
    runner = McpStudyRunner(
        cast(HarnessToolProvider, tools),
        submissions=MemorySubmissions(),
        cancelled=lambda request: len(tools.calls) == 2,
    )
    with pytest.raises(BacktestRunError, match="cancelled"):
        runner.run(_request())
    assert len(tools.calls) == 2


@pytest.mark.parametrize(
    "failure", [OSError("status unavailable"), _ok(job_id="job-1", state="unknown")]
)
def test_poll_failure_does_not_retry_launch(failure) -> None:
    tools = _Tools(_absent(), _ok(job_id="job-1", state="queued"), failure)
    with pytest.raises(BacktestRunError, match="status unavailable|unknown state"):
        McpStudyRunner(
            cast(HarnessToolProvider, tools), submissions=MemorySubmissions()
        ).run(_request())
    assert [name for name, _ in tools.calls].count("run_study") == 1


def test_launch_without_acknowledged_job_id_fails_closed() -> None:
    tools = _Tools(
        _absent(),
        _ok(state="succeeded", metrics={}, artifacts=[]),
    )
    with pytest.raises(BacktestRunError, match="did not acknowledge"):
        McpBacktestRunner(
            cast(HarnessToolProvider, tools), submissions=MemorySubmissions()
        ).run(_request())


def test_poll_without_acknowledged_job_id_fails_closed() -> None:
    tools = _Tools(
        _ok(job_id="job-1", state="running"),
        _ok(state="succeeded"),
    )
    with pytest.raises(BacktestRunError, match="did not acknowledge"):
        McpBacktestRunner(
            cast(HarnessToolProvider, tools), submissions=MemorySubmissions()
        ).run(_request())


def test_launch_without_state_is_unknown_not_a_domain_failure() -> None:
    tools = _Tools(_absent(), _ok(job_id="job-1"))
    with pytest.raises(BacktestRunError, match="unknown state"):
        McpStudyRunner(
            cast(HarnessToolProvider, tools), submissions=MemorySubmissions()
        ).run(_request())
    assert len(tools.calls) == 2
