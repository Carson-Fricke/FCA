"""Harness MCP-backed HTTP surface for the Thesis UI.

The harness publishes its backtest operations (launch, job status, job result,
and artifact queries) as MCP tools over either a local transport or the remote
``POST /mcp`` endpoint.  This router fronts those operations for the UI on
neutral ``/v1/jobs`` read routes so the same routes work for all four
Thesis/harness topologies, and it exposes ``GET /v1/harness/status`` reporting
the resolved topology, reachability, the published tool catalog, and the remote
MCP health document.

Every call goes through :func:`thesis.integrations.mcp.open_harness_mcp_client`
so local and remote deployments are indistinguishable to the UI.  A status
probe never fails the request: an unreachable harness is reported as
``reachable: false`` with an ``error`` string, letting the UI render the
connection state instead of a transport error.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from pydantic import BaseModel, ConfigDict, Field

from thesis.integrations.mcp import (
    HarnessMcpClient,
    HarnessMcpError,
    HarnessMcpProtocolError,
    HarnessMcpToolError,
    HarnessMcpTopology,
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
        """Invoke the ``run_backtest`` tool for the configuration document.

        The execution identity is allocated before dispatch and retained on
        every failure path, so an unknown outcome is inspectable by its
        original id and is never replayed under another one.
        """
        config_path = request.config_path
        workspace = request.workspace
        job_id = f"job-{uuid4().hex}"
        try:
            result = self._tool_result(
                lambda client: client.run_backtest(
                    config_path=config_path, workspace=workspace, job_id=job_id
                )
            )
        except HarnessMcpError as error:
            error.job_id = job_id
            raise
        payload = _object(result.structured)
        returned = _text(payload.get("job_id"))
        if returned is None or returned != job_id:
            error = HarnessMcpProtocolError(
                "run_backtest acknowledged job_id "
                f"{returned!r} for submitted job_id {job_id!r}"
            )
            error.job_id = job_id
            raise error
        exit_code = _optional_int(payload.get("exit_code"))
        return {
            "action": "backtest",
            "kind": "backtest",
            "config_path": config_path,
            "workspace": workspace,
            "boundary": _text(payload.get("boundary")),
            "job_id": job_id,
            "state": _text(payload.get("state")) or _exit_state(exit_code),
            "exit_code": exit_code,
            "location": _text(payload.get("location")),
        }

    def job_status(self, job_id: str) -> dict[str, object]:
        """Return one job row from the ``get_job_status`` tool."""
        result = self._tool_result(lambda client: client.get_job_status(job_id=job_id))
        return _object(result.structured)

    def get_job_result(self, job_id: str) -> dict[str, object]:
        """Return one job result row from the ``get_job_result`` tool."""
        result = self._tool_result(lambda client: client.get_job_result(job_id=job_id))
        return _object(result.structured)

    def list_artifacts(self, job_id: str) -> dict[str, object]:
        """Return the job's artifact manifest, normalized for the UI contract."""
        result = self._tool_result(lambda client: client.list_artifacts(job_id=job_id))
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
            "job_id": _text(payload.get("job_id")) or job_id,
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
    return "succeeded" if exit_code == 0 else "failed"


def get_harness_service(request: Request) -> HarnessDataService:
    """Resolve the service from app state so applications control the topology."""
    from thesis.http.errors import require_service

    return require_service(request, "harness_data_service", HarnessDataService)


def _service_error(error: Exception) -> HTTPException:
    from thesis.http.errors import service_error

    return service_error(error)


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

    @router.get("/v1/jobs/{job_id}")
    async def backtest_job_status(
        job_id: Annotated[str, Path(min_length=1, max_length=128)],
        service: Annotated[HarnessDataService, Depends(get_harness_service)],
    ) -> dict[str, object]:
        try:
            return service.job_status(job_id)
        except Exception as error:
            raise _service_error(error) from error

    @router.get("/v1/jobs/{job_id}/result")
    async def backtest_job_result(
        job_id: Annotated[str, Path(min_length=1, max_length=128)],
        service: Annotated[HarnessDataService, Depends(get_harness_service)],
    ) -> dict[str, object]:
        try:
            return service.get_job_result(job_id)
        except Exception as error:
            raise _service_error(error) from error

    @router.get("/v1/jobs/{job_id}/artifacts")
    async def backtest_job_artifacts(
        job_id: Annotated[str, Path(min_length=1, max_length=128)],
        service: Annotated[HarnessDataService, Depends(get_harness_service)],
    ) -> dict[str, object]:
        try:
            return service.list_artifacts(job_id)
        except Exception as error:
            raise _service_error(error) from error

    return router


__all__ = [
    "HarnessDataService",
    "LaunchHarnessBacktestRequest",
    "create_harness_router",
    "get_harness_service",
]
