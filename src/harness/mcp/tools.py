"""Tool catalog and execution boundaries for the harness MCP server.

The registry mirrors the harness's published surfaces as MCP tools:

- ``run_backtest`` — local ``harness backtest-job --config <path>`` process or
  a bearer-authenticated ``POST /v1/backtests/jobs`` to the remote API
  (202 ``{job_id, state}`` plus ``Location``), selected by the injected
  execution boundary.
- ``get_backtest_status`` — ``GET /v1/backtests/jobs/{job_id}``
  (``BacktestJobStatusResponse``).
- ``get_run`` — ``GET /v1/backtests/runs/{run_id}`` (``RunResponse``).
- ``list_artifacts`` — ``GET /v1/backtests/runs/{run_id}/artifacts``
  (``ArtifactSummary`` list).
- ``run_sweep`` — the local :class:`harness.sweeps.runner.SweepRunner`
  programmatic surface through an injected submitter (sweeps have no remote
  route or process boundary).
- ``launch_trader`` — local ``harness trader --config <path>`` process,
  gated behind an explicit trader capability flag.

Capability semantics mirror T173's :class:`thesis.integrations.harness_activation.HarnessCapabilities`
record: backtest, status, artifacts, and sweeps are available by default and
trader is opt-in. Shadow and live trading have no implemented harness
boundary, so they have no capability field and never appear in the tool
list — unimplemented capabilities are absent, not stubbed.

Every boundary is injected. The server composes no subprocess, HTTP client,
or credential beyond the :class:`harness.mcp.auth.BearerTokenSource`, and
serialized settings never contain a token value.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Final, Protocol
from urllib.parse import quote

from harness.configuration import load_configuration
from harness.mcp.auth import BEARER_TOKEN_VARIABLE, BearerTokenSource

TOOL_RUN_BACKTEST: Final = "run_backtest"
TOOL_BACKTEST_STATUS: Final = "get_backtest_status"
TOOL_GET_RUN: Final = "get_run"
TOOL_LIST_ARTIFACTS: Final = "list_artifacts"
TOOL_RUN_SWEEP: Final = "run_sweep"
TOOL_LAUNCH_TRADER: Final = "launch_trader"

_LOCAL_BOUNDARY: Final = "local"
_REMOTE_BOUNDARY: Final = "remote"
_LAUNCH_ROUTE: Final = "/v1/backtests/jobs"
_JOB_STATUS_ROUTE: Final = "/v1/backtests/jobs"
_RUN_ROUTE: Final = "/v1/backtests/runs"
_IDENTIFIER_MAX: Final = 128

_TEXT_TYPE: Final = {"type": "string", "minLength": 1, "maxLength": _IDENTIFIER_MAX}


class ToolExecutionError(RuntimeError):
    """A tool-level failure reported as an ``isError`` MCP result.

    ``payload`` optionally carries structured outcome data (for example the
    local process exit code) so the failure stays queryable by the caller.
    """

    def __init__(
        self,
        message: str,
        *,
        payload: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.payload = None if payload is None else dict(payload)


@dataclass(frozen=True, slots=True)
class ToolCapabilities:
    """Fail-closed declaration of the tools this server publishes.

    Mirrors the T173 capability record: the implemented local boundaries
    (``backtest-job``, ``trader``) and the remote API's implemented routes
    (launch, job status, run query, artifacts) drive the defaults. Shadow and
    live are intentionally unmodelable — the harness has no such boundary, so
    no flag and no tool exist for them.
    """

    backtest: bool = True
    backtest_status: bool = True
    artifacts: bool = True
    sweeps: bool = True
    trader: bool = False

    def __post_init__(self) -> None:
        for name in ("backtest", "backtest_status", "artifacts", "sweeps", "trader"):
            value = getattr(self, name)
            if not isinstance(value, bool):
                raise TypeError(f"capability {name!r} must be a boolean")

    def as_mapping(self) -> dict[str, bool]:
        """Return the declaration keyed by capability name."""
        return {
            "backtest": self.backtest,
            "backtest_status": self.backtest_status,
            "artifacts": self.artifacts,
            "sweeps": self.sweeps,
            "trader": self.trader,
        }


@dataclass(frozen=True, slots=True)
class ProcessOutcome:
    """One local harness process invocation's observed outcome."""

    returncode: int
    stdout: str = ""
    stderr: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.returncode, int) or isinstance(self.returncode, bool):
            raise TypeError("returncode must be an integer")
        if not isinstance(self.stdout, str) or not isinstance(self.stderr, str):
            raise TypeError("stdout and stderr must be strings")


class ProcessRunner(Protocol):
    """Injected local process boundary; no subprocess is created here."""

    def __call__(self, argv: Sequence[str]) -> ProcessOutcome: ...


@dataclass(frozen=True, slots=True)
class RemoteResponse:
    """One decoded remote harness API response."""

    status_code: int
    headers: Mapping[str, str] = field(default_factory=dict)
    body: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.status_code, int) or isinstance(self.status_code, bool):
            raise TypeError("status_code must be an integer")
        if not isinstance(self.headers, Mapping):
            raise TypeError("headers must be a mapping of strings")
        if not isinstance(self.body, Mapping):
            raise TypeError("body must be the decoded JSON object mapping")


class RemoteTransport(Protocol):
    """HTTP request surface implemented by a real client or a test fake."""

    def get(self, url: str, headers: Mapping[str, str]) -> RemoteResponse: ...

    def post(
        self,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
    ) -> RemoteResponse: ...


class SweepSubmitter(Protocol):
    """Injected local sweeps surface for one spec document path."""

    def __call__(self, spec_path: str) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class HarnessToolsContext:
    """Injected boundaries and settings behind the tool handlers.

    ``mode`` selects the ``run_backtest`` boundary: ``local`` invokes the
    configured :class:`ProcessRunner`, ``remote`` posts to the remote API.
    The remote query tools use the remote boundary whenever ``api_base_url``
    and a transport are composed. ``credentials`` resolves the bearer token
    per call; it is the only credential-shaped object here and never
    serializes a token value (see :meth:`as_settings`).
    """

    mode: str
    credentials: BearerTokenSource
    capabilities: ToolCapabilities = field(default_factory=ToolCapabilities)
    program: tuple[str, ...] = ("harness",)
    api_base_url: str | None = None
    remote_transport: RemoteTransport | None = None
    runner: ProcessRunner | None = None
    sweep_submitter: SweepSubmitter | None = None

    def __post_init__(self) -> None:
        if self.mode not in (_LOCAL_BOUNDARY, _REMOTE_BOUNDARY):
            raise ValueError(
                f"mode must be {_LOCAL_BOUNDARY!r} or {_REMOTE_BOUNDARY!r}, "
                f"got {self.mode!r}"
            )
        if not isinstance(self.capabilities, ToolCapabilities):
            raise TypeError("capabilities must be a ToolCapabilities record")
        if not callable(getattr(self.credentials, "bearer_token", None)) or not (
            callable(getattr(self.credentials, "bearer_headers", None))
        ):
            raise TypeError(
                "credentials must implement BearerTokenSource (bearer_token/"
                "bearer_headers)"
            )
        program = tuple(self.program)
        if not program or any(
            not isinstance(part, str) or not part.strip() for part in program
        ):
            raise ValueError(
                "program must be a non-empty sequence of non-empty strings"
            )
        object.__setattr__(self, "program", program)
        base_url = self.api_base_url
        if base_url is not None:
            if not isinstance(base_url, str) or not base_url.strip():
                raise ValueError("api_base_url must be a non-empty string")
            if not base_url.strip().lower().startswith("https://"):
                raise ValueError("remote harness api_base_url must use HTTPS")
            object.__setattr__(self, "api_base_url", base_url.strip().rstrip("/"))
        if self.remote_transport is not None and not (
            callable(getattr(self.remote_transport, "get", None))
            and callable(getattr(self.remote_transport, "post", None))
        ):
            raise TypeError(
                "remote_transport must implement RemoteTransport (get/post)"
            )
        if self.mode == _REMOTE_BOUNDARY:
            if self.api_base_url is None or self.remote_transport is None:
                raise ValueError(
                    "remote execution requires an https api_base_url and a "
                    "RemoteTransport"
                )
        elif not callable(self.runner):
            raise TypeError("local execution requires a callable ProcessRunner")
        if self.sweep_submitter is not None and not callable(self.sweep_submitter):
            raise TypeError("sweep_submitter must be callable")

    def as_settings(self) -> dict[str, object]:
        """Serialize the deployment settings without any credential value."""
        return {
            "mode": self.mode,
            "program": list(self.program),
            "api_base_url": self.api_base_url,
            "bearer_env_var": BEARER_TOKEN_VARIABLE,
            "bearer_configured": self.credentials.bearer_token() is not None,
            "capabilities": self.capabilities.as_mapping(),
        }


@dataclass(frozen=True, slots=True)
class Tool:
    """One MCP tool: name, contract, and dispatch target."""

    name: str
    description: str
    input_schema: Mapping[str, object]
    handler: Callable[[Mapping[str, object]], Mapping[str, object]]
    capability: str

    def schema_entry(self) -> dict[str, object]:
        """Return the ``tools/list`` entry for this tool."""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": dict(self.input_schema),
        }


@dataclass(frozen=True, slots=True)
class ToolCatalog:
    """Immutable, capability-filtered tool registry."""

    tools: tuple[Tool, ...]

    def __post_init__(self) -> None:
        names = [tool.name for tool in self.tools]
        if len(set(names)) != len(names):
            raise ValueError("tool names must be unique")

    def resolve(self, name: object) -> Tool | None:
        """Return the named tool, or ``None`` when not published."""
        for tool in self.tools:
            if tool.name == name:
                return tool
        return None

    def names(self) -> tuple[str, ...]:
        """Return the published tool names in catalog order."""
        return tuple(tool.name for tool in self.tools)


@dataclass(frozen=True, slots=True)
class HarnessTools:
    """Handlers bound to one injected boundary composition."""

    context: HarnessToolsContext

    def run_backtest(self, arguments: Mapping[str, object]) -> dict[str, object]:
        """Run one backtest through the selected local or remote boundary."""
        config_path = _text_argument(arguments, "config_path")
        if self.context.mode == _REMOTE_BOUNDARY:
            return self._launch_remote_backtest(config_path)
        tail = ["backtest-job", "--config", config_path]
        tail.extend(_workspace_arguments(arguments))
        return self._invoke_process(tail, "backtest-job")

    def launch_trader(self, arguments: Mapping[str, object]) -> dict[str, object]:
        """Run one supervised paper trading session locally."""
        config_path = _text_argument(arguments, "config_path")
        return self._invoke_process(["trader", "--config", config_path], "trader")

    def get_backtest_status(
        self, arguments: Mapping[str, object]
    ) -> Mapping[str, object]:
        """Read one job's persisted state from the remote harness API."""
        job_id = _identifier_argument(arguments, "job_id")
        return self._remote_get(
            self._remote_url(f"{_JOB_STATUS_ROUTE}/{quote(job_id, safe='')}"),
            not_found_message=f"backtest job {job_id!r} does not exist",
        )

    def get_run(self, arguments: Mapping[str, object]) -> Mapping[str, object]:
        """Read one stored run's catalog row from the remote harness API."""
        run_id = _identifier_argument(arguments, "run_id")
        return self._remote_get(
            self._remote_url(f"{_RUN_ROUTE}/{quote(run_id, safe='')}"),
            not_found_message=f"backtest run {run_id!r} does not exist",
        )

    def list_artifacts(self, arguments: Mapping[str, object]) -> Mapping[str, object]:
        """List one stored run's artifacts from the remote harness API."""
        run_id = _identifier_argument(arguments, "run_id")
        return self._remote_get(
            self._remote_url(f"{_RUN_ROUTE}/{quote(run_id, safe='')}/artifacts"),
            not_found_message=f"backtest run {run_id!r} does not exist",
        )

    def run_sweep(self, arguments: Mapping[str, object]) -> Mapping[str, object]:
        """Expand and submit one sweep through the injected local runner."""
        spec_path = _text_argument(arguments, "spec_path")
        submitter = self.context.sweep_submitter
        if submitter is None:
            raise ToolExecutionError(
                "sweep execution is not composed in this MCP server; "
                "sweeps run through the local sweeps runner only"
            )
        try:
            submission = submitter(spec_path)
        except Exception as error:
            raise ToolExecutionError(
                "the local sweep runner could not complete the sweep"
            ) from error
        if not isinstance(submission, Mapping):
            raise ToolExecutionError(
                "the local sweep runner returned an invalid submission"
            )
        return dict(submission)

    def _invoke_process(self, tail: Sequence[str], process: str) -> dict[str, object]:
        runner = self.context.runner
        if runner is None:
            raise ToolExecutionError(
                f"the local harness process {process!r} could not be invoked: "
                "no process runner is composed"
            )
        argv = (*self.context.program, *tail)
        try:
            outcome = runner(argv)
        except Exception as error:
            raise ToolExecutionError(
                f"the local harness process {process!r} could not be invoked"
            ) from error
        if not isinstance(outcome, ProcessOutcome):
            raise ToolExecutionError(
                "the local harness runner returned an invalid process outcome"
            )
        payload: dict[str, object] = {
            "boundary": _LOCAL_BOUNDARY,
            "exit_code": outcome.returncode,
            "stdout": outcome.stdout,
            "stderr": outcome.stderr,
        }
        if outcome.returncode != 0:
            raise ToolExecutionError(
                f"the local harness process {process!r} exited with code "
                f"{outcome.returncode}",
                payload=payload,
            )
        return payload

    def _launch_remote_backtest(self, config_path: str) -> dict[str, object]:
        response = self._remote_post(
            self._remote_url(_LAUNCH_ROUTE),
            _backtest_payload(config_path),
        )
        status_code = response.status_code
        if status_code in (401, 403):
            raise ToolExecutionError(_credential_rejected(status_code))
        if status_code in (404, 501):
            raise ToolExecutionError(
                "the remote harness API does not implement backtest launch "
                f"(HTTP {status_code})"
            )
        if status_code < 200 or status_code >= 300:
            raise ToolExecutionError(
                f"remote backtest launch failed (HTTP {status_code})"
            )
        body = response.body
        job_id = body.get("job_id")
        state = body.get("state")
        if not isinstance(job_id, str) or not job_id.strip():
            raise ToolExecutionError(
                "remote harness launch response requires a non-empty 'job_id' string"
            )
        if not isinstance(state, str) or not state.strip():
            raise ToolExecutionError(
                "remote harness launch response requires a non-empty 'state' string"
            )
        location = response.headers.get("Location")
        return {
            "boundary": _REMOTE_BOUNDARY,
            "job_id": job_id.strip(),
            "state": state.strip(),
            "location": location,
        }

    def _remote_get(self, url: str, *, not_found_message: str) -> Mapping[str, object]:
        response = self._dispatch_remote("get", url)
        status_code = response.status_code
        if status_code == 200:
            return dict(response.body)
        if status_code in (401, 403):
            raise ToolExecutionError(_credential_rejected(status_code))
        if status_code == 404:
            raise ToolExecutionError(not_found_message)
        raise ToolExecutionError(f"the remote harness API returned HTTP {status_code}")

    def _remote_post(self, url: str, payload: Mapping[str, object]) -> RemoteResponse:
        return self._dispatch_remote("post", url, payload)

    def _dispatch_remote(
        self,
        method: str,
        url: str,
        payload: Mapping[str, object] | None = None,
    ) -> RemoteResponse:
        context = self.context
        if context.api_base_url is None or context.remote_transport is None:
            raise ToolExecutionError(
                "the remote harness API boundary is not configured for this MCP server"
            )
        headers = self._remote_headers()
        transport = context.remote_transport
        try:
            if method == "get":
                return transport.get(url, headers)
            return transport.post(url, headers, payload or {})
        except ToolExecutionError:
            raise
        except Exception as error:
            raise ToolExecutionError(
                "the remote harness API could not be reached"
            ) from error

    def _remote_headers(self) -> dict[str, str]:
        try:
            return self.context.credentials.bearer_headers()
        except Exception as error:
            raise ToolExecutionError(
                f"remote harness API calls need a bearer credential: {error}"
            ) from error

    def _remote_url(self, route: str) -> str:
        base_url = self.context.api_base_url
        if base_url is None:
            raise ToolExecutionError(
                "the remote harness API boundary is not configured for this MCP server"
            )
        return f"{base_url}{route}"


def build_catalog(context: HarnessToolsContext) -> ToolCatalog:
    """Build the capability-filtered tool catalog for one composition."""
    capabilities = context.capabilities
    tools = HarnessTools(context)
    candidates: list[Tool] = []
    if capabilities.backtest:
        candidates.append(
            Tool(
                name=TOOL_RUN_BACKTEST,
                description=(
                    "Run one deterministic backtest: a local 'harness "
                    "backtest-job' process, or a bearer-authenticated POST to "
                    "the remote harness API returning {job_id, state}."
                ),
                input_schema={
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["config_path"],
                    "properties": {
                        "config_path": {
                            "type": "string",
                            "minLength": 1,
                            "description": (
                                "Path to the backtest configuration document."
                            ),
                        },
                        "workspace": {
                            "type": "string",
                            "minLength": 1,
                            "description": (
                                "Local output directory passed as --output-dir; "
                                "ignored when the remote API executes the launch."
                            ),
                        },
                    },
                },
                handler=tools.run_backtest,
                capability="backtest",
            )
        )
    if capabilities.backtest_status:
        candidates.append(
            Tool(
                name=TOOL_BACKTEST_STATUS,
                description=(
                    "Read one backtest job's persisted state "
                    "{job_id, state, created_at, updated_at, error_message} "
                    "from the remote harness API."
                ),
                input_schema={
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["job_id"],
                    "properties": {"job_id": _TEXT_TYPE},
                },
                handler=tools.get_backtest_status,
                capability="backtest_status",
            )
        )
    if capabilities.artifacts:
        candidates.append(
            Tool(
                name=TOOL_GET_RUN,
                description=(
                    "Read one stored run's catalog row {run_id, status, "
                    "strategy_id, metrics, ...} from the remote harness API."
                ),
                input_schema={
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["run_id"],
                    "properties": {"run_id": _TEXT_TYPE},
                },
                handler=tools.get_run,
                capability="artifacts",
            )
        )
        candidates.append(
            Tool(
                name=TOOL_LIST_ARTIFACTS,
                description=(
                    "List one stored run's artifact summaries {name, size, "
                    "object_path, sas_url} from the remote harness API."
                ),
                input_schema={
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["run_id"],
                    "properties": {"run_id": _TEXT_TYPE},
                },
                handler=tools.list_artifacts,
                capability="artifacts",
            )
        )
    if capabilities.sweeps:
        candidates.append(
            Tool(
                name=TOOL_RUN_SWEEP,
                description=(
                    "Expand and submit one parameter sweep through the local "
                    "sweeps runner; returns {sweep_id, requested, expanded, "
                    "duplicates_skipped, run_ids}."
                ),
                input_schema={
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["spec_path"],
                    "properties": {
                        "spec_path": {
                            "type": "string",
                            "minLength": 1,
                            "description": (
                                "Path to the JSON sweep specification document."
                            ),
                        },
                    },
                },
                handler=tools.run_sweep,
                capability="sweeps",
            )
        )
    if capabilities.trader:
        candidates.append(
            Tool(
                name=TOOL_LAUNCH_TRADER,
                description=(
                    "Run one supervised paper trading session through the "
                    "local 'harness trader' process; the exit code is the "
                    "session outcome."
                ),
                input_schema={
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["config_path"],
                    "properties": {
                        "config_path": {
                            "type": "string",
                            "minLength": 1,
                            "description": (
                                "Path to the trader configuration document."
                            ),
                        },
                    },
                },
                handler=tools.launch_trader,
                capability="trader",
            )
        )
    return ToolCatalog(tools=tuple(candidates))


def _workspace_arguments(arguments: Mapping[str, object]) -> list[str]:
    workspace = arguments.get("workspace")
    if workspace is None:
        return []
    if not isinstance(workspace, str) or not workspace.strip():
        raise ToolExecutionError("workspace must be a non-empty string")
    return ["--output-dir", workspace]


def _backtest_payload(config_path: str) -> dict[str, object]:
    """Render the API launch body: one validated backtest configuration."""
    try:
        configuration = load_configuration(config_path)
    except (OSError, ValueError) as error:
        raise ToolExecutionError(
            f"could not load the backtest configuration {config_path!r}: {error}"
        ) from error
    document = configuration.model_dump(mode="python")
    if not isinstance(document, dict):
        raise ToolExecutionError("the backtest configuration must be an object")
    return {key: _json_ready(value) for key, value in document.items()}


def _json_ready(value: object) -> object:
    """Map exact-decimal YAML scalars onto the JSON client spellings.

    Typed decimals travel as JSON numbers and string-exact parameter values
    stay strings, mirroring the committed launch-route client convention so
    the API's exact-decimal decoder revalidates the document unchanged.
    """
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Mapping):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_ready(item) for item in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _text_argument(arguments: Mapping[str, object], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ToolExecutionError(f"{key} must be a non-empty string")
    return value


def _identifier_argument(arguments: Mapping[str, object], key: str) -> str:
    value = _text_argument(arguments, key)
    if len(value) > _IDENTIFIER_MAX:
        raise ToolExecutionError(f"{key} must be at most {_IDENTIFIER_MAX} characters")
    return value


def _credential_rejected(status_code: int) -> str:
    return f"the remote harness API rejected the bearer credential (HTTP {status_code})"


__all__ = [
    "HarnessTools",
    "HarnessToolsContext",
    "ProcessOutcome",
    "ProcessRunner",
    "RemoteResponse",
    "RemoteTransport",
    "SweepSubmitter",
    "TOOL_BACKTEST_STATUS",
    "TOOL_GET_RUN",
    "TOOL_LAUNCH_TRADER",
    "TOOL_LIST_ARTIFACTS",
    "TOOL_RUN_BACKTEST",
    "TOOL_RUN_SWEEP",
    "Tool",
    "ToolCapabilities",
    "ToolCatalog",
    "ToolExecutionError",
    "build_catalog",
]
