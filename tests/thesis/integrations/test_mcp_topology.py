"""Topology-aware harness MCP client coverage.

These tests exercise the four Thesis/harness deployment combinations, the
remote HTTP JSON-RPC transport, bearer resolution and redaction, and transport
error mapping.  They never spawn a process or open a socket: stdio selection is
observed through a monkeypatched transport and HTTP through ``httpx``'s mock
transport.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import cast

import httpx
import pytest

from thesis.integrations import mcp
from thesis.integrations.mcp import (
    HARNESS_API_BEARER_TOKEN_VARIABLE,
    HARNESS_MCP_COMMAND_VARIABLE,
    HARNESS_MCP_URL_VARIABLE,
    THESIS_LOCATION_VARIABLE,
    EnvironmentHarnessTokenSource,
    HarnessMcpAuthError,
    HarnessMcpClient,
    HarnessMcpProtocolError,
    HarnessMcpTopology,
    HarnessMcpTransportError,
    InProcessMcpTransport,
    McpLocation,
    RemoteHttpMcpTransport,
    StdioMcpTransport,
    harness_mcp_topology_from_env,
    open_harness_mcp_client,
)

HARNESS_URL = "http://harness.test:9000"
BEARER = "harness-secret"


class FakeMcpHandler:
    """In-process MCP dispatch used to observe transport selection."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def handle(self, request: object) -> dict[str, object] | None:
        if not isinstance(request, Mapping):
            raise TypeError("request must be a mapping")
        message = dict(request)
        self.calls.append(message)
        method = message.get("method")
        if "id" not in message:
            return None
        if method == "tools/list":
            result: dict[str, object] = {
                "tools": [
                    {"name": "run_backtest", "description": "", "inputSchema": {}}
                ]
            }
        elif method == "tools/call":
            params = message["params"]
            assert isinstance(params, dict)
            result = {
                "isError": False,
                "content": [],
                "structuredContent": {"echo": params["name"]},
            }
        else:
            result = {"serverInfo": {}}
        return {"jsonrpc": "2.0", "id": message.get("id"), "result": result}


class StubTransport:
    """A transport double used to observe stdio selection."""

    def __init__(self) -> None:
        self.closed = False

    def exchange(self, request: Mapping[str, object]) -> Mapping[str, object]:
        return {"jsonrpc": "2.0", "id": request.get("id"), "result": {}}

    def notify(self, request: Mapping[str, object]) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class FakeHttpMcpServer:
    """Minimal MCP-over-HTTP server for the ``POST /mcp`` contract."""

    def __init__(
        self,
        *,
        token: str | None = BEARER,
        require_token: bool = True,
        status_override: int | None = None,
    ) -> None:
        self.token = token
        self.require_token = require_token
        self.status_override = status_override
        self.seen: list[tuple[str, str, str | None, object]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        authorization = request.headers.get("Authorization")
        body = json.loads(request.content) if request.content else None
        self.seen.append((request.method, request.url.path, authorization, body))
        if self.require_token and authorization != f"Bearer {self.token}":
            return httpx.Response(401, json={"error": "unauthorized"})
        if request.method == "GET" and request.url.path == "/mcp/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.method == "POST" and request.url.path == "/mcp":
            if self.status_override is not None:
                return httpx.Response(self.status_override, json={})
            assert isinstance(body, dict)
            if "id" not in body:
                return httpx.Response(202)
            return httpx.Response(200, json=self._dispatch(body))
        return httpx.Response(404, json={})

    @staticmethod
    def _dispatch(body: dict[str, object]) -> dict[str, object]:
        method = body.get("method")
        if method == "initialize":
            result: dict[str, object] = {"protocolVersion": "2024-11-05"}
        elif method == "tools/list":
            result = {
                "tools": [
                    {"name": "run_backtest", "description": "", "inputSchema": {}}
                ]
            }
        elif method == "tools/call":
            params = body["params"]
            assert isinstance(params, dict)
            if params.get("name") == "unknown-tool":
                return {
                    "jsonrpc": "2.0",
                    "id": body.get("id"),
                    "error": {"code": -32602, "message": "unknown method"},
                }
            result = {
                "isError": False,
                "content": [{"type": "text", "text": "ok"}],
                "structuredContent": {"echo": params["name"]},
            }
        else:
            result = {}
        return {"jsonrpc": "2.0", "id": body.get("id"), "result": result}


def _remote_topology(
    thesis_location: McpLocation = "local", *, token: str | None = BEARER
) -> HarnessMcpTopology:
    return HarnessMcpTopology(
        thesis_location=thesis_location,
        harness_location="remote",
        harness_url=HARNESS_URL,
        bearer_token=token,
    )


def _remote_client(
    server: FakeHttpMcpServer,
    *,
    thesis_location: McpLocation = "local",
    token: str | None = BEARER,
) -> HarnessMcpClient:
    return open_harness_mcp_client(
        _remote_topology(thesis_location, token=token),
        http_transport=httpx.MockTransport(server.handler),
    )


def test_local_thesis_and_local_harness_prefers_in_process() -> None:
    handler = FakeMcpHandler()
    client = open_harness_mcp_client(
        HarnessMcpTopology(thesis_location="local", harness_location="local"),
        in_process_handler=handler,
    )

    assert isinstance(client.transport, InProcessMcpTransport)
    client.initialize()
    assert [tool.name for tool in client.list_tools()] == ["run_backtest"]


def test_local_thesis_and_local_harness_uses_the_configured_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: dict[str, tuple[str, ...]] = {}

    class RecordingStdio:
        def __init__(self, argv: tuple[str, ...]) -> None:
            created["argv"] = tuple(argv)

    monkeypatch.setattr(mcp, "StdioMcpTransport", RecordingStdio)
    client = open_harness_mcp_client(
        HarnessMcpTopology(harness_command=("custom-harness", "mcp"))
    )

    assert isinstance(client.transport, RecordingStdio)
    assert created["argv"] == ("custom-harness", "mcp")


def test_remote_thesis_with_local_harness_uses_stdio_not_in_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: dict[str, tuple[str, ...]] = {}

    class RecordingStdio:
        def __init__(self, argv: tuple[str, ...]) -> None:
            created["argv"] = tuple(argv)

    monkeypatch.setattr(mcp, "StdioMcpTransport", RecordingStdio)
    client = open_harness_mcp_client(
        HarnessMcpTopology(thesis_location="remote", harness_location="local"),
        in_process_handler=FakeMcpHandler(),
    )

    assert isinstance(client.transport, RecordingStdio)
    assert created["argv"] == ("harness", "mcp")


@pytest.mark.parametrize("thesis_location", ["local", "remote"])
def test_remote_harness_selects_http_for_every_thesis_location(
    thesis_location: str,
) -> None:
    server = FakeHttpMcpServer()
    client = _remote_client(server, thesis_location=cast(McpLocation, thesis_location))

    assert isinstance(client.transport, RemoteHttpMcpTransport)
    assert not isinstance(client.transport, StdioMcpTransport)


def test_remote_harness_wins_over_injected_local_seams() -> None:
    server = FakeHttpMcpServer()
    client = open_harness_mcp_client(
        _remote_topology(),
        in_process_handler=FakeMcpHandler(),
        stdio_transport=StubTransport(),
        http_transport=httpx.MockTransport(server.handler),
    )

    assert isinstance(client.transport, RemoteHttpMcpTransport)


def test_remote_http_round_trip_initializes_lists_and_calls_tools() -> None:
    server = FakeHttpMcpServer()
    client = _remote_client(server)

    initialized = client.initialize()
    tools = client.list_tools()
    result = client.call_tool("run_backtest", {"config_path": "config.json"})

    assert initialized["protocolVersion"] == "2024-11-05"
    assert [tool.name for tool in tools] == ["run_backtest"]
    assert result.is_error is False
    assert result.structured["echo"] == "run_backtest"
    assert result.text == "ok"
    assert {call[1] for call in server.seen} == {"/mcp"}
    assert all(auth == f"Bearer {BEARER}" for _, _, auth, _ in server.seen)
    client.close()


def test_remote_transport_health_probe_uses_the_health_route() -> None:
    server = FakeHttpMcpServer()
    transport = RemoteHttpMcpTransport(
        HARNESS_URL,
        bearer_token=BEARER,
        transport=httpx.MockTransport(server.handler),
    )

    assert transport.health() == {"status": "ok"}
    assert server.seen[-1][:3] == ("GET", "/mcp/health", f"Bearer {BEARER}")
    transport.close()


def test_bearer_precedence_prefers_explicit_then_source_then_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(HARNESS_API_BEARER_TOKEN_VARIABLE, "env-token")
    server = FakeHttpMcpServer(require_token=False)

    explicit = RemoteHttpMcpTransport(
        HARNESS_URL,
        bearer_token="explicit-token",
        bearer_source=EnvironmentHarnessTokenSource(),
        transport=httpx.MockTransport(server.handler),
    )
    explicit.exchange({"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert server.seen[-1][2] == "Bearer explicit-token"

    environment = RemoteHttpMcpTransport(
        HARNESS_URL, transport=httpx.MockTransport(server.handler)
    )
    environment.exchange({"jsonrpc": "2.0", "id": 2, "method": "ping"})
    assert server.seen[-1][2] == "Bearer env-token"

    monkeypatch.setenv(HARNESS_API_BEARER_TOKEN_VARIABLE, "source-token")
    sourced = RemoteHttpMcpTransport(
        HARNESS_URL,
        bearer_source=EnvironmentHarnessTokenSource(),
        transport=httpx.MockTransport(server.handler),
    )
    sourced.exchange({"jsonrpc": "2.0", "id": 3, "method": "ping"})
    assert server.seen[-1][2] == "Bearer source-token"


def test_remote_transport_requires_a_bearer_before_exchanging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(HARNESS_API_BEARER_TOKEN_VARIABLE, raising=False)
    server = FakeHttpMcpServer()
    transport = RemoteHttpMcpTransport(
        HARNESS_URL, transport=httpx.MockTransport(server.handler)
    )

    with pytest.raises(HarnessMcpAuthError):
        transport.exchange({"jsonrpc": "2.0", "id": 1, "method": "ping"})

    assert server.seen == []


def test_remote_topology_without_a_token_fails_closed_on_first_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(HARNESS_API_BEARER_TOKEN_VARIABLE, raising=False)
    server = FakeHttpMcpServer()
    client = _remote_client(server, token=None)

    with pytest.raises(HarnessMcpAuthError):
        client.initialize()


def test_bearer_token_is_redacted_from_every_representation() -> None:
    topology = _remote_topology()
    transport = RemoteHttpMcpTransport(HARNESS_URL, bearer_token=BEARER)
    error = HarnessMcpAuthError("the harness MCP endpoint rejected the credential")

    assert BEARER not in repr(topology)
    assert BEARER not in repr(transport)
    assert BEARER not in str(error)
    transport.close()


@pytest.mark.parametrize("status", [401, 403])
def test_http_auth_status_maps_to_auth_error(status: int) -> None:
    server = FakeHttpMcpServer(status_override=status)
    transport = RemoteHttpMcpTransport(
        HARNESS_URL,
        bearer_token=BEARER,
        transport=httpx.MockTransport(server.handler),
    )

    with pytest.raises(HarnessMcpAuthError) as caught:
        transport.exchange({"jsonrpc": "2.0", "id": 1, "method": "ping"})

    assert caught.value.status_code == status
    transport.close()


def test_http_server_error_maps_to_transport_error() -> None:
    server = FakeHttpMcpServer(status_override=500)
    transport = RemoteHttpMcpTransport(
        HARNESS_URL,
        bearer_token=BEARER,
        transport=httpx.MockTransport(server.handler),
    )

    with pytest.raises(HarnessMcpTransportError) as caught:
        transport.exchange({"jsonrpc": "2.0", "id": 1, "method": "ping"})

    assert caught.value.status_code == 500
    assert not isinstance(caught.value, HarnessMcpAuthError)
    transport.close()


def test_unreachable_endpoint_maps_to_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    transport = RemoteHttpMcpTransport(
        HARNESS_URL,
        bearer_token=BEARER,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(HarnessMcpTransportError):
        transport.exchange({"jsonrpc": "2.0", "id": 1, "method": "ping"})
    transport.close()


def test_non_json_and_non_object_bodies_map_to_protocol_error() -> None:
    def text_handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, text="not json")

    def array_handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json=[1, 2, 3])

    for handler in (text_handler, array_handler):
        transport = RemoteHttpMcpTransport(
            HARNESS_URL,
            bearer_token=BEARER,
            transport=httpx.MockTransport(handler),
        )
        with pytest.raises(HarnessMcpProtocolError):
            transport.exchange({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        transport.close()


def test_json_rpc_error_object_maps_to_protocol_error() -> None:
    server = FakeHttpMcpServer()
    client = _remote_client(server)

    with pytest.raises(HarnessMcpProtocolError, match="unknown method"):
        client.call_tool("unknown-tool")

    client.close()


def test_notifications_post_without_requiring_a_response_body() -> None:
    server = FakeHttpMcpServer()
    transport = RemoteHttpMcpTransport(
        HARNESS_URL,
        bearer_token=BEARER,
        transport=httpx.MockTransport(server.handler),
    )

    transport.notify({"jsonrpc": "2.0", "method": "notifications/initialized"})

    assert server.seen[-1][0] == "POST"
    assert server.seen[-1][1] == "/mcp"
    transport.close()


def test_config_resolution_prefers_url_and_parses_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        HARNESS_MCP_URL_VARIABLE,
        HARNESS_MCP_COMMAND_VARIABLE,
        HARNESS_API_BEARER_TOKEN_VARIABLE,
        THESIS_LOCATION_VARIABLE,
    ):
        monkeypatch.delenv(name, raising=False)

    from_url = harness_mcp_topology_from_env(
        {
            HARNESS_MCP_URL_VARIABLE: "https://remote-harness.test",
            HARNESS_API_BEARER_TOKEN_VARIABLE: "token-from-env",
            HARNESS_MCP_COMMAND_VARIABLE: "ignored mcp",
        }
    )
    assert from_url.harness_location == "remote"
    assert from_url.harness_url == "https://remote-harness.test"
    assert from_url.resolved_bearer_token() == "token-from-env"

    from_command = harness_mcp_topology_from_env(
        {
            HARNESS_MCP_COMMAND_VARIABLE: '"/opt/harness" mcp --verbose',
            THESIS_LOCATION_VARIABLE: "remote",
        }
    )
    assert from_command.harness_location == "local"
    assert from_command.thesis_location == "remote"
    assert from_command.harness_command == ("/opt/harness", "mcp", "--verbose")
    assert from_command.resolved_bearer_token() is None


def test_config_resolution_defaults_to_local_command_and_rejects_bad_values() -> None:
    empty = harness_mcp_topology_from_env({})
    assert empty.harness_location == "local"
    assert empty.thesis_location == "local"
    assert empty.harness_command == ("harness", "mcp")

    with pytest.raises(ValueError, match="local' or 'remote"):
        harness_mcp_topology_from_env({THESIS_LOCATION_VARIABLE: "somewhere"})

    with pytest.raises(ValueError, match="service URL"):
        HarnessMcpTopology(
            harness_location="remote", harness_url="https://user:pw@harness.test"
        )

    with pytest.raises(ValueError, match="harness_url is required"):
        HarnessMcpTopology(harness_location="remote")

    with pytest.raises(ValueError, match="harness_command"):
        HarnessMcpTopology(harness_command=("harness", " "))
