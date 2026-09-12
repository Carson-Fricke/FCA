"""Harness MCP tool provider coverage for the Thesis agent.

These tests prove tool discovery and typed calls across the local in-process,
remote-Thesis/local-harness stdio seam, and remote HTTP topologies without
spawning a process or opening a socket.  They also prove that authentication,
transport, protocol, and tool failures surface as agent-visible values.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

import httpx
import pytest

from thesis.agent.harness_tools import (
    HARNESS_TOOL_NAMES,
    GetBacktestStatusArgs,
    GetRunArgs,
    HarnessToolError,
    HarnessToolProvider,
    LaunchTraderArgs,
    ListArtifactsArgs,
    ReadArtifactArgs,
    RunBacktestArgs,
    RunSweepArgs,
    harness_mcp_skill,
    open_harness_tool_provider,
)
from thesis.agent.skills import SkillRegistry
from thesis.integrations.mcp import (
    HARNESS_API_BEARER_TOKEN_VARIABLE,
    HARNESS_MCP_URL_VARIABLE,
    HarnessMcpTopology,
)

HARNESS_URL = "http://harness.test:9000"
BEARER = "harness-secret"

_TOOL_NAMES = {name: {"type": "object"} for name in HARNESS_TOOL_NAMES}


class FakeHarnessHandler:
    """In-process MCP handler publishing every harness tool."""

    def __init__(self, *, is_error: bool = False, error_message: str = "boom") -> None:
        self.calls: list[dict[str, object]] = []
        self._is_error = is_error
        self._error_message = error_message

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
                    {
                        "name": name,
                        "description": f"{name} tool",
                        "inputSchema": schema,
                    }
                    for name, schema in _TOOL_NAMES.items()
                ]
            }
        elif method == "tools/call":
            params = message.get("params")
            assert isinstance(params, dict)
            self.calls.append(params)
            if self._is_error:
                result = {
                    "isError": True,
                    "content": [{"type": "text", "text": self._error_message}],
                    "structuredContent": {"state": "failed"},
                }
            else:
                result = {
                    "isError": False,
                    "content": [{"type": "text", "text": "ok"}],
                    "structuredContent": {"jobId": "job-1", "tool": params.get("name")},
                }
        else:
            result = {}
        return {"jsonrpc": "2.0", "id": message.get("id"), "result": result}


class FakeTransport:
    """Stdio seam double dispatching against an in-process handler."""

    def __init__(self, handler: FakeHarnessHandler) -> None:
        self._handler = handler
        self.closed = False

    def exchange(self, request: Mapping[str, object]) -> Mapping[str, object]:
        response = self._handler.handle(dict(request))
        assert response is not None
        return response

    def notify(self, request: Mapping[str, object]) -> None:
        self._handler.handle(dict(request))

    def close(self) -> None:
        self.closed = True


class FakeHttpHarnessServer:
    """Minimal MCP-over-HTTP harness server, including failure modes."""

    def __init__(
        self,
        *,
        token: str | None = BEARER,
        require_token: bool = True,
        status_override: int | None = None,
        rpc_error: bool = False,
    ) -> None:
        self.token = token
        self.require_token = require_token
        self.status_override = status_override
        self.rpc_error = rpc_error
        self.seen: list[tuple[str, str, str | None, object]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        authorization = request.headers.get("Authorization")
        body = json.loads(request.content) if request.content else None
        self.seen.append((request.method, request.url.path, authorization, body))
        if self.require_token and authorization != f"Bearer {self.token}":
            return httpx.Response(401, json={"error": "unauthorized"})
        if request.method != "POST" or request.url.path != "/mcp":
            return httpx.Response(404, json={})
        if self.status_override is not None:
            return httpx.Response(self.status_override, json={})
        assert isinstance(body, dict)
        if "id" not in body:
            return httpx.Response(202)
        return httpx.Response(200, json=self._dispatch(body))

    def _dispatch(self, body: dict[str, object]) -> dict[str, object]:
        if self.rpc_error:
            return {
                "jsonrpc": "2.0",
                "id": body.get("id"),
                "error": {"code": -32603, "message": "harness exploded"},
            }
        method = body.get("method")
        if method == "initialize":
            result: dict[str, object] = {"protocolVersion": "2024-11-05"}
        elif method == "tools/list":
            result = {
                "tools": [
                    {"name": name, "description": "", "inputSchema": schema}
                    for name, schema in _TOOL_NAMES.items()
                ]
            }
        elif method == "tools/call":
            params = body["params"]
            assert isinstance(params, dict)
            result = {
                "isError": False,
                "content": [{"type": "text", "text": "ok"}],
                "structuredContent": {"jobId": "job-1", "tool": params.get("name")},
            }
        else:
            result = {}
        return {"jsonrpc": "2.0", "id": body.get("id"), "result": result}


def _in_process_provider(
    handler: FakeHarnessHandler | None = None,
) -> tuple[HarnessToolProvider, FakeHarnessHandler]:
    selected = FakeHarnessHandler() if handler is None else handler
    provider = open_harness_tool_provider(
        HarnessMcpTopology(thesis_location="local", harness_location="local"),
        in_process_handler=selected,
    )
    return provider, selected


def _remote_provider(
    server: FakeHttpHarnessServer, *, token: str | None = BEARER
) -> HarnessToolProvider:
    return open_harness_tool_provider(
        HarnessMcpTopology(
            harness_location="remote",
            harness_url=HARNESS_URL,
            bearer_token=token,
        ),
        http_transport=httpx.MockTransport(server.handler),
    )


def test_local_in_process_provider_discovers_and_calls_every_typed_tool() -> None:
    provider, handler = _in_process_provider()

    catalog = provider.discover()
    assert catalog.ok
    assert [tool.name for tool in catalog.tools] == list(HARNESS_TOOL_NAMES)
    assert catalog.tools[0].as_dict()["inputSchema"] == {"type": "object"}

    calls = (
        provider.run_backtest(
            RunBacktestArgs(config_path="cfg/bt.yaml", workspace="ws")
        ),
        provider.backtest_config_template(),
        provider.get_backtest_status(GetBacktestStatusArgs(job_id="job-1")),
        provider.get_run(GetRunArgs(run_id="run-1")),
        provider.list_artifacts(ListArtifactsArgs(run_id="run-1")),
        provider.read_artifact(ReadArtifactArgs(run_id="run-1", name="metrics.json")),
        provider.run_sweep(RunSweepArgs(spec_path="cfg/sweep.yaml")),
        provider.launch_trader(LaunchTraderArgs(config_path="cfg/trader.yaml")),
    )
    for result in calls:
        assert result.ok, result.failure
        assert result.structured["tool"] == result.tool
        assert result.tool in HARNESS_TOOL_NAMES

    names = [str(call["name"]) for call in handler.calls]
    assert sorted(names) == sorted(HARNESS_TOOL_NAMES)
    assert handler.calls[0]["arguments"] == {
        "config_path": "cfg/bt.yaml",
        "workspace": "ws",
    }


def test_remote_thesis_local_harness_uses_injected_stdio_transport() -> None:
    handler = FakeHarnessHandler()
    transport = FakeTransport(handler)
    provider = open_harness_tool_provider(
        HarnessMcpTopology(thesis_location="remote", harness_location="local"),
        stdio_transport=transport,
    )

    assert provider.list_tools()
    result = provider.get_run(GetRunArgs(run_id="run-42"))

    assert result.ok
    assert handler.calls[-1] == {"name": "get_run", "arguments": {"run_id": "run-42"}}


def test_remote_http_provider_discovers_and_calls_tools() -> None:
    server = FakeHttpHarnessServer()
    provider = _remote_provider(server)

    assert {tool.name for tool in provider.list_tools()} == set(HARNESS_TOOL_NAMES)
    result = provider.launch_trader(LaunchTraderArgs(config_path="cfg/trader.yaml"))

    assert result.ok
    assert result.structured["jobId"] == "job-1"
    posted = [seen for seen in server.seen if seen[1] == "/mcp"]
    assert posted
    assert all(seen[2] == f"Bearer {BEARER}" for seen in posted)
    payloads = [seen[3] for seen in posted if isinstance(seen[3], dict)]
    call = next(body for body in payloads if body.get("method") == "tools/call")
    assert call["params"] == {
        "name": "launch_trader",
        "arguments": {"config_path": "cfg/trader.yaml"},
    }


def test_environment_topology_selects_remote_without_agent_branching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(HARNESS_MCP_URL_VARIABLE, HARNESS_URL)
    monkeypatch.setenv(HARNESS_API_BEARER_TOKEN_VARIABLE, BEARER)
    server = FakeHttpHarnessServer()

    provider = open_harness_tool_provider(
        http_transport=httpx.MockTransport(server.handler)
    )

    assert [tool.name for tool in provider.list_tools()] == list(HARNESS_TOOL_NAMES)


def test_auth_failure_is_agent_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(HARNESS_API_BEARER_TOKEN_VARIABLE, raising=False)
    server = FakeHttpHarnessServer(require_token=False)
    provider = _remote_provider(server, token=None)

    catalog = provider.discover()
    result = provider.run_backtest(RunBacktestArgs(config_path="cfg.yaml"))

    assert catalog.ok is False
    assert catalog.failure is not None
    assert catalog.failure.kind == "auth"
    assert result.ok is False
    assert result.failure is not None
    assert result.failure.kind == "auth"
    assert result.is_error
    assert server.seen == []


def test_transport_failure_is_agent_visible() -> None:
    provider = _remote_provider(FakeHttpHarnessServer(status_override=500))

    result = provider.get_backtest_status(GetBacktestStatusArgs(job_id="job-1"))

    assert result.failure is not None
    assert result.failure.kind == "transport"
    assert result.failure.status_code == 500


def test_protocol_failure_is_agent_visible() -> None:
    provider = _remote_provider(FakeHttpHarnessServer(rpc_error=True))

    result = provider.get_run(GetRunArgs(run_id="run-1"))

    assert result.failure is not None
    assert result.failure.kind == "protocol"


def test_tool_error_result_is_agent_visible() -> None:
    provider, _ = _in_process_provider(
        FakeHarnessHandler(is_error=True, error_message="job rejected")
    )

    result = provider.run_sweep(RunSweepArgs(spec_path="cfg/sweep.yaml"))

    assert result.ok is False
    assert result.failure is not None
    assert result.failure.kind == "tool"
    assert result.failure.message == "job rejected"
    assert result.as_dict()["structured"] == {"state": "failed"}


def test_unknown_tool_reports_failure_before_the_transport() -> None:
    provider, handler = _in_process_provider()

    result = provider.call_tool("delete_everything")

    assert result.ok is False
    assert result.failure is not None
    assert result.failure.kind == "unknown_tool"
    assert handler.calls == []


def test_list_tools_raises_typed_error_on_discovery_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(HARNESS_API_BEARER_TOKEN_VARIABLE, raising=False)
    provider = _remote_provider(FakeHttpHarnessServer(require_token=False), token=None)

    with pytest.raises(HarnessToolError) as caught:
        provider.list_tools()

    assert caught.value.failure.kind == "auth"


def test_skill_declares_harness_tools_and_registers() -> None:
    skill = harness_mcp_skill()

    assert skill.skill_id == "harness-mcp"
    assert skill.allowed_tools == tuple(sorted(HARNESS_TOOL_NAMES))
    registry = SkillRegistry((skill,))
    assert registry.resolve("harness-mcp").content_hash == skill.content_hash


def test_typed_arguments_serialize_and_reject_empty_values() -> None:
    assert RunBacktestArgs("cfg.yaml").to_arguments() == {"config_path": "cfg.yaml"}
    assert RunBacktestArgs("cfg.yaml", workspace="ws").to_arguments() == {
        "config_path": "cfg.yaml",
        "workspace": "ws",
    }
    assert GetBacktestStatusArgs("job-1").to_arguments() == {"job_id": "job-1"}
    assert GetRunArgs("run-1").to_arguments() == {"run_id": "run-1"}
    assert ListArtifactsArgs("run-1").to_arguments() == {"run_id": "run-1"}
    assert RunSweepArgs("sweep.yaml").to_arguments() == {"spec_path": "sweep.yaml"}
    assert LaunchTraderArgs("trader.yaml").to_arguments() == {
        "config_path": "trader.yaml"
    }

    with pytest.raises(ValueError, match="config_path"):
        RunBacktestArgs("  ")
    with pytest.raises(ValueError, match="job_id"):
        GetBacktestStatusArgs("")
