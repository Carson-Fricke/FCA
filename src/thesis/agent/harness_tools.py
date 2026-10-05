"""Harness MCP tool provider for the Thesis agent.

The agent discovers and calls the harness-hosted MCP tool catalog (backtests,
studies, run queries, artifacts, and the supervised trader) through a single
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

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, Protocol

import httpx

from thesis.agent.skills import SkillRef, SkillRegistry, ThesisSkill
from thesis.integrations.mcp import (
    DEFAULT_PROBE_ATTEMPTS,
    DEFAULT_PROBE_BACKOFF_SECONDS,
    TOOL_BACKTEST_CONFIG_TEMPLATE,
    TOOL_GET_JOB_STATUS,
    TOOL_GET_RUN,
    TOOL_GET_SCREENER_STATUS,
    TOOL_LAUNCH_TRADER,
    TOOL_LIST_ARTIFACTS,
    TOOL_READ_ARTIFACT,
    TOOL_READ_SDK_REFERENCE,
    TOOL_RUN_BACKTEST,
    TOOL_RUN_SCREENER,
    TOOL_RUN_STUDY,
    TOOL_SCREENER_CONFIG_TEMPLATE,
    TOOL_VALIDATE_STRATEGY,
    TOOL_VALIDATE_STUDY,
    ExecutionCertainty,
    HarnessMcpAuthError,
    HarnessMcpBoundary,
    HarnessMcpClient,
    HarnessMcpError,
    HarnessMcpProtocolError,
    HarnessMcpToolError,
    HarnessMcpTopology,
    HarnessMcpTransportError,
    McpToolResult,
    harness_mcp_topology_from_env,
    open_harness_mcp_boundary,
    open_harness_mcp_client,
    outcome_certainty,
)

#: The harness MCP tools the Thesis agent may discover and call.
HARNESS_TOOL_NAMES: tuple[str, ...] = (
    TOOL_RUN_BACKTEST,
    TOOL_RUN_STUDY,
    TOOL_VALIDATE_STUDY,
    TOOL_RUN_SCREENER,
    TOOL_VALIDATE_STRATEGY,
    TOOL_BACKTEST_CONFIG_TEMPLATE,
    TOOL_SCREENER_CONFIG_TEMPLATE,
    TOOL_GET_JOB_STATUS,
    TOOL_GET_SCREENER_STATUS,
    TOOL_GET_RUN,
    TOOL_LIST_ARTIFACTS,
    TOOL_READ_ARTIFACT,
    TOOL_LAUNCH_TRADER,
    TOOL_READ_SDK_REFERENCE,
)

#: Skill identity and version declaring the harness tools to role policies.
HARNESS_MCP_SKILL_ID = "harness-mcp"
HARNESS_MCP_SKILL_VERSION = "1.0.0"

HARNESS_MCP_SKILL_INSTRUCTIONS = (
    "Use the quant-harness MCP tools for deterministic research execution. "
    "run_backtest and run_study launch backtest and study jobs; validate_study "
    "and validate_strategy compile, load, and construct authored programs without replaying so an authoring error "
    "is caught before the submission; run_screener "
    "launches a screening run of an authored Rust program; "
    "backtest_config_template and screener_config_template return starter "
    "configurations to adapt when no config file exists, and run_backtest and "
    "run_screener accept those configurations inline as the `config` argument; "
    "get_job_status, get_screener_status, and get_run report job and run "
    "state; list_artifacts enumerates a run's immutable outputs and read_artifact "
    "reads one artifact's text or hex-encoded bytes; and launch_trader "
    "starts a supervised trader session. Pass typed arguments, rely on the "
    "structured result content as evidence, and surface any returned failure "
    "instead of assuming success."
)

FailureKind = Literal[
    "auth", "transport", "protocol", "tool", "harness", "denied", "unknown_tool"
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
    execution_certainty: ExecutionCertainty = "not_started"
    deterministic_compiler_diagnostic: bool = False

    @property
    def retry_guidance(self) -> str:
        """Describe recovery without authorizing an automatic mutation replay."""
        if self.deterministic_compiler_diagnostic:
            return "Fix the authored source using these compiler diagnostics before validating again."
        if self.execution_certainty == "not_started":
            return (
                "Resolve the failure before an explicit retry; execution did not start."
            )
        return (
            "Inspect run/job state before explicitly retrying a mutation; "
            "never automatically replay run_study, run_backtest, run_screener, or launch_trader."
        )

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
            "executionCertainty": self.execution_certainty,
            "retryGuidance": self.retry_guidance,
            "deterministicCompilerDiagnostic": self.deterministic_compiler_diagnostic,
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
class RunStudyArgs:
    """Typed arguments for ``run_study``."""

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
class RunScreenerArgs:
    """Typed arguments for ``run_screener``."""

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
class GetJobStatusArgs:
    """Typed arguments for ``get_job_status``."""

    job_id: str

    def __post_init__(self) -> None:
        _required_text(self.job_id, "job_id")

    def to_arguments(self) -> dict[str, object]:
        """Return the MCP ``tools/call`` arguments for this request."""
        return {"job_id": self.job_id}


@dataclass(frozen=True, slots=True)
class GetScreenerStatusArgs:
    """Typed arguments for ``get_screener_status``."""

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
class ReadArtifactArgs:
    """Typed arguments for ``read_artifact``."""

    run_id: str
    name: str

    def __post_init__(self) -> None:
        _required_text(self.run_id, "run_id")
        _required_text(self.name, "name")

    def to_arguments(self) -> dict[str, object]:
        """Return the MCP ``tools/call`` arguments for this request."""
        return {"run_id": self.run_id, "name": self.name}


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
        self._catalog_lock = threading.RLock()
        self._catalog: HarnessToolCatalog | None = None

    def invalidate(self) -> None:
        """Drop the catalog; validation identity and reuse belong to the compiler."""
        with self._catalog_lock:
            self._catalog = None

    def refresh(self) -> HarnessToolCatalog:
        """Invalidate and rediscover the published tool catalog."""
        with self._catalog_lock:
            self.invalidate()
            return self.discover()

    @property
    def client(self) -> HarnessMcpClient:
        """Return the topology-resolved harness MCP client."""
        return self._client

    def discover(self) -> HarnessToolCatalog:
        """Return the published harness tool catalog or a typed failure."""
        with self._catalog_lock:
            if self._catalog is not None:
                return self._catalog
            try:
                tools = self._client.list_tools()
            except HarnessMcpError as error:
                return HarnessToolCatalog(failure=_failure_from_error(error))
            self._catalog = HarnessToolCatalog(
                tools=tuple(
                    HarnessToolSpec(
                        name=tool.name,
                        description=tool.description,
                        input_schema=tool.input_schema,
                    )
                    for tool in tools
                )
            )
            return self._catalog

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
            return _result_from_mcp(name, self._client.call_tool(name, arguments))
        except HarnessMcpError as error:
            return HarnessToolResult(
                tool=name,
                ok=False,
                failure=_failure_from_error(error, tool=name),
            )

    def run_backtest(self, args: RunBacktestArgs) -> HarnessToolResult:
        """Call ``run_backtest`` with typed arguments."""
        return self.call_tool(TOOL_RUN_BACKTEST, args.to_arguments())

    def run_study(self, args: RunStudyArgs) -> HarnessToolResult:
        """Call ``run_study`` with typed arguments."""
        return self.call_tool(TOOL_RUN_STUDY, args.to_arguments())

    def run_screener(self, args: RunScreenerArgs) -> HarnessToolResult:
        """Call ``run_screener`` with typed arguments."""
        return self.call_tool(TOOL_RUN_SCREENER, args.to_arguments())

    def validate_study(self, *, model: Mapping[str, object]) -> HarnessToolResult:
        """Call ``validate_study`` to compile one authored model program."""
        return self.call_tool(TOOL_VALIDATE_STUDY, {"model": dict(model)})

    def validate_strategy(self, *, model: Mapping[str, object]) -> HarnessToolResult:
        """Validate a strategy and preserve structured diagnostics in a typed result."""
        return self.call_tool(TOOL_VALIDATE_STRATEGY, {"model": dict(model)})

    def current_server_info(self) -> Mapping[str, object]:
        """Fetch fresh serving SDK/compiler metadata, not the catalog cache."""
        return self._client.current_server_info()

    def validation_identity(self) -> str | None:
        """Fetch the current compiler identity for persisted-validation checks."""
        return self._client.validation_identity()

    def read_sdk_reference(self, **arguments: object) -> HarnessToolResult:
        """Read serving SDK metadata or bounded source with typed failure handling."""
        return self.call_tool(TOOL_READ_SDK_REFERENCE, arguments)

    def backtest_config_template(self) -> HarnessToolResult:
        """Call ``backtest_config_template`` for a starter configuration."""
        return self.call_tool(TOOL_BACKTEST_CONFIG_TEMPLATE, {})

    def screener_config_template(self) -> HarnessToolResult:
        """Call ``screener_config_template`` for a starter screen configuration."""
        return self.call_tool(TOOL_SCREENER_CONFIG_TEMPLATE, {})

    def get_job_status(self, args: GetJobStatusArgs) -> HarnessToolResult:
        """Call ``get_job_status`` with typed arguments."""
        return self.call_tool(TOOL_GET_JOB_STATUS, args.to_arguments())

    def get_screener_status(self, args: GetScreenerStatusArgs) -> HarnessToolResult:
        """Call ``get_screener_status`` with typed arguments."""
        return self.call_tool(TOOL_GET_SCREENER_STATUS, args.to_arguments())

    def get_run(self, args: GetRunArgs) -> HarnessToolResult:
        """Call ``get_run`` with typed arguments."""
        return self.call_tool(TOOL_GET_RUN, args.to_arguments())

    def list_artifacts(self, args: ListArtifactsArgs) -> HarnessToolResult:
        """Call ``list_artifacts`` with typed arguments."""
        return self.call_tool(TOOL_LIST_ARTIFACTS, args.to_arguments())

    def read_artifact(self, args: ReadArtifactArgs) -> HarnessToolResult:
        """Call ``read_artifact`` with typed arguments."""
        return self.call_tool(TOOL_READ_ARTIFACT, args.to_arguments())

    def launch_trader(self, args: LaunchTraderArgs) -> HarnessToolResult:
        """Call ``launch_trader`` with typed arguments."""
        return self.call_tool(TOOL_LAUNCH_TRADER, args.to_arguments())

    def close(self) -> None:
        """Close the underlying harness MCP transport."""
        self.invalidate()
        self._client.close()


@dataclass(frozen=True, slots=True)
class HarnessMcpComposition:
    """One shared harness MCP client with the boundary connected over it.

    Both the :class:`~thesis.integrations.mcp.HarnessMcpBoundary` (backtest
    launches) and :meth:`tool_provider` (the chat tool loop) reuse
    :attr:`client`, so a local stdio process or a remote ``/mcp`` session is
    opened and initialized exactly once for the whole composition.
    """

    client: HarnessMcpClient
    boundary: HarnessMcpBoundary

    @property
    def tool_names(self) -> tuple[str, ...]:
        """Return the tool names discovered while connecting the boundary."""
        return self.boundary.tools

    def tool_provider(self) -> HarnessToolProvider:
        """Return a tool provider that reuses the already-initialized client."""
        return open_harness_tool_provider(client=self.client)


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


def harness_mcp_skill_ref(
    version: str = HARNESS_MCP_SKILL_VERSION,
) -> SkillRef:
    """Return the pinned ``harness-mcp`` skill reference used by role policies."""
    return SkillRef(HARNESS_MCP_SKILL_ID, version)


def register_harness_mcp_skill(
    registry: SkillRegistry,
    *,
    version: str = HARNESS_MCP_SKILL_VERSION,
) -> ThesisSkill:
    """Register the harness MCP skill into ``registry`` and return it.

    Registration is idempotent for the default definition, so a composition
    root can safely register the skill before building the
    :class:`~thesis.agent.router.ModelRouter` and callers may still inject the
    same registry.
    """
    skill = harness_mcp_skill(version)
    registry.register(skill)
    return skill


def open_harness_tool_provider(
    topology: HarnessMcpTopology | None = None,
    *,
    client: HarnessMcpClient | None = None,
    in_process_handler: InProcessMcpHandler | None = None,
    http_transport: httpx.BaseTransport | None = None,
) -> HarnessToolProvider:
    """Resolve the deployment topology and open a harness tool provider.

    With ``client`` the provider wraps an already-initialized shared client --
    for example the one behind a :class:`HarnessMcpBoundary` -- and never opens
    or initializes a second transport.  Otherwise ``topology`` takes precedence
    over the process environment, and ``in_process_handler``,
    and ``http_transport`` are test/integration seams
    passed straight to :func:`open_harness_mcp_client`, so the agent never
    selects a transport.
    """
    if client is not None:
        if (
            topology is not None
            or in_process_handler is not None
            or http_transport is not None
        ):
            raise ValueError(
                "open_harness_tool_provider takes either a client or topology "
                "seams, not both"
            )
        return HarnessToolProvider(client)
    resolved = harness_mcp_topology_from_env() if topology is None else topology
    opened = open_harness_mcp_client(
        resolved,
        in_process_handler=in_process_handler,
        http_transport=http_transport,
    )
    return HarnessToolProvider(opened)


def open_harness_mcp_composition(
    topology: HarnessMcpTopology | None = None,
    *,
    in_process_handler: InProcessMcpHandler | None = None,
    http_transport: httpx.BaseTransport | None = None,
    attempts: int = DEFAULT_PROBE_ATTEMPTS,
    backoff_seconds: float = DEFAULT_PROBE_BACKOFF_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> HarnessMcpComposition:
    """Open exactly one harness MCP client and connect the boundary over it.

    The topology-aware :func:`open_harness_mcp_client` selects the harness-hosted
    the remote ``/mcp`` HTTP transport.  The client is initialized once while
    connecting the boundary (with a bounded retry for a flaky first probe), and
    the returned composition shares that same client with its tool provider.
    """
    resolved = harness_mcp_topology_from_env() if topology is None else topology
    boundary = open_harness_mcp_boundary(
        resolved,
        in_process_handler=in_process_handler,
        http_transport=http_transport,
        attempts=attempts,
        backoff_seconds=backoff_seconds,
        sleep=sleep,
    )
    return HarnessMcpComposition(client=boundary.client, boundary=boundary)


def _failure_from_error(
    error: HarnessMcpError, *, tool: str | None = None
) -> HarnessToolFailure:
    if isinstance(error, HarnessMcpToolError):
        return HarnessToolFailure(
            kind="tool",
            message=str(error),
            tool=error.tool,
            payload=error.payload or {},
            execution_certainty=error.execution_certainty,
            deterministic_compiler_diagnostic=(
                (error.payload or {}).get("deterministicCompilerDiagnostic") is True
            ),
        )
    if isinstance(error, HarnessMcpAuthError):
        return HarnessToolFailure(
            kind="auth",
            message=str(error),
            tool=tool,
            status_code=error.status_code,
            execution_certainty=error.execution_certainty,
        )
    if isinstance(error, HarnessMcpTransportError):
        return HarnessToolFailure(
            kind="transport",
            message=str(error),
            tool=tool,
            status_code=error.status_code,
            execution_certainty=error.execution_certainty,
        )
    if isinstance(error, HarnessMcpProtocolError):
        return HarnessToolFailure(
            kind="protocol",
            message=str(error),
            tool=tool,
            payload=error.payload,
            execution_certainty=error.execution_certainty,
        )
    return HarnessToolFailure(
        kind="harness",
        message=str(error),
        tool=tool,
        execution_certainty=error.execution_certainty,
    )


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
                execution_certainty=outcome_certainty(result.structured),
                deterministic_compiler_diagnostic=(
                    result.structured.get("deterministicCompilerDiagnostic") is True
                ),
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
    "GetJobStatusArgs",
    "GetRunArgs",
    "GetScreenerStatusArgs",
    "HarnessToolCatalog",
    "HarnessToolError",
    "HarnessToolFailure",
    "HarnessMcpComposition",
    "HarnessToolProvider",
    "HarnessToolResult",
    "HarnessToolSpec",
    "InProcessMcpHandler",
    "LaunchTraderArgs",
    "ListArtifactsArgs",
    "ReadArtifactArgs",
    "RunBacktestArgs",
    "RunScreenerArgs",
    "RunStudyArgs",
    "harness_mcp_skill",
    "harness_mcp_skill_ref",
    "open_harness_mcp_composition",
    "open_harness_tool_provider",
    "register_harness_mcp_skill",
]
