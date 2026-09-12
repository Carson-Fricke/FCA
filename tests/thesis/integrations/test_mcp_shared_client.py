"""Shared, topology-aware harness MCP client composition coverage.

These tests prove the local and remote harnesses run through one mechanism:
:func:`open_harness_mcp_composition` opens exactly one
:class:`~thesis.integrations.mcp.HarnessMcpClient` (local stdio/in-process or
remote ``POST /mcp``), initializes it once, and shares it between the backtest
boundary and the chat tool provider.  They also prove the bounded retry for a
flaky first probe and that tools are disabled only after retries are exhausted.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import httpx
import pytest

from thesis.agent.harness_tools import (
    HARNESS_TOOL_NAMES,
    GetRunArgs,
    HarnessMcpComposition,
    open_harness_mcp_composition,
    open_harness_tool_provider,
)
from thesis.integrations import mcp
from thesis.integrations.harness_activation import HarnessActivationRequest
from thesis.integrations.mcp import (
    HarnessMcpClient,
    HarnessMcpTopology,
    HarnessMcpTransportError,
    InProcessMcpTransport,
)

HARNESS_URL = "http://harness.test:9000"
BEARER = "harness-secret"


class CatalogHandler:
    """In-process MCP handler that counts the initialize/list/call traffic."""

    def __init__(self) -> None:
        self.initialize_count = 0
        self.list_count = 0
        self.calls: list[dict[str, object]] = []

    def handle(self, request: object) -> dict[str, object] | None:
        if not isinstance(request, Mapping):
            raise TypeError("request must be a mapping")
        message = dict(request)
        method = message.get("method")
        if method == "initialize":
            self.initialize_count += 1
        elif method == "tools/list":
            self.list_count += 1
        if "id" not in message:
            return None
        if method == "initialize":
            result: dict[str, object] = {"protocolVersion": "2024-11-05"}
        elif method == "tools/list":
            result = {
                "tools": [
                    {"name": name, "description": "", "inputSchema": {"type": "object"}}
                    for name in HARNESS_TOOL_NAMES
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


class FlakyTransport:
    """Stdio seam that fails the first exchange, then delegates."""

    def __init__(self, handler: CatalogHandler, *, failures: int = 1) -> None:
        self._handler = handler
        self._remaining = failures
        self.closed = False

    def exchange(self, request: Mapping[str, object]) -> Mapping[str, object]:
        if self._remaining > 0:
            self._remaining -= 1
            raise HarnessMcpTransportError(
                "the harness MCP server closed the stream before answering"
            )
        response = self._handler.handle(dict(request))
        assert response is not None
        return response

    def notify(self, request: Mapping[str, object]) -> None:
        self._handler.handle(dict(request))

    def close(self) -> None:
        self.closed = True


class FailingTransport:
    """Stdio seam that always fails and records how many probes it saw."""

    def __init__(self) -> None:
        self.exchanges = 0
        self.closed = False

    def exchange(self, request: Mapping[str, object]) -> Mapping[str, object]:
        del request
        self.exchanges += 1
        raise HarnessMcpTransportError("the harness MCP server is unreachable")

    def notify(self, request: Mapping[str, object]) -> None:
        del request

    def close(self) -> None:
        self.closed = True


class FakeHttpMcpServer:
    """Minimal remote ``POST /mcp`` server that counts initialize traffic."""

    def __init__(self) -> None:
        self.initialize_count = 0
        self.seen: list[tuple[str, str, str | None, object]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        authorization = request.headers.get("Authorization")
        body = json.loads(request.content) if request.content else None
        self.seen.append((request.method, request.url.path, authorization, body))
        if authorization != f"Bearer {BEARER}":
            return httpx.Response(401, json={"error": "unauthorized"})
        if request.method != "POST" or request.url.path != "/mcp":
            return httpx.Response(404, json={})
        assert isinstance(body, dict)
        if "id" not in body:
            return httpx.Response(202)
        return httpx.Response(200, json=self._dispatch(body))

    def _dispatch(self, body: dict[str, object]) -> dict[str, object]:
        method = body.get("method")
        if method == "initialize":
            self.initialize_count += 1
            result: dict[str, object] = {"protocolVersion": "2024-11-05"}
        elif method == "tools/list":
            result = {
                "tools": [
                    {"name": name, "description": "", "inputSchema": {"type": "object"}}
                    for name in HARNESS_TOOL_NAMES
                ]
            }
        elif method == "tools/call":
            params = body["params"]
            assert isinstance(params, dict)
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
        return {"jsonrpc": "2.0", "id": body.get("id"), "result": result}


def _local_topology() -> HarnessMcpTopology:
    return HarnessMcpTopology(thesis_location="local", harness_location="local")


def _remote_topology() -> HarnessMcpTopology:
    return HarnessMcpTopology(
        thesis_location="local",
        harness_location="remote",
        harness_url=HARNESS_URL,
        bearer_token=BEARER,
    )


def _request() -> HarnessActivationRequest:
    return HarnessActivationRequest(
        bundle_ref="b" * 64, config_path="cfg/bt.yaml", action="backtest"
    )


def test_local_composition_opens_one_client_shared_by_boundary_and_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handler = CatalogHandler()
    created: list[HarnessMcpClient] = []
    real = mcp.open_harness_mcp_client

    def recording(topology: HarnessMcpTopology, **kwargs: Any) -> HarnessMcpClient:
        client = real(topology, **kwargs)
        created.append(client)
        return client

    monkeypatch.setattr(mcp, "open_harness_mcp_client", recording)

    composition = open_harness_mcp_composition(
        _local_topology(), in_process_handler=handler
    )
    provider = composition.tool_provider()

    assert isinstance(composition, HarnessMcpComposition)
    assert len(created) == 1
    assert composition.client is created[0]
    assert composition.boundary.client is composition.client
    assert provider.client is composition.client
    assert handler.initialize_count == 1

    assert set(composition.tool_names) == set(HARNESS_TOOL_NAMES)
    assert {spec.name for spec in provider.list_tools()} == set(HARNESS_TOOL_NAMES)

    outcome = composition.boundary.run_backtest(_request())
    assert outcome.exit_code == 0
    assert handler.calls[-1]["name"] == "run_backtest"

    result = provider.get_run(GetRunArgs(run_id="run-1"))
    assert result.ok
    assert handler.calls[-1] == {"name": "get_run", "arguments": {"run_id": "run-1"}}


def test_remote_composition_shares_one_client_over_http() -> None:
    server = FakeHttpMcpServer()

    composition = open_harness_mcp_composition(
        _remote_topology(),
        http_transport=httpx.MockTransport(server.handler),
    )
    provider = composition.tool_provider()

    assert composition.boundary.capabilities.backtest is True
    assert {spec.name for spec in provider.list_tools()} == set(HARNESS_TOOL_NAMES)
    assert server.initialize_count == 1

    result = provider.get_run(GetRunArgs(run_id="run-1"))

    assert result.ok
    posted = [seen for seen in server.seen if seen[0] == "POST" and seen[1] == "/mcp"]
    assert posted
    assert all(seen[2] == f"Bearer {BEARER}" for seen in posted)
    payloads = [seen[3] for seen in posted if isinstance(seen[3], dict)]
    call = next(body for body in payloads if body.get("method") == "tools/call")
    assert call["params"] == {
        "name": "get_run",
        "arguments": {"run_id": "run-1"},
    }


def test_composition_retries_a_flaky_first_probe_then_succeeds() -> None:
    handler = CatalogHandler()
    transport = FlakyTransport(handler, failures=1)
    sleeps: list[float] = []

    composition = open_harness_mcp_composition(
        _local_topology(),
        stdio_transport=transport,
        attempts=3,
        backoff_seconds=0.25,
        sleep=sleeps.append,
    )

    assert set(composition.tool_names) == set(HARNESS_TOOL_NAMES)
    assert handler.initialize_count == 1
    assert sleeps == [0.25]


def test_composition_disables_tools_only_after_retries_exhausted() -> None:
    transport = FailingTransport()
    sleeps: list[float] = []

    with pytest.raises(HarnessMcpTransportError):
        open_harness_mcp_composition(
            _local_topology(),
            stdio_transport=transport,
            attempts=3,
            backoff_seconds=0.1,
            sleep=sleeps.append,
        )

    assert transport.exchanges == 3
    assert sleeps == [0.1, 0.2]
    assert transport.closed is True


def test_client_initialize_is_idempotent() -> None:
    handler = CatalogHandler()
    client = HarnessMcpClient(InProcessMcpTransport(handler))

    first = client.initialize()
    second = client.initialize()

    assert first == second
    assert client.initialized is True
    assert handler.initialize_count == 1


def test_shared_client_provider_rejects_competing_seams() -> None:
    client = HarnessMcpClient(InProcessMcpTransport(CatalogHandler()))

    with pytest.raises(ValueError, match="either a client or topology"):
        open_harness_tool_provider(_local_topology(), client=client)
