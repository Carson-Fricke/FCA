"""Harness MCP-backed HTTP surface for the Thesis UI.

The harness publishes its backtest operations (launch, job status, run, and
artifact queries) as MCP tools over either a local transport or the remote
``POST /mcp`` endpoint.  This router fronts those operations for the UI so the
same routes work for all four Thesis/harness topologies, and it exposes
``GET /v1/harness/status`` reporting the resolved topology, reachability, the
published tool catalog, and the remote MCP health document.

Every call goes through :func:`thesis.integrations.mcp.open_harness_mcp_client`
so local and remote deployments are indistinguishable to the UI.  A status
probe never fails the request: an unreachable harness is reported as
``reachable: false`` with an ``error`` string, letting the UI render the
connection state instead of a transport error.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from pydantic import BaseModel, ConfigDict, Field

from thesis.integrations.harness_activation import (
    CapabilityUnavailableError,
    HarnessActivationError,
)
from thesis.integrations.mcp import (
    HarnessMcpAuthError,
    HarnessMcpClient,
    HarnessMcpError,
    HarnessMcpProtocolError,
    HarnessMcpToolError,
    HarnessMcpTopology,
    HarnessMcpTransportError,
    McpToolResult,
    RemoteHttpMcpTransport,
    harness_mcp_topology_from_env,
    open_harness_mcp_client,
)

ClientFactory = Callable[[], HarnessMcpClient]


class LaunchHarnessBacktestRequest(BaseModel):
    """The canonical launch body accepted by ``POST /v1/backtests/jobs``."""

    model_config = ConfigDict(extra="forbid")

    config_path: str = Field(min_length=1, max_length=2_048)
    workspace: str | None = Field(default=None, max_length=128)


class HarnessDataService:
    """MCP-backed harness operations plus a topology/connection status probe.

    ``factory`` is the transport seam: it defaults to
    :func:`open_harness_mcp_client` over the resolved topology, while tests
    inject an in-process handler or a mocked HTTP transport.  A fresh client is
    opened per call and closed afterwards, so a rotated local process or a
    changed remote endpoint never leaks across requests.
    """

    def __init__(
        self,
        topology: HarnessMcpTopology | None = None,
        *,
        factory: ClientFactory | None = None,
    ) -> None:
        self._topology = (
            topology if topology is not None else harness_mcp_topology_from_env()
        )
        self._factory: ClientFactory = (
            factory
            if factory is not None
            else lambda: open_harness_mcp_client(self._topology)
        )

    @property
    def topology(self) -> HarnessMcpTopology:
        """Return the resolved Thesis/harness topology."""
        return self._topology

    def status(self) -> dict[str, object]:
        """Probe the harness and report topology, reachability, and tools."""
        topology = self._topology
        document: dict[str, object] = {
            "mode": topology.harness_location,
            "thesis_location": topology.thesis_location,
            "harness_location": topology.harness_location,
            "reachable": False,
            "server": {},
            "tools": [],
            "health": None,
            "error": None,
        }
        try:
            client = self._factory()
        except HarnessMcpError as error:
            document["error"] = str(error)
            return document
        try:
            client.initialize()
            tools = client.list_tools()
            document["server"] = dict(client.server_info)
            document["tools"] = [
                {"name": tool.name, "description": tool.description} for tool in tools
            ]
            document["reachable"] = True
            health = _probe_health(client)
            if health is not None:
                document["health"] = health
        except HarnessMcpError as error:
            document["error"] = str(error)
        finally:
            client.close()
        return document

    def launch_backtest(
        self, request: LaunchHarnessBacktestRequest
    ) -> dict[str, object]:
        """Invoke the ``run_backtest`` tool for the configuration document."""
        config_path = request.config_path
        workspace = request.workspace
        result = self._tool_result(
            lambda client: client.run_backtest(
                config_path=config_path, workspace=workspace
            )
        )
        payload = _object(result.structured)
        exit_code = _optional_int(payload.get("exit_code"))
        return {
            "action": "backtest",
            "config_path": config_path,
            "workspace": workspace,
            "boundary": _text(payload.get("boundary")),
            "job_id": _text(payload.get("job_id")),
            "state": _text(payload.get("state")) or _exit_state(exit_code),
            "exit_code": exit_code,
            "location": _text(payload.get("location")),
            "result": payload,
        }

    def job_status(self, job_id: str) -> dict[str, object]:
        """Return one backtest job row from the ``get_backtest_status`` tool."""
        result = self._tool_result(
            lambda client: client.get_backtest_status(job_id=job_id)
        )
        return _object(result.structured)

    def get_run(self, run_id: str) -> dict[str, object]:
        """Return one stored run row from the ``get_run`` tool."""
        result = self._tool_result(lambda client: client.get_run(run_id=run_id))
        return _object(result.structured)

    def list_artifacts(self, run_id: str) -> dict[str, object]:
        """Return the run's artifact manifest, normalized for the UI contract."""
        result = self._tool_result(lambda client: client.list_artifacts(run_id=run_id))
        payload = _object(result.structured)
        entries = payload.get("artifacts")
        artifacts: list[dict[str, object]] = []
        if isinstance(entries, (list, tuple)):
            for entry in entries:
                if not isinstance(entry, Mapping):
                    continue
                artifacts.append(
                    {
                        "name": _text(entry.get("name")),
                        "uri": _text(entry.get("uri")),
                        "sha256": _text(entry.get("sha256")),
                        "sizeBytes": _non_negative_int(entry.get("sizeBytes")),
                        "sasUrl": _text(entry.get("sas_url")),
                    }
                )
        return {
            "run_id": _text(payload.get("run_id")) or run_id,
            "artifacts": artifacts,
        }

    def _tool_result(
        self, operation: Callable[[HarnessMcpClient], McpToolResult]
    ) -> McpToolResult:
        client = self._factory()
        try:
            client.initialize()
            result = operation(client)
        finally:
            client.close()
        payload = result.structured
        # A non-zero local exit is an observed outcome, not a client failure;
        # only an error without a process outcome is raised.
        if result.is_error and payload.get("exit_code") is None:
            raise HarnessMcpToolError(
                result.tool, result.text or "unknown error", payload
            )
        return result


def _probe_health(client: HarnessMcpClient) -> dict[str, object] | None:
    transport = client.transport
    if isinstance(transport, RemoteHttpMcpTransport):
        try:
            return dict(transport.health())
        except HarnessMcpError:
            return None
    return None


def _object(value: object) -> dict[str, object]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return {}


def _text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _non_negative_int(value: object) -> int:
    resolved = _optional_int(value)
    if resolved is None or resolved < 0:
        return 0
    return resolved


def _exit_state(exit_code: int | None) -> str | None:
    if exit_code is None:
        return None
    return "completed" if exit_code == 0 else "failed"


def get_harness_service(request: Request) -> HarnessDataService:
    """Resolve the service from app state so applications control the topology."""
    service = getattr(request.app.state, "harness_data_service", None)
    if service is None:
        service = HarnessDataService()
        request.app.state.harness_data_service = service
    if not isinstance(service, HarnessDataService):
        raise RuntimeError(
            "app.state.harness_data_service must be a HarnessDataService"
        )
    return service


def _service_error(error: Exception) -> HTTPException:
    if isinstance(error, HarnessMcpAuthError):
        return HTTPException(
            status_code=503,
            detail={"code": "harness_mcp_auth_error", "message": str(error)},
        )
    if isinstance(error, HarnessMcpTransportError):
        return HTTPException(
            status_code=503,
            detail={"code": "harness_mcp_unreachable", "message": str(error)},
        )
    if isinstance(error, HarnessMcpToolError):
        if "does not exist" in str(error):
            return HTTPException(
                status_code=404,
                detail={"code": "harness_resource_not_found", "message": str(error)},
            )
        return HTTPException(
            status_code=502,
            detail={"code": "harness_tool_error", "message": str(error)},
        )
    if isinstance(error, HarnessMcpProtocolError):
        return HTTPException(
            status_code=502,
            detail={"code": "harness_protocol_error", "message": str(error)},
        )
    if isinstance(error, HarnessMcpError):
        return HTTPException(
            status_code=502,
            detail={"code": "harness_mcp_error", "message": str(error)},
        )
    if isinstance(error, (CapabilityUnavailableError, HarnessActivationError)):
        return HTTPException(
            status_code=503,
            detail={"code": "harness_unavailable", "message": str(error)},
        )
    if isinstance(error, ValueError):
        return HTTPException(status_code=422, detail=str(error))
    return HTTPException(
        status_code=500,
        detail={"code": "harness_error", "message": "request failed"},
    )


def create_harness_router() -> APIRouter:
    """Create the harness MCP router; the service resolves from app state."""
    router = APIRouter(tags=["harness"])

    @router.get("/v1/harness/status")
    async def harness_status(
        service: Annotated[HarnessDataService, Depends(get_harness_service)],
    ) -> dict[str, object]:
        return service.status()

    @router.post("/v1/backtests/jobs")
    async def launch_backtest(
        request: LaunchHarnessBacktestRequest,
        service: Annotated[HarnessDataService, Depends(get_harness_service)],
    ) -> dict[str, object]:
        try:
            return service.launch_backtest(request)
        except Exception as error:
            raise _service_error(error) from error

    @router.get("/v1/backtests/jobs/{job_id}")
    async def backtest_job_status(
        job_id: Annotated[str, Path(min_length=1, max_length=128)],
        service: Annotated[HarnessDataService, Depends(get_harness_service)],
    ) -> dict[str, object]:
        try:
            return service.job_status(job_id)
        except Exception as error:
            raise _service_error(error) from error

    @router.get("/v1/backtests/runs/{run_id}")
    async def backtest_run(
        run_id: Annotated[str, Path(min_length=1, max_length=128)],
        service: Annotated[HarnessDataService, Depends(get_harness_service)],
    ) -> dict[str, object]:
        try:
            return service.get_run(run_id)
        except Exception as error:
            raise _service_error(error) from error

    @router.get("/v1/backtests/runs/{run_id}/artifacts")
    async def backtest_run_artifacts(
        run_id: Annotated[str, Path(min_length=1, max_length=128)],
        service: Annotated[HarnessDataService, Depends(get_harness_service)],
    ) -> dict[str, object]:
        try:
            return service.list_artifacts(run_id)
        except Exception as error:
            raise _service_error(error) from error

    return router


__all__ = [
    "HarnessDataService",
    "LaunchHarnessBacktestRequest",
    "create_harness_router",
    "get_harness_service",
]
