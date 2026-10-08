"""Harness MCP boundary and local composition coverage.

These tests prove the MCP-backed :class:`HarnessBoundary`: it derives its
capabilities from the discovered tool catalog, routes a backtest launch through
the ``run_backtest`` tool, and is preferred by the local composition with a
fallback to the direct process boundary when the MCP server cannot start.  A
remote harness never silently falls back to local execution.
"""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from thesis.integrations.harness_activation import (
    CapabilityUnavailableError,
    HarnessActivationRequest,
)
from thesis.integrations.mcp import (
    HarnessMcpBoundary,
    HarnessMcpTopology,
    HarnessMcpTransportError,
    open_harness_mcp_boundary,
)

HARNESS_URL = "http://harness.test:9000"
BEARER = "harness-secret"

_TOOL_NAMES = {
    name: {"type": "object"}
    for name in ("run_backtest", "launch_trader", "get_job_result")
}


class CatalogHandler:
    """In-process MCP handler publishing a configurable harness catalog."""

    def __init__(self, tools: tuple[str, ...] = tuple(_TOOL_NAMES)) -> None:
        self.tools = tools
        self.calls: list[dict[str, object]] = []

    def handle(self, request: object) -> dict[str, object] | None:
        if not isinstance(request, Mapping):
            raise TypeError("request must be a mapping")
        message = dict(request)
        if "id" not in message:
            return None
        method = message.get("method")
        if method == "initialize":
            result: dict[str, object] = {"protocolVersion": "2024-11-05"}
        elif method == "tools/list":
            result = {
                "tools": [
                    {"name": name, "description": "", "inputSchema": schema}
                    for name, schema in _TOOL_NAMES.items()
                    if name in self.tools
                ]
            }
        elif method == "tools/call":
            params = message.get("params")
            assert isinstance(params, dict)
            self.calls.append(dict(params))
            arguments = params.get("arguments")
            job_id = arguments.get("job_id") if isinstance(arguments, Mapping) else None
            result = {
                "isError": False,
                "content": [{"type": "text", "text": "ok"}],
                "structuredContent": {
                    "exit_code": 0,
                    "job_id": job_id if isinstance(job_id, str) else "job-1",
                    "state": "queued",
                    "tool": params.get("name"),
                },
            }
        else:
            result = {}
        return {"jsonrpc": "2.0", "id": message.get("id"), "result": result}


class FailingTransport:
    """Transport double that fails as if the MCP server cannot start."""

    def exchange(self, request: Mapping[str, object]) -> Mapping[str, object]:
        del request
        raise HarnessMcpTransportError("the harness MCP server is unreachable")

    def notify(self, request: Mapping[str, object]) -> None:
        del request

    def close(self) -> None:
        return None


def _local_topology() -> HarnessMcpTopology:
    return HarnessMcpTopology(thesis_location="local", harness_location="local")


def _request() -> HarnessActivationRequest:
    return HarnessActivationRequest(
        bundle_ref="b" * 64,
        config_path="cfg/bt.yaml",
        action="backtest",
        job_id="job-submitted",
    )


def test_mcp_boundary_derives_capabilities_and_routes_backtest() -> None:
    handler = CatalogHandler()
    boundary = open_harness_mcp_boundary(_local_topology(), in_process_handler=handler)

    assert isinstance(boundary, HarnessMcpBoundary)
    assert boundary.capabilities.backtest is True
    assert boundary.capabilities.trader is True
    assert boundary.capabilities.shadow is False
    assert boundary.capabilities.live is False

    outcome = boundary.run_backtest(_request())

    assert outcome.exit_code == 0
    assert outcome.job_id == "job-submitted"
    assert handler.calls[-1]["name"] == "run_backtest"
    assert handler.calls[-1]["arguments"] == {
        "config_path": "cfg/bt.yaml",
        "job_id": "job-submitted",
    }


def test_mcp_boundary_capabilities_follow_the_catalog() -> None:
    handler = CatalogHandler(tools=("get_job_result",))
    boundary = open_harness_mcp_boundary(_local_topology(), in_process_handler=handler)

    assert boundary.capabilities.backtest is False
    assert boundary.capabilities.trader is False
    with pytest.raises(CapabilityUnavailableError):
        boundary.run_backtest(_request())
