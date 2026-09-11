"""Thesis client for the quant-harness MCP server.

The harness exposes backtests, sweeps, run queries, artifacts, and the
supervised trader as MCP tools over newline-delimited JSON-RPC 2.0 on stdio
(``harness mcp``).  This module is the consumer side: a small client plus a
:class:`~thesis.integrations.harness_activation.HarnessBoundary` implementation
so Thesis can launch and query backtests through the MCP tool catalog instead
of hand-rolled HTTP or subprocess plumbing.

Two transports compose the client: :class:`StdioMcpTransport` spawns the real
``harness mcp`` process, and :class:`InProcessMcpTransport` dispatches against
an in-process server for tests and single-process runtimes.  The MCP server
resolves the harness API bearer from its own ``HARNESS_API_BEARER_TOKEN``
environment, so no credential is ever passed per call.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import count
from typing import Protocol

from thesis.integrations.harness_activation import (
    ActivationOutcome,
    HarnessActivationError,
    HarnessActivationRequest,
    HarnessCapabilities,
)

PROTOCOL_VERSION = "2024-11-05"
CLIENT_NAME = "thesis-mcp-client"
CLIENT_VERSION = "0.1.0"

TOOL_RUN_BACKTEST = "run_backtest"
TOOL_LAUNCH_TRADER = "launch_trader"


class HarnessMcpError(RuntimeError):
    """Base error for a failed or misused harness MCP client call."""


class HarnessMcpProtocolError(HarnessMcpError):
    """The MCP server returned a malformed or error response."""


class HarnessMcpToolError(HarnessMcpError):
    """A tool call returned ``isError`` without an observed process outcome."""

    def __init__(
        self,
        tool: str,
        message: str,
        payload: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(f"harness MCP tool {tool!r} failed: {message}")
        self.tool = tool
        self.payload = None if payload is None else dict(payload)


@dataclass(frozen=True, slots=True)
class McpTool:
    """One published tool from ``tools/list``."""

    name: str
    description: str
    input_schema: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class McpToolResult:
    """One ``tools/call`` result, including domain failures."""

    tool: str
    is_error: bool
    structured: Mapping[str, object] = field(default_factory=dict)
    text: str = ""


class McpTransport(Protocol):
    """JSON-RPC exchange boundary for the client."""

    def exchange(self, request: Mapping[str, object]) -> Mapping[str, object]: ...

    def notify(self, request: Mapping[str, object]) -> None: ...

    def close(self) -> None: ...


class _JsonRpcHandler(Protocol):
    """The dispatch surface an in-process MCP server exposes."""

    def handle(self, request: object) -> dict[str, object] | None: ...


class InProcessMcpTransport:
    """Dispatch decoded requests against an in-process MCP handler."""

    def __init__(self, handler: _JsonRpcHandler) -> None:
        self._handler = handler

    def exchange(self, request: Mapping[str, object]) -> Mapping[str, object]:
        response = self._handler.handle(dict(request))
        if response is None:
            raise HarnessMcpProtocolError("the MCP handler produced no response")
        return dict(response)

    def notify(self, request: Mapping[str, object]) -> None:
        self._handler.handle(dict(request))

    def close(self) -> None:
        return None


class StdioMcpTransport:
    """Spawn ``harness mcp`` and exchange newline-delimited JSON-RPC over stdio."""

    def __init__(
        self,
        argv: Sequence[str] = ("harness", "mcp"),
        *,
        environ: Mapping[str, str] | None = None,
        cwd: str | None = None,
    ) -> None:
        program = tuple(argv)
        if not program or any(
            not isinstance(part, str) or not part.strip() for part in program
        ):
            raise ValueError("argv must be a non-empty sequence of non-empty strings")
        self._argv = program
        self._process = subprocess.Popen(
            program,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            cwd=cwd,
            env=dict(os.environ if environ is None else environ),
        )

    def exchange(self, request: Mapping[str, object]) -> Mapping[str, object]:
        self._write(request)
        stdout = self._process.stdout
        if stdout is None:
            raise HarnessMcpProtocolError("the harness MCP stdout stream is closed")
        line = stdout.readline()
        if not line:
            raise HarnessMcpProtocolError(
                "the harness MCP server closed the stream before answering"
            )
        try:
            decoded = json.loads(line)
        except ValueError as error:
            raise HarnessMcpProtocolError(
                "the harness MCP server returned a non-JSON line"
            ) from error
        if not isinstance(decoded, Mapping):
            raise HarnessMcpProtocolError(
                "the harness MCP server returned a non-object response"
            )
        return dict(decoded)

    def notify(self, request: Mapping[str, object]) -> None:
        self._write(request)

    def _write(self, request: Mapping[str, object]) -> None:
        stdin = self._process.stdin
        if stdin is None:
            raise HarnessMcpProtocolError("the harness MCP stdin stream is closed")
        stdin.write(
            json.dumps(dict(request), separators=(",", ":"), ensure_ascii=True) + "\n"
        )
        stdin.flush()

    def close(self) -> None:
        if self._process.poll() is None:
            try:
                if self._process.stdin is not None:
                    self._process.stdin.close()
                self._process.wait(timeout=5)
            except Exception:
                self._process.kill()


class HarnessMcpClient:
    """Discover and call quant-harness MCP tools over an injected transport."""

    def __init__(
        self,
        transport: McpTransport,
        *,
        id_factory: Callable[[], int] | None = None,
    ) -> None:
        self._transport = transport
        if id_factory is not None:
            self._next_id = id_factory
        else:
            counter = count(1)
            self._next_id = lambda: next(counter)  # noqa: E731
        self._server: Mapping[str, object] = {}

    @property
    def server_info(self) -> Mapping[str, object]:
        """Return the ``initialize`` server info, or empty before initialize."""
        return self._server

    def initialize(self) -> Mapping[str, object]:
        """Negotiate the protocol and acknowledge the initialization."""
        result = self._request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
            },
        )
        self._server = dict(result)
        self._transport.notify(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
        return result

    def list_tools(self) -> tuple[McpTool, ...]:
        """Return the server's published tool catalog."""
        result = self._request("tools/list", {})
        raw = result.get("tools")
        tools: list[McpTool] = []
        if isinstance(raw, (list, tuple)):
            for entry in raw:
                if not isinstance(entry, Mapping):
                    continue
                name = entry.get("name")
                schema = entry.get("inputSchema")
                if not isinstance(name, str) or not name:
                    continue
                tools.append(
                    McpTool(
                        name=name,
                        description=str(entry.get("description", "")),
                        input_schema=dict(schema)
                        if isinstance(schema, Mapping)
                        else {},
                    )
                )
        return tuple(tools)

    def call_tool(
        self, name: str, arguments: Mapping[str, object] | None = None
    ) -> McpToolResult:
        """Call one tool and return its result, including domain errors."""
        result = self._request(
            "tools/call",
            {"name": name, "arguments": dict(arguments or {})},
        )
        structured = result.get("structuredContent")
        payload = dict(structured) if isinstance(structured, Mapping) else {}
        return McpToolResult(
            tool=name,
            is_error=bool(result.get("isError")),
            structured=payload,
            text=_content_text(result),
        )

    def run_backtest(
        self, *, config_path: str, workspace: str | None = None
    ) -> McpToolResult:
        """Call the ``run_backtest`` tool."""
        arguments: dict[str, object] = {"config_path": config_path}
        if workspace is not None:
            arguments["workspace"] = workspace
        return self.call_tool(TOOL_RUN_BACKTEST, arguments)

    def launch_trader(self, *, config_path: str) -> McpToolResult:
        """Call the ``launch_trader`` tool."""
        return self.call_tool(TOOL_LAUNCH_TRADER, {"config_path": config_path})

    def close(self) -> None:
        """Close the underlying transport."""
        self._transport.close()

    def _request(
        self, method: str, params: Mapping[str, object]
    ) -> Mapping[str, object]:
        response = self._transport.exchange(
            {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": method,
                "params": dict(params),
            }
        )
        error = response.get("error")
        if isinstance(error, Mapping):
            raise HarnessMcpProtocolError(
                f"harness MCP {method} failed: {error.get('message', 'unknown error')}"
            )
        result = response.get("result")
        if not isinstance(result, Mapping):
            raise HarnessMcpProtocolError(
                f"harness MCP {method} returned no result object"
            )
        return result


def _content_text(result: Mapping[str, object]) -> str:
    content = result.get("content")
    if not isinstance(content, (list, tuple)):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, Mapping) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "\n".join(parts)


@dataclass(frozen=True, slots=True)
class HarnessMcpBoundary:
    """A :class:`HarnessBoundary` backed by the harness MCP tool catalog."""

    client: HarnessMcpClient
    _capabilities: HarnessCapabilities

    @classmethod
    def connect(cls, client: HarnessMcpClient) -> HarnessMcpBoundary:
        """Initialize the client and derive capabilities from its tool catalog."""
        client.initialize()
        names = {tool.name for tool in client.list_tools()}
        return cls(
            client=client,
            _capabilities=HarnessCapabilities(
                backtest=TOOL_RUN_BACKTEST in names,
                trader=TOOL_LAUNCH_TRADER in names,
                shadow=False,
                live=False,
            ),
        )

    @property
    def capabilities(self) -> HarnessCapabilities:
        return self._capabilities

    def run_backtest(
        self, request: HarnessActivationRequest, *, bearer_token: str | None = None
    ) -> ActivationOutcome:
        """Launch the requested backtest through the MCP boundary."""
        self._reject_credential(bearer_token)
        self._capabilities.require_capability("backtest")
        result = self.client.run_backtest(config_path=request.config_path)
        return self._outcome(request, result)

    def launch_trader(
        self, request: HarnessActivationRequest, *, bearer_token: str | None = None
    ) -> ActivationOutcome:
        """Launch the requested trader session through the MCP boundary."""
        self._reject_credential(bearer_token)
        self._capabilities.require_capability("trader")
        result = self.client.launch_trader(config_path=request.config_path)
        return self._outcome(request, result)

    def _outcome(
        self, request: HarnessActivationRequest, result: McpToolResult
    ) -> ActivationOutcome:
        payload = dict(result.structured)
        exit_code = payload.get("exit_code")
        observed_exit = (
            exit_code
            if isinstance(exit_code, int) and not isinstance(exit_code, bool)
            else None
        )
        # A non-zero local process exit is an observed harness outcome, not a
        # client failure; only an error without one is raised.
        if result.is_error and observed_exit is None:
            raise HarnessMcpToolError(
                result.tool, result.text or "unknown error", payload
            )
        return ActivationOutcome(
            action=request.action,
            bundle_ref=request.bundle_ref,
            environment=request.environment,
            request_payload=request.to_payload(),
            exit_code=observed_exit,
            job_id=_optional_str(payload.get("job_id")),
            state=_optional_str(payload.get("state")),
            stdout=_optional_str(payload.get("stdout")) or "",
            stderr=_optional_str(payload.get("stderr")) or "",
            raw=payload,
        )

    @staticmethod
    def _reject_credential(bearer_token: str | None) -> None:
        if bearer_token is not None:
            raise HarnessActivationError(
                "the harness MCP server resolves its own bearer credential from "
                "HARNESS_API_BEARER_TOKEN; Thesis never passes one per call"
            )


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


__all__ = [
    "CLIENT_NAME",
    "CLIENT_VERSION",
    "PROTOCOL_VERSION",
    "TOOL_LAUNCH_TRADER",
    "TOOL_RUN_BACKTEST",
    "HarnessMcpBoundary",
    "HarnessMcpClient",
    "HarnessMcpError",
    "HarnessMcpProtocolError",
    "HarnessMcpToolError",
    "InProcessMcpTransport",
    "McpTool",
    "McpToolResult",
    "McpTransport",
    "StdioMcpTransport",
]
