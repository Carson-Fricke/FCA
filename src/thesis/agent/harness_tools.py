"""Harness MCP tool provider for the Thesis agent.

The agent discovers and calls the quant-harness MCP tool catalog (backtests,
sweeps, run queries, artifacts, and the supervised trader) through a single
:class:`HarnessToolProvider`.  The provider wraps the merged
:class:`~thesis.integrations.mcp.HarnessMcpClient` and exposes:

* :meth:`HarnessToolProvider.discover` -- the published tool catalog.
* :meth:`HarnessToolProvider.list_tools` -- the catalog or a typed error.
* :meth:`HarnessToolProvider.call_tool` -- one structured call result.
* one typed convenience method per harness tool.
* :func:`harness_mcp_skill` -- a versioned :class:`~thesis.agent.skills.ThesisSkill`
  that declares the harness tools for role policies.

Topology is resolved once from the deployment environment through
:func:`~thesis.integrations.mcp.harness_mcp_topology_from_env` and
:func:`~thesis.integrations.mcp.open_harness_mcp_client`; agent code never
branches on where Thesis or the harness runs.  Authentication, transport,
protocol, and tool failures become agent-visible
:class:`HarnessToolFailure` values instead of unhandled exceptions.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, Protocol

import httpx

from thesis.agent.skills import ThesisSkill
from thesis.integrations.mcp import (
    TOOL_GET_BACKTEST_STATUS,
    TOOL_GET_RUN,
    TOOL_LAUNCH_TRADER,
    TOOL_LIST_ARTIFACTS,
    TOOL_RUN_BACKTEST,
    TOOL_RUN_SWEEP,
    HarnessMcpAuthError,
    HarnessMcpClient,
    HarnessMcpError,
    HarnessMcpProtocolError,
    HarnessMcpToolError,
    HarnessMcpTopology,
    HarnessMcpTransportError,
    McpToolResult,
    McpTransport,
    harness_mcp_topology_from_env,
    open_harness_mcp_client,
)

#: The harness MCP tools the Thesis agent may discover and call.
HARNESS_TOOL_NAMES: tuple[str, ...] = (
    TOOL_RUN_BACKTEST,
    TOOL_GET_BACKTEST_STATUS,
    TOOL_GET_RUN,
    TOOL_LIST_ARTIFACTS,
    TOOL_RUN_SWEEP,
    TOOL_LAUNCH_TRADER,
)

#: Skill identity and version declaring the harness tools to role policies.
HARNESS_MCP_SKILL_ID = "harness-mcp"
HARNESS_MCP_SKILL_VERSION = "1.0.0"

HARNESS_MCP_SKILL_INSTRUCTIONS = (
    "Use the quant-harness MCP tools for deterministic research execution. "
    "run_backtest and run_sweep launch backtest and sweep jobs; "
    "get_backtest_status and get_run report job and run state; "
    "list_artifacts enumerates a run's immutable outputs; and launch_trader "
    "starts a supervised trader session. Pass typed arguments, rely on the "
    "structured result content as evidence, and surface any returned failure "
    "instead of assuming success."
)

FailureKind = Literal[
    "auth", "transport", "protocol", "tool", "harness", "unknown_tool"
]


class HarnessToolError(RuntimeError):
    """Raised when the harness tool catalog cannot be used by the agent."""

    def __init__(self, failure: HarnessToolFailure) -> None:
        super().__init__(failure.message)
        self.failure = failure


@dataclass(frozen=True, slots=True)
class HarnessToolFailure:
    """A typed, agent-visible harness tool failure with no credentials."""

    kind: FailureKind
    message: str
    tool: str | None = None
    status_code: int | None = None
    payload: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))

    def as_dict(self) -> dict[str, object]:
        """Return the canonical, credential-free failure representation."""
        return {
            "kind": self.kind,
            "message": self.message,
            "payload": dict(self.payload),
            "statusCode": self.status_code,
            "tool": self.tool,
        }


@dataclass(frozen=True, slots=True)
class HarnessToolSpec:
    """One published harness tool from ``tools/list``."""

    name: str
    description: str
    input_schema: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "input_schema", MappingProxyType(dict(self.input_schema))
        )

    def as_dict(self) -> dict[str, object]:
        """Return the canonical tool metadata."""
        return {
            "description": self.description,
            "inputSchema": dict(self.input_schema),
            "name": self.name,
        }


@dataclass(frozen=True, slots=True)
class HarnessToolCatalog:
    """The discovered harness tool catalog, or the failure that prevented it."""

    tools: tuple[HarnessToolSpec, ...] = ()
    failure: HarnessToolFailure | None = None

    @property
    def ok(self) -> bool:
        """Return whether discovery completed without a failure."""
        return self.failure is None


@dataclass(frozen=True, slots=True)
class HarnessToolResult:
    """The structured outcome of one harness tool call."""

    tool: str
    ok: bool
    structured: Mapping[str, object] = field(default_factory=dict)
    text: str = ""
    failure: HarnessToolFailure | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "structured", MappingProxyType(dict(self.structured)))
        if self.ok and self.failure is not None:
            raise ValueError("a successful harness tool result cannot carry a failure")
        if not self.ok and self.failure is None:
            raise ValueError("a failed harness tool result must carry a failure")

    @property
    def is_error(self) -> bool:
        """Return whether the call surfaced a failure."""
        return not self.ok

    def as_dict(self) -> dict[str, object]:
        """Return the canonical structured result representation."""
        return {
            "failure": None if self.failure is None else self.failure.as_dict(),
            "ok": self.ok,
            "structured": dict(self.structured),
            "text": self.text,
            "tool": self.tool,
        }


@dataclass(frozen=True, slots=True)
class RunBacktestArgs:
    """Typed arguments for ``run_backtest``."""

    config_path: str
    workspace: str | None = None

    def __post_init__(self) -> None:
        _required_text(self.config_path, "config_path")
        if self.workspace is not None:
            _required_text(self.workspace, "workspace")

    def to_arguments(self) -> dict[str, object]:
        """Return the MCP ``tools/call`` arguments for this request."""
        arguments: dict[str, object] = {"config_path": self.config_path}
        if self.workspace is not None:
            arguments["workspace"] = self.workspace
        return arguments


@dataclass(frozen=True, slots=True)
class GetBacktestStatusArgs:
    """Typed arguments for ``get_backtest_status``."""

    job_id: str

    def __post_init__(self) -> None:
        _required_text(self.job_id, "job_id")

    def to_arguments(self) -> dict[str, object]:
        """Return the MCP ``tools/call`` arguments for this request."""
        return {"job_id": self.job_id}


@dataclass(frozen=True, slots=True)
class GetRunArgs:
    """Typed arguments for ``get_run``."""

    run_id: str

    def __post_init__(self) -> None:
        _required_text(self.run_id, "run_id")

    def to_arguments(self) -> dict[str, object]:
        """Return the MCP ``tools/call`` arguments for this request."""
        return {"run_id": self.run_id}


@dataclass(frozen=True, slots=True)
class ListArtifactsArgs:
    """Typed arguments for ``list_artifacts``."""

    run_id: str

    def __post_init__(self) -> None:
        _required_text(self.run_id, "run_id")

    def to_arguments(self) -> dict[str, object]:
        """Return the MCP ``tools/call`` arguments for this request."""
        return {"run_id": self.run_id}


@dataclass(frozen=True, slots=True)
class RunSweepArgs:
    """Typed arguments for ``run_sweep``."""

    spec_path: str

    def __post_init__(self) -> None:
        _required_text(self.spec_path, "spec_path")

    def to_arguments(self) -> dict[str, object]:
        """Return the MCP ``tools/call`` arguments for this request."""
        return {"spec_path": self.spec_path}


@dataclass(frozen=True, slots=True)
class LaunchTraderArgs:
    """Typed arguments for ``launch_trader``."""

    config_path: str

    def __post_init__(self) -> None:
        _required_text(self.config_path, "config_path")

    def to_arguments(self) -> dict[str, object]:
        """Return the MCP ``tools/call`` arguments for this request."""
        return {"config_path": self.config_path}


class InProcessMcpHandler(Protocol):
    """Structural contract for an injected in-process MCP handler."""

    def handle(self, request: object) -> dict[str, object] | None: ...


class HarnessToolProvider:
    """Discover and call harness MCP tools through an injected client.

    The provider adds typed arguments, structured results, and agent-visible
    failures on top of the raw :class:`HarnessMcpClient` JSON-RPC surface.
    """

    def __init__(self, client: HarnessMcpClient) -> None:
        self._client = client

    @property
    def client(self) -> HarnessMcpClient:
        """Return the topology-resolved harness MCP client."""
        return self._client

    def discover(self) -> HarnessToolCatalog:
        """Return the published harness tool catalog or a typed failure."""
        try:
            tools = self._client.list_tools()
        except HarnessMcpError as error:
            return HarnessToolCatalog(failure=_failure_from_error(error))
        return HarnessToolCatalog(
            tools=tuple(
                HarnessToolSpec(
                    name=tool.name,
                    description=tool.description,
                    input_schema=tool.input_schema,
                )
                for tool in tools
            )
        )

    def list_tools(self) -> tuple[HarnessToolSpec, ...]:
        """Return the discovered catalog, raising :class:`HarnessToolError`."""
        catalog = self.discover()
        if catalog.failure is not None:
            raise HarnessToolError(catalog.failure)
        return catalog.tools

    def call_tool(
        self, name: str, arguments: Mapping[str, object] | None = None
    ) -> HarnessToolResult:
        """Call one harness tool, mapping every failure to a typed result."""
        if name not in HARNESS_TOOL_NAMES:
            return HarnessToolResult(
                tool=name,
                ok=False,
                failure=HarnessToolFailure(
                    kind="unknown_tool",
                    message=f"unknown harness MCP tool: {name!r}",
                    tool=name,
                ),
            )
        try:
            result = self._client.call_tool(name, arguments)
        except HarnessMcpError as error:
            return HarnessToolResult(
                tool=name,
                ok=False,
                failure=_failure_from_error(error, tool=name),
            )
        return _result_from_mcp(name, result)

    def run_backtest(self, args: RunBacktestArgs) -> HarnessToolResult:
        """Call ``run_backtest`` with typed arguments."""
        return self.call_tool(TOOL_RUN_BACKTEST, args.to_arguments())

    def get_backtest_status(self, args: GetBacktestStatusArgs) -> HarnessToolResult:
        """Call ``get_backtest_status`` with typed arguments."""
        return self.call_tool(TOOL_GET_BACKTEST_STATUS, args.to_arguments())

    def get_run(self, args: GetRunArgs) -> HarnessToolResult:
        """Call ``get_run`` with typed arguments."""
        return self.call_tool(TOOL_GET_RUN, args.to_arguments())

    def list_artifacts(self, args: ListArtifactsArgs) -> HarnessToolResult:
        """Call ``list_artifacts`` with typed arguments."""
        return self.call_tool(TOOL_LIST_ARTIFACTS, args.to_arguments())

    def run_sweep(self, args: RunSweepArgs) -> HarnessToolResult:
        """Call ``run_sweep`` with typed arguments."""
        return self.call_tool(TOOL_RUN_SWEEP, args.to_arguments())

    def launch_trader(self, args: LaunchTraderArgs) -> HarnessToolResult:
        """Call ``launch_trader`` with typed arguments."""
        return self.call_tool(TOOL_LAUNCH_TRADER, args.to_arguments())

    def close(self) -> None:
        """Close the underlying harness MCP transport."""
        self._client.close()


def harness_mcp_skill(
    version: str = HARNESS_MCP_SKILL_VERSION,
) -> ThesisSkill:
    """Return the versioned skill declaring the harness MCP tool surface."""
    return ThesisSkill(
        skill_id=HARNESS_MCP_SKILL_ID,
        version=version,
        instructions=HARNESS_MCP_SKILL_INSTRUCTIONS,
        allowed_tools=HARNESS_TOOL_NAMES,
    )


def open_harness_tool_provider(
    topology: HarnessMcpTopology | None = None,
    *,
    in_process_handler: InProcessMcpHandler | None = None,
    http_transport: httpx.BaseTransport | None = None,
    stdio_transport: McpTransport | None = None,
) -> HarnessToolProvider:
    """Resolve the deployment topology and open a harness tool provider.

    A ``topology`` argument takes precedence; otherwise the topology is read
    from the process environment.  ``in_process_handler``, ``http_transport``,
    and ``stdio_transport`` are test/integration seams passed straight to
    :func:`open_harness_mcp_client`, so the agent never selects a transport.
    """
    resolved = harness_mcp_topology_from_env() if topology is None else topology
    client = open_harness_mcp_client(
        resolved,
        in_process_handler=in_process_handler,
        http_transport=http_transport,
        stdio_transport=stdio_transport,
    )
    return HarnessToolProvider(client)


def _failure_from_error(
    error: HarnessMcpError, *, tool: str | None = None
) -> HarnessToolFailure:
    if isinstance(error, HarnessMcpToolError):
        return HarnessToolFailure(
            kind="tool",
            message=str(error),
            tool=error.tool,
            payload=error.payload or {},
        )
    if isinstance(error, HarnessMcpAuthError):
        return HarnessToolFailure(
            kind="auth",
            message=str(error),
            tool=tool,
            status_code=error.status_code,
        )
    if isinstance(error, HarnessMcpTransportError):
        return HarnessToolFailure(
            kind="transport",
            message=str(error),
            tool=tool,
            status_code=error.status_code,
        )
    if isinstance(error, HarnessMcpProtocolError):
        return HarnessToolFailure(kind="protocol", message=str(error), tool=tool)
    return HarnessToolFailure(kind="harness", message=str(error), tool=tool)


def _result_from_mcp(tool: str, result: McpToolResult) -> HarnessToolResult:
    if result.is_error:
        return HarnessToolResult(
            tool=tool,
            ok=False,
            structured=result.structured,
            text=result.text,
            failure=HarnessToolFailure(
                kind="tool",
                message=result.text or f"harness MCP tool {tool!r} returned an error",
                tool=tool,
                payload=result.structured,
            ),
        )
    return HarnessToolResult(
        tool=tool,
        ok=True,
        structured=result.structured,
        text=result.text,
    )


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-empty text")
    return value


__all__ = [
    "HARNESS_MCP_SKILL_ID",
    "HARNESS_MCP_SKILL_INSTRUCTIONS",
    "HARNESS_MCP_SKILL_VERSION",
    "HARNESS_TOOL_NAMES",
    "FailureKind",
    "GetBacktestStatusArgs",
    "GetRunArgs",
    "HarnessToolCatalog",
    "HarnessToolError",
    "HarnessToolFailure",
    "HarnessToolProvider",
    "HarnessToolResult",
    "HarnessToolSpec",
    "InProcessMcpHandler",
    "LaunchTraderArgs",
    "ListArtifactsArgs",
    "RunBacktestArgs",
    "RunSweepArgs",
    "harness_mcp_skill",
    "open_harness_tool_provider",
]
