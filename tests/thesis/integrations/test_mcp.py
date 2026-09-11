"""Focused tests for the Thesis harness MCP client and boundary."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from harness.mcp.auth import EnvironmentBearerTokenSource
from harness.mcp.server import create_server
from harness.mcp.tools import HarnessToolsContext, ProcessOutcome, ToolCapabilities
from thesis.integrations.harness_activation import (
    CapabilityUnavailableError,
    HarnessActivationRequest,
)
from thesis.integrations.mcp import (
    HarnessMcpBoundary,
    HarnessMcpClient,
    HarnessMcpProtocolError,
    InProcessMcpTransport,
)


def _boundary(exit_code: int = 0) -> tuple[HarnessMcpBoundary, list[tuple[str, ...]]]:
    calls: list[tuple[str, ...]] = []

    def runner(argv: Sequence[str]) -> ProcessOutcome:
        calls.append(tuple(argv))
        return ProcessOutcome(exit_code, "out", "err")

    context = HarnessToolsContext(
        mode="local",
        credentials=EnvironmentBearerTokenSource(environ={}),
        capabilities=ToolCapabilities(trader=False),
        runner=runner,
        sweep_submitter=lambda spec_path: {"sweep_id": "s1", "spec": spec_path},
    )
    server = create_server(context)
    client = HarnessMcpClient(InProcessMcpTransport(server))
    return HarnessMcpBoundary.connect(client), calls


def _request() -> HarnessActivationRequest:
    return HarnessActivationRequest(
        bundle_ref="bundle-abc",
        config_path="config/harness/backtest.yaml",
        action="backtest",
    )


def test_boundary_initializes_and_reports_capabilities() -> None:
    boundary, _ = _boundary()

    assert boundary.capabilities.backtest is True
    assert boundary.capabilities.trader is False
    assert boundary.capabilities.shadow is False
    assert boundary.client.server_info["serverInfo"] is not None


def test_run_backtest_dispatches_the_mcp_tool() -> None:
    boundary, calls = _boundary(exit_code=0)

    outcome = boundary.run_backtest(_request())

    assert outcome.exit_code == 0
    assert outcome.bundle_ref == "bundle-abc"
    assert outcome.raw["boundary"] == "local"
    assert calls and calls[0][1] == "backtest-job"
    assert calls[0][-1] == "config/harness/backtest.yaml"


def test_nonzero_process_exit_is_reported_not_raised() -> None:
    boundary, _ = _boundary(exit_code=2)

    outcome = boundary.run_backtest(_request())

    assert outcome.exit_code == 2
    assert outcome.raw["stderr"] == "err"


def test_unknown_tool_raises_a_protocol_error() -> None:
    boundary, _ = _boundary()

    with pytest.raises(HarnessMcpProtocolError):
        boundary.client.call_tool("not_a_tool")


def test_trader_capability_is_unavailable_without_the_tool() -> None:
    boundary, _ = _boundary()

    with pytest.raises(CapabilityUnavailableError):
        boundary.launch_trader(
            HarnessActivationRequest(
                bundle_ref="bundle-abc",
                config_path="config/harness/trader.yaml",
                action="trader",
            )
        )
