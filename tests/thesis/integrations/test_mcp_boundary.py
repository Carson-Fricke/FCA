"""Harness MCP boundary and local composition coverage.

These tests prove the MCP-backed :class:`HarnessBoundary`: it derives its
capabilities from the discovered tool catalog, routes a backtest launch through
the ``run_backtest`` tool, and is preferred by the local composition with a
fallback to the direct process boundary when the MCP server cannot start.  A
remote harness never silently falls back to local execution.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import httpx
import pytest

from thesis.integrations import mcp
from thesis.integrations.harness import ProcessResult
from thesis.integrations.harness_activation import (
    CapabilityUnavailableError,
    HarnessActivationRequest,
    LocalHarnessProcess,
)
from thesis.integrations.mcp import (
    HarnessMcpBoundary,
    HarnessMcpTopology,
    HarnessMcpTransportError,
    open_harness_mcp_boundary,
    open_local_harness_boundary,
)

HARNESS_URL = "http://harness.test:9000"
BEARER = "harness-secret"

_TOOL_NAMES = {
    name: {"type": "object"} for name in ("run_backtest", "launch_trader", "get_run")
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
            result = {
                "isError": False,
                "content": [{"type": "text", "text": "ok"}],
                "structuredContent": {
                    "exit_code": 0,
                    "job_id": "job-1",
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


class RecordingProcessRunner:
    """Injected local process boundary that records the rendered argv."""

    def __init__(self) -> None:
        self.argv: tuple[str, ...] | None = None

    def __call__(self, argv: Sequence[str]) -> ProcessResult:
        self.argv = tuple(argv)
        return ProcessResult(returncode=0, stdout="{}", stderr="")


def _local_topology() -> HarnessMcpTopology:
    return HarnessMcpTopology(thesis_location="local", harness_location="local")


def _request() -> HarnessActivationRequest:
    return HarnessActivationRequest(
        bundle_ref="b" * 64, config_path="cfg/bt.yaml", action="backtest"
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
    assert outcome.job_id == "job-1"
    assert handler.calls[-1]["name"] == "run_backtest"
    assert handler.calls[-1]["arguments"] == {"config_path": "cfg/bt.yaml"}


def test_mcp_boundary_capabilities_follow_the_catalog() -> None:
    handler = CatalogHandler(tools=("get_run",))
    boundary = open_harness_mcp_boundary(_local_topology(), in_process_handler=handler)

    assert boundary.capabilities.backtest is False
    assert boundary.capabilities.trader is False
    with pytest.raises(CapabilityUnavailableError):
        boundary.run_backtest(_request())


def test_local_composition_prefers_the_mcp_boundary() -> None:
    handler = CatalogHandler()
    runner = RecordingProcessRunner()

    boundary = open_local_harness_boundary(
        topology=_local_topology(),
        in_process_handler=handler,
        process_runner=runner,
    )

    assert isinstance(boundary, HarnessMcpBoundary)
    assert runner.argv is None


def test_local_composition_falls_back_to_the_process_boundary() -> None:
    runner = RecordingProcessRunner()

    boundary = open_local_harness_boundary(
        topology=_local_topology(),
        stdio_transport=FailingTransport(),
        process_runner=runner,
    )

    assert isinstance(boundary, LocalHarnessProcess)
    outcome = boundary.run_backtest(_request())

    assert runner.argv == ("harness", "backtest-job", "--config", "cfg/bt.yaml")
    assert outcome.exit_code == 0
    assert outcome.stdout == "{}"


def test_local_composition_falls_back_when_the_process_cannot_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise FileNotFoundError("harness executable not found")

    monkeypatch.setattr(mcp, "open_harness_mcp_client", _raise)
    runner = RecordingProcessRunner()

    boundary = open_local_harness_boundary(process_runner=runner)

    assert isinstance(boundary, LocalHarnessProcess)


def test_local_composition_propagates_without_a_fallback_runner() -> None:
    with pytest.raises(HarnessMcpTransportError):
        open_local_harness_boundary(
            topology=_local_topology(),
            stdio_transport=FailingTransport(),
        )


def test_remote_harness_failure_never_falls_back_to_local() -> None:
    def _handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    runner = RecordingProcessRunner()

    with pytest.raises(HarnessMcpTransportError):
        open_local_harness_boundary(
            topology=HarnessMcpTopology(
                harness_location="remote",
                harness_url=HARNESS_URL,
                bearer_token=BEARER,
            ),
            http_transport=httpx.MockTransport(_handler),
            process_runner=runner,
        )

    assert runner.argv is None
