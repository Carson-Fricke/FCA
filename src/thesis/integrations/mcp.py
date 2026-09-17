"""Thesis client for the quant-harness MCP server.

The harness exposes backtests, sweeps, run queries, artifacts, and the
supervised trader as MCP tools over JSON-RPC 2.0.  This module is the consumer
side: a small client plus a
:class:`~thesis.integrations.harness_activation.HarnessBoundary` implementation
so Thesis can launch and query backtests through the MCP tool catalog instead
of hand-rolled HTTP or subprocess plumbing.

Three transports compose the client and cover every Thesis/harness topology:

* :class:`InProcessMcpTransport` dispatches against an in-process server for
  single-process runtimes (harness local, Thesis local).
* :class:`StdioMcpTransport` spawns the local ``harness mcp`` process and
  exchanges newline-delimited JSON-RPC over stdio (harness local).
* :class:`RemoteHttpMcpTransport` posts JSON-RPC to the harness HTTP MCP
  endpoint at ``POST /mcp`` with a bearer credential (harness remote).

:class:`HarnessMcpTopology` names the deployment locations and
:func:`open_harness_mcp_client` selects the transport.  Configuration resolves
from ``HARNESS_MCP_URL``, ``HARNESS_MCP_COMMAND``, and
``HARNESS_API_BEARER_TOKEN`` (explicit topology fields take precedence).  A
bearer credential is only ever sent as an ``Authorization`` header on a remote
exchange and is redacted from every representation.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import count
from typing import Literal, Protocol, cast
from urllib.parse import urlsplit

import httpx

from thesis.integrations.harness_activation import (
    ActivationOutcome,
    HarnessActivationError,
    HarnessActivationRequest,
    HarnessBoundary,
    HarnessCapabilities,
    HarnessProcessRunner,
    LocalHarnessProcess,
)

PROTOCOL_VERSION = "2024-11-05"
CLIENT_NAME = "thesis-mcp-client"
CLIENT_VERSION = "0.1.0"

#: Deployment variable naming the remote harness HTTP MCP endpoint.
HARNESS_MCP_URL_VARIABLE = "HARNESS_MCP_URL"
#: Deployment variable naming the local ``harness mcp`` command.
HARNESS_MCP_COMMAND_VARIABLE = "HARNESS_MCP_COMMAND"
#: Deployment variable holding the bearer credential for the harness boundary.
HARNESS_API_BEARER_TOKEN_VARIABLE = "HARNESS_API_BEARER_TOKEN"
#: Deployment variable naming where Thesis itself runs (``local`` or ``remote``).
THESIS_LOCATION_VARIABLE = "THESIS_LOCATION"

#: The remote harness MCP JSON-RPC route.
MCP_HTTP_ROUTE = "mcp"
#: The remote harness MCP liveness route.
MCP_HEALTH_ROUTE = "mcp/health"
#: Default local process command for the harness MCP server.
DEFAULT_HARNESS_MCP_COMMAND = ("harness", "mcp")
#: Default timeout applied to every remote harness MCP HTTP exchange.
DEFAULT_HTTP_TIMEOUT = 30.0
#: Default number of initialize/list_tools probe attempts before giving up.
DEFAULT_PROBE_ATTEMPTS = 3
#: Default base delay between probe attempts, in seconds.
DEFAULT_PROBE_BACKOFF_SECONDS = 0.25

McpLocation = Literal["local", "remote"]

TOOL_RUN_BACKTEST = "run_backtest"
TOOL_BACKTEST_CONFIG_TEMPLATE = "backtest_config_template"
TOOL_GET_BACKTEST_STATUS = "get_backtest_status"
TOOL_GET_RUN = "get_run"
TOOL_LIST_ARTIFACTS = "list_artifacts"
TOOL_READ_ARTIFACT = "read_artifact"
TOOL_RUN_SWEEP = "run_sweep"
TOOL_RUN_STUDY = "run_study"
TOOL_LAUNCH_TRADER = "launch_trader"


class HarnessMcpError(RuntimeError):
    """Base error for a failed or misused harness MCP client call."""


class HarnessMcpProtocolError(HarnessMcpError):
    """The MCP server returned a malformed or error response."""


class HarnessMcpTransportError(HarnessMcpError):
    """The MCP transport could not complete an exchange.

    Covers network failures, non-2xx HTTP responses, and non-JSON bodies on the
    remote HTTP transport.  The optional ``status_code`` is the HTTP status
    when one was observed, and never carries a credential.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class HarnessMcpAuthError(HarnessMcpTransportError):
    """The harness MCP endpoint rejected or received no bearer credential."""


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


class McpBearerSource(Protocol):
    """Resolve the bearer credential presented to a remote harness MCP endpoint."""

    def bearer_token(self) -> str | None: ...


class EnvironmentHarnessTokenSource:
    """Resolve the harness boundary bearer token from the process environment.

    The token is read on every call so a rotated deployment credential takes
    effect without rebuilding the transport, and it is never retained.
    """

    def __init__(self, variable: str = HARNESS_API_BEARER_TOKEN_VARIABLE) -> None:
        self._variable = variable

    def bearer_token(self) -> str | None:
        return _clean_text(os.environ.get(self._variable))


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


class RemoteHttpMcpTransport:
    """Exchange JSON-RPC with a remote harness MCP server over HTTP.

    Every request is a ``POST /mcp`` carrying one JSON-RPC message.  The bearer
    credential resolves per request from an explicit token, an injected
    :class:`McpBearerSource`, or the process environment, in that order of
    precedence, and is sent only as an ``Authorization`` header.  Network,
    authentication, and HTTP failures raise :class:`HarnessMcpTransportError`
    (or its :class:`HarnessMcpAuthError` subclass); JSON-RPC error *objects*
    are returned to the caller for the client to map.
    """

    def __init__(
        self,
        base_url: str,
        *,
        bearer_token: str | None = None,
        bearer_source: McpBearerSource | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float | httpx.Timeout = DEFAULT_HTTP_TIMEOUT,
        route: str = MCP_HTTP_ROUTE,
        health_route: str = MCP_HEALTH_ROUTE,
    ) -> None:
        _validate_service_url(base_url, require_https=False)
        if bearer_token is not None and not bearer_token.strip():
            raise ValueError("bearer_token must be non-empty when supplied")
        self._base_url = base_url.rstrip("/")
        self._bearer_token = None if bearer_token is None else bearer_token.strip()
        self._bearer_source = bearer_source
        self._route = _route(route, "route")
        self._health_route = _route(health_route, "health_route")
        self._http = httpx.Client(
            base_url=self._base_url + "/",
            transport=transport,
            timeout=timeout,
        )

    def exchange(self, request: Mapping[str, object]) -> Mapping[str, object]:
        body = self._post(request, expect_response=True)
        assert body is not None
        return body

    def notify(self, request: Mapping[str, object]) -> None:
        self._post(request, expect_response=False)

    def health(self) -> Mapping[str, object]:
        """Probe ``GET /mcp/health`` and return the decoded liveness body."""
        try:
            response = self._http.get(self._health_route, headers=self._headers())
        except httpx.HTTPError as error:
            raise HarnessMcpTransportError(
                "the harness MCP endpoint is unreachable"
            ) from error
        self._raise_for_status(response)
        return _response_object(response)

    def close(self) -> None:
        self._http.close()

    def _post(
        self, request: Mapping[str, object], *, expect_response: bool
    ) -> Mapping[str, object] | None:
        try:
            response = self._http.post(
                self._route,
                json=dict(request),
                headers=self._headers(),
            )
        except httpx.HTTPError as error:
            raise HarnessMcpTransportError(
                "the harness MCP endpoint is unreachable"
            ) from error
        self._raise_for_status(response)
        if not expect_response:
            return None
        return _response_object(response)

    def _headers(self) -> dict[str, str]:
        token = self._resolve_token()
        if token is None:
            raise HarnessMcpAuthError(
                "the harness MCP endpoint requires a bearer credential "
                f"({HARNESS_API_BEARER_TOKEN_VARIABLE} is unset)"
            )
        return {"Authorization": f"Bearer {token}"}

    def _resolve_token(self) -> str | None:
        if self._bearer_token is not None:
            return self._bearer_token
        if self._bearer_source is not None:
            return _clean_text(self._bearer_source.bearer_token())
        return _clean_text(os.environ.get(HARNESS_API_BEARER_TOKEN_VARIABLE))

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code in {401, 403}:
            raise HarnessMcpAuthError(
                "the harness MCP endpoint rejected the bearer credential",
                status_code=response.status_code,
            )
        if response.is_error:
            raise HarnessMcpTransportError(
                f"the harness MCP endpoint returned HTTP {response.status_code}",
                status_code=response.status_code,
            )


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
        self._lock = threading.RLock()
        self._initialized = False
        self._closed = False

    @property
    def server_info(self) -> Mapping[str, object]:
        """Return the ``initialize`` server info, or empty before initialize."""
        return self._server

    @property
    def initialized(self) -> bool:
        """Return whether the initialization handshake has completed."""
        return self._initialized

    @property
    def transport(self) -> McpTransport:
        """Return the transport selected for this client's topology."""
        return self._transport

    def initialize(self) -> Mapping[str, object]:
        """Negotiate the protocol once and acknowledge the initialization.

        The handshake is idempotent: after one successful exchange this returns
        the cached server info without another round trip, so a single client
        can back both the boundary and the tool provider without a duplicate
        initialize.  Sends are serialized on the transport lock, so a shared
        client is safe for the boundary runner and the chat tool loop alike.
        """
        with self._lock:
            if self._initialized:
                return dict(self._server)
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
            self._initialized = True
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

    def get_backtest_status(self, *, job_id: str) -> McpToolResult:
        """Call the ``get_backtest_status`` tool."""
        return self.call_tool(TOOL_GET_BACKTEST_STATUS, {"job_id": job_id})

    def get_run(self, *, run_id: str) -> McpToolResult:
        """Call the ``get_run`` tool."""
        return self.call_tool(TOOL_GET_RUN, {"run_id": run_id})

    def list_artifacts(self, *, run_id: str) -> McpToolResult:
        """Call the ``list_artifacts`` tool."""
        return self.call_tool(TOOL_LIST_ARTIFACTS, {"run_id": run_id})

    def read_artifact(self, *, run_id: str, name: str) -> McpToolResult:
        """Call the ``read_artifact`` tool."""
        return self.call_tool(TOOL_READ_ARTIFACT, {"run_id": run_id, "name": name})

    def run_sweep(self, *, spec_path: str) -> McpToolResult:
        """Call the ``run_sweep`` tool."""
        return self.call_tool(TOOL_RUN_SWEEP, {"spec_path": spec_path})

    def close(self) -> None:
        """Close the underlying transport exactly once."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._transport.close()

    def _request(
        self, method: str, params: Mapping[str, object]
    ) -> Mapping[str, object]:
        with self._lock:
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
    tools: tuple[str, ...] = ()

    @classmethod
    def connect(
        cls,
        client: HarnessMcpClient,
        *,
        attempts: int = DEFAULT_PROBE_ATTEMPTS,
        backoff_seconds: float = DEFAULT_PROBE_BACKOFF_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> HarnessMcpBoundary:
        """Initialize the client and derive capabilities from its catalog.

        A flaky first probe is retried up to ``attempts`` times with a short
        backoff instead of disabling the tool surface on the first failure.
        ``sleep`` is injectable so tests do not wait on a real clock. The
        client handshake is idempotent, so a retry after a failed ``tools/list``
        reuses the successful initialize.
        """
        if attempts < 1:
            raise ValueError("attempts must be a positive integer")
        if backoff_seconds < 0:
            raise ValueError("backoff_seconds must be non-negative")
        last_error: HarnessMcpError | None = None
        for attempt in range(1, attempts + 1):
            try:
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
                    tools=tuple(sorted(names)),
                )
            except HarnessMcpError as error:
                last_error = error
                if attempt < attempts:
                    sleep(backoff_seconds * attempt)
        assert last_error is not None
        raise last_error

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


@dataclass(frozen=True, slots=True)
class HarnessMcpTopology:
    """Where Thesis and the harness run, and how to reach the harness.

    The four Thesis/harness location combinations map onto the transports as:

    ==============  ===============  ==========================================
    Thesis          Harness          Transport
    ==============  ===============  ==========================================
    local           local            :class:`InProcessMcpTransport` when a
                                     handler is supplied, else
                                     :class:`StdioMcpTransport`
    remote          local            :class:`StdioMcpTransport`; only valid when
                                     the Thesis process can reach a local
                                     ``harness`` executable (documented
                                     constraint), so co-locate or inject an
                                     in-process handler
    local           remote           :class:`RemoteHttpMcpTransport`
    remote          remote           :class:`RemoteHttpMcpTransport`
    ==============  ===============  ==========================================

    ``harness_url`` is required for a remote harness and ``harness_command`` is
    used for a local harness.  A bearer credential resolves in this order:
    ``bearer_token``, then ``bearer_source``, then the process environment.
    The token and source are excluded from the representation.
    """

    thesis_location: McpLocation = "local"
    harness_location: McpLocation = "local"
    harness_url: str | None = None
    harness_command: tuple[str, ...] = DEFAULT_HARNESS_MCP_COMMAND
    bearer_token: str | None = field(default=None, repr=False)
    bearer_source: McpBearerSource | None = field(
        default=None, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "thesis_location", _location(self.thesis_location, "thesis_location")
        )
        object.__setattr__(
            self,
            "harness_location",
            _location(self.harness_location, "harness_location"),
        )
        if self.harness_url is not None:
            _validate_service_url(self.harness_url, require_https=False)
        if not isinstance(self.harness_command, (tuple, list)) or any(
            not isinstance(part, str) or not part.strip()
            for part in self.harness_command
        ):
            raise ValueError("harness_command must be a sequence of non-empty strings")
        object.__setattr__(self, "harness_command", tuple(self.harness_command))
        if self.bearer_token is not None:
            token = _clean_text(self.bearer_token)
            if token is None:
                raise ValueError("bearer_token must be non-empty when supplied")
            object.__setattr__(self, "bearer_token", token)
        if self.harness_location == "remote" and self.harness_url is None:
            raise ValueError("harness_url is required for a remote harness")

    def require_harness_url(self) -> str:
        """Return the validated remote URL or raise when it is not published."""
        if self.harness_url is None:
            raise ValueError("harness_url is required for a remote harness")
        return self.harness_url

    def resolved_bearer_token(self) -> str | None:
        """Resolve the bearer token from the topology, source, then environment."""
        if self.bearer_token is not None:
            return self.bearer_token
        if self.bearer_source is not None:
            token = _clean_text(self.bearer_source.bearer_token())
            if token is not None:
                return token
        return _clean_text(os.environ.get(HARNESS_API_BEARER_TOKEN_VARIABLE))


def open_harness_mcp_client(
    topology: HarnessMcpTopology,
    *,
    in_process_handler: _JsonRpcHandler | None = None,
    http_transport: httpx.BaseTransport | None = None,
    stdio_transport: McpTransport | None = None,
) -> HarnessMcpClient:
    """Select and open the harness MCP transport for ``topology``.

    The returned :class:`HarnessMcpClient` is the stable interface the Thesis
    agent consumes: :meth:`HarnessMcpClient.list_tools` and
    :meth:`HarnessMcpClient.call_tool`.  ``in_process_handler``,
    ``http_transport``, and ``stdio_transport`` are test/integration seams that
    avoid spawning a process or opening a socket.
    """
    if topology.harness_location == "remote":
        transport: McpTransport = RemoteHttpMcpTransport(
            topology.require_harness_url(),
            bearer_token=topology.bearer_token,
            bearer_source=topology.bearer_source,
            transport=http_transport,
        )
    elif topology.thesis_location == "local" and in_process_handler is not None:
        transport = InProcessMcpTransport(in_process_handler)
    elif stdio_transport is not None:
        transport = stdio_transport
    else:
        transport = StdioMcpTransport(topology.harness_command)
    return HarnessMcpClient(transport)


def open_harness_mcp_boundary(
    topology: HarnessMcpTopology,
    *,
    in_process_handler: _JsonRpcHandler | None = None,
    http_transport: httpx.BaseTransport | None = None,
    stdio_transport: McpTransport | None = None,
    attempts: int = DEFAULT_PROBE_ATTEMPTS,
    backoff_seconds: float = DEFAULT_PROBE_BACKOFF_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> HarnessMcpBoundary:
    """Open and connect a harness MCP boundary for ``topology``.

    The boundary initializes the JSON-RPC session and derives its declared
    capabilities from the discovered ``tools/list`` catalog, so a launch only
    routes through a tool the harness actually publishes.  A flaky probe is
    retried ``attempts`` times with ``backoff_seconds``-scaled delay before the
    failure propagates; the opened client is closed on failure.
    """
    client = open_harness_mcp_client(
        topology,
        in_process_handler=in_process_handler,
        http_transport=http_transport,
        stdio_transport=stdio_transport,
    )
    try:
        return HarnessMcpBoundary.connect(
            client,
            attempts=attempts,
            backoff_seconds=backoff_seconds,
            sleep=sleep,
        )
    except HarnessMcpError:
        try:
            client.close()
        except Exception:
            pass
        raise


def open_local_harness_boundary(
    *,
    topology: HarnessMcpTopology | None = None,
    process_runner: HarnessProcessRunner | None = None,
    program: Sequence[str] = ("harness",),
    capabilities: HarnessCapabilities | None = None,
    in_process_handler: _JsonRpcHandler | None = None,
    http_transport: httpx.BaseTransport | None = None,
    stdio_transport: McpTransport | None = None,
    attempts: int = DEFAULT_PROBE_ATTEMPTS,
    backoff_seconds: float = DEFAULT_PROBE_BACKOFF_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> HarnessBoundary:
    """Compose the local harness boundary, preferring the MCP tool catalog.

    The MCP boundary is tried first so a local Thesis speaks the published
    harness MCP contract.  When the MCP server cannot be reached or started and
    the harness runs locally, the composition falls back to the direct
    :class:`~thesis.integrations.harness_activation.LocalHarnessProcess`
    boundary using ``process_runner``.  A remote harness never falls back: an
    unreachable remote endpoint raises rather than silently switching to local
    execution.  When no fallback runner is supplied, the MCP failure propagates.
    """
    resolved = harness_mcp_topology_from_env() if topology is None else topology
    try:
        return open_harness_mcp_boundary(
            resolved,
            in_process_handler=in_process_handler,
            http_transport=http_transport,
            stdio_transport=stdio_transport,
            attempts=attempts,
            backoff_seconds=backoff_seconds,
            sleep=sleep,
        )
    except (HarnessMcpError, OSError):
        if resolved.harness_location != "local" or process_runner is None:
            raise
        return LocalHarnessProcess(
            process_runner,
            program=program,
            capabilities=capabilities,
        )


def harness_mcp_topology_from_env(
    environ: Mapping[str, str] | None = None,
) -> HarnessMcpTopology:
    """Build a topology from the deployment environment.

    Precedence is explicit: ``HARNESS_MCP_URL`` selects a remote harness (and
    wins over ``HARNESS_MCP_COMMAND``), ``HARNESS_MCP_COMMAND`` customizes the
    local process, ``HARNESS_API_BEARER_TOKEN`` supplies the remote bearer, and
    ``THESIS_LOCATION`` names where Thesis runs (default ``local``).
    """
    source = os.environ if environ is None else environ
    url = _clean_text(source.get(HARNESS_MCP_URL_VARIABLE))
    token = _clean_text(source.get(HARNESS_API_BEARER_TOKEN_VARIABLE))
    command = _parse_command(source.get(HARNESS_MCP_COMMAND_VARIABLE))
    thesis_location = _location(
        _clean_text(source.get(THESIS_LOCATION_VARIABLE)) or "local",
        THESIS_LOCATION_VARIABLE,
    )
    return HarnessMcpTopology(
        thesis_location=thesis_location,
        harness_location="remote" if url is not None else "local",
        harness_url=url,
        harness_command=command,
        bearer_token=token,
    )


def _parse_command(value: str | None) -> tuple[str, ...]:
    text = _clean_text(value)
    if text is None:
        return DEFAULT_HARNESS_MCP_COMMAND
    try:
        parts = tuple(shlex.split(text))
    except ValueError as error:
        raise ValueError(
            f"{HARNESS_MCP_COMMAND_VARIABLE} is not a valid command"
        ) from error
    if not parts:
        return DEFAULT_HARNESS_MCP_COMMAND
    return parts


def _location(value: object, field: str) -> McpLocation:
    if value not in ("local", "remote"):
        raise ValueError(f"{field} must be 'local' or 'remote'")
    return cast(McpLocation, value)


def _route(value: str, field: str) -> str:
    text = _clean_text(value)
    if text is None:
        raise ValueError(f"{field} must be a non-empty string")
    return text.strip("/")


def _clean_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _validate_service_url(value: str, *, require_https: bool) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("base_url must be a non-empty string")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in ({"https"} if require_https else {"http", "https"})
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        scheme = "HTTPS" if require_https else "HTTP or HTTPS"
        raise ValueError(f"base_url must be a {scheme} service URL")


def _response_object(response: httpx.Response) -> dict[str, object]:
    try:
        decoded = response.json()
    except ValueError as error:
        raise HarnessMcpProtocolError(
            "the harness MCP endpoint returned a non-JSON body"
        ) from error
    if not isinstance(decoded, Mapping):
        raise HarnessMcpProtocolError(
            "the harness MCP endpoint returned a non-object response"
        )
    return dict(decoded)


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


__all__ = [
    "CLIENT_NAME",
    "CLIENT_VERSION",
    "DEFAULT_HARNESS_MCP_COMMAND",
    "DEFAULT_HTTP_TIMEOUT",
    "DEFAULT_PROBE_ATTEMPTS",
    "DEFAULT_PROBE_BACKOFF_SECONDS",
    "HARNESS_API_BEARER_TOKEN_VARIABLE",
    "HARNESS_MCP_COMMAND_VARIABLE",
    "HARNESS_MCP_URL_VARIABLE",
    "MCP_HEALTH_ROUTE",
    "MCP_HTTP_ROUTE",
    "PROTOCOL_VERSION",
    "THESIS_LOCATION_VARIABLE",
    "TOOL_BACKTEST_CONFIG_TEMPLATE",
    "TOOL_GET_BACKTEST_STATUS",
    "TOOL_GET_RUN",
    "TOOL_LAUNCH_TRADER",
    "TOOL_LIST_ARTIFACTS",
    "TOOL_READ_ARTIFACT",
    "TOOL_RUN_BACKTEST",
    "TOOL_RUN_STUDY",
    "TOOL_RUN_SWEEP",
    "EnvironmentHarnessTokenSource",
    "HarnessMcpAuthError",
    "HarnessMcpBoundary",
    "HarnessMcpClient",
    "HarnessMcpError",
    "HarnessMcpProtocolError",
    "HarnessMcpToolError",
    "HarnessMcpTopology",
    "HarnessMcpTransportError",
    "InProcessMcpTransport",
    "McpBearerSource",
    "McpLocation",
    "McpTool",
    "McpToolResult",
    "McpTransport",
    "RemoteHttpMcpTransport",
    "StdioMcpTransport",
    "harness_mcp_topology_from_env",
    "open_harness_mcp_boundary",
    "open_harness_mcp_client",
    "open_local_harness_boundary",
]
