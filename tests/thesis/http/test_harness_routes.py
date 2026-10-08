"""Focused route tests for the MCP-backed harness HTTP surface.

The harness is never spawned: the local topology is exercised through an
in-process JSON-RPC handler and the remote topology through ``httpx``'s mock
transport, so launch, job status, job result, artifacts, and the topology
status probe are covered without a process or socket.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping

import httpx
from fastapi.testclient import TestClient

from thesis.http.application import create_app
from thesis.http.harness import HarnessDataService
from thesis.integrations.mcp import (
    HarnessMcpTopology,
    open_harness_mcp_client,
)

BEARER_TOKEN = "thesis-test-token"
HEADERS = {"Authorization": f"Bearer {BEARER_TOKEN}"}
HARNESS_URL = "http://harness.test:9000"
SHA = "a" * 64


class FakeMcpHandler:
    """In-process MCP dispatch for a local harness."""

    def __init__(
        self,
        *,
        missing: bool = False,
        acknowledged_job_id: str | None = None,
        omit_job_id: bool = False,
    ) -> None:
        self._missing = missing
        self._acknowledged_job_id = acknowledged_job_id
        self._omit_job_id = omit_job_id
        self.calls: list[str] = []
        self.arguments: list[dict[str, object]] = []

    def handle(self, request: object) -> dict[str, object] | None:
        if not isinstance(request, Mapping):
            raise TypeError("request must be a mapping")
        message = dict(request)
        if "id" not in message:
            return None
        method = message.get("method")
        if method == "initialize":
            result: dict[str, object] = {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "harness", "version": "0.1.0"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {"name": name, "description": f"{name} tool", "inputSchema": {}}
                    for name in (
                        "run_backtest",
                        "get_job_status",
                        "get_job_result",
                        "list_artifacts",
                    )
                ]
            }
        elif method == "tools/call":
            params = message["params"]
            assert isinstance(params, dict)
            name = str(params.get("name"))
            self.calls.append(name)
            arguments = params.get("arguments")
            self.arguments.append(
                dict(arguments) if isinstance(arguments, Mapping) else {}
            )
            result = self._call(name, self.arguments[-1])
        else:
            result = {}
        return {"jsonrpc": "2.0", "id": message.get("id"), "result": result}

    def _call(self, name: str, arguments: Mapping[str, object]) -> dict[str, object]:
        if self._missing and name != "run_backtest":
            identifier = "job-1"
            noun = "job"
            return {
                "isError": True,
                "content": [
                    {"type": "text", "text": f"{noun} '{identifier}' does not exist"}
                ],
            }
        if name == "run_backtest":
            job_id: object = arguments.get("job_id")
            if self._omit_job_id:
                job_id = None
            elif self._acknowledged_job_id is not None:
                job_id = self._acknowledged_job_id
            payload: dict[str, object] = {
                "boundary": "remote",
                "kind": "backtest",
                "state": "queued",
                "location": f"/v1/jobs/{job_id}",
            }
            if job_id is not None:
                payload["job_id"] = job_id
            return {"isError": False, "content": [], "structuredContent": payload}
        payloads: dict[str, dict[str, object]] = {
            "get_job_status": {
                "job_id": "job-1",
                "state": "succeeded",
                "created_at": "2026-09-09T12:00:00Z",
                "updated_at": "2026-09-09T12:01:00Z",
                "error_message": None,
            },
            "get_job_result": {
                "job_id": "job-1",
                "kind": "backtest",
                "state": "succeeded",
                "strategy_id": "momentum",
                "artifact_count": 1,
                "total_bytes": 512,
            },
            "list_artifacts": {
                "job_id": "job-1",
                "artifacts": [
                    {
                        "name": "metrics.json",
                        "uri": "jobs/job-1/artifacts/metrics.json",
                        "sha256": SHA,
                        "sizeBytes": 512,
                        "sas_url": None,
                    }
                ],
            },
        }
        return {"isError": False, "content": [], "structuredContent": payloads[name]}


class FakeHttpMcpServer:
    """Minimal MCP-over-HTTP server implementing ``POST /mcp`` and health."""

    def __init__(self, *, status: int = 200) -> None:
        self._status = status

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("Authorization") != f"Bearer {BEARER_TOKEN}":
            return httpx.Response(401, json={"error": "unauthorized"})
        if self._status != 200:
            return httpx.Response(self._status, json={})
        if request.method == "GET" and request.url.path == "/mcp/health":
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "serverInfo": {"name": "harness", "version": "0.1.0"},
                    "tools": ["run_backtest"],
                },
            )
        if request.method == "POST" and request.url.path == "/mcp":
            body = json.loads(request.content)
            if "id" not in body:
                return httpx.Response(202)
            method = body.get("method")
            if method == "initialize":
                result: dict[str, object] = {"protocolVersion": "2024-11-05"}
            elif method == "tools/list":
                result = {
                    "tools": [
                        {"name": "run_backtest", "description": "", "inputSchema": {}}
                    ]
                }
            else:
                result = {}
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": body.get("id"), "result": result}
            )
        return httpx.Response(404, json={})


def _local_service(handler: FakeMcpHandler) -> HarnessDataService:
    topology = HarnessMcpTopology(thesis_location="local", harness_location="local")
    return HarnessDataService(
        topology,
        factory=lambda: open_harness_mcp_client(topology, in_process_handler=handler),
    )


def _remote_service(server: FakeHttpMcpServer) -> HarnessDataService:
    topology = HarnessMcpTopology(
        thesis_location="local",
        harness_location="remote",
        harness_url=HARNESS_URL,
        bearer_token=BEARER_TOKEN,
    )
    return HarnessDataService(
        topology,
        factory=lambda: open_harness_mcp_client(
            topology, http_transport=httpx.MockTransport(server.handler)
        ),
    )


def _client(service: HarnessDataService) -> TestClient:
    app = create_app(bearer_token=BEARER_TOKEN, harness_data_service=service)
    return TestClient(app)


def test_status_reports_local_topology_catalog() -> None:
    handler = FakeMcpHandler()
    client = _client(_local_service(handler))

    response = client.get("/v1/harness/status", headers=HEADERS)

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "local"
    assert body["thesis_location"] == "local"
    assert body["harness_location"] == "local"
    assert body["reachable"] is True
    assert body["error"] is None
    assert body["health"] is None
    assert [tool["name"] for tool in body["tools"]] == [
        "run_backtest",
        "get_job_status",
        "get_job_result",
        "list_artifacts",
    ]
    assert body["server"]["serverInfo"]["name"] == "harness"


def test_status_reports_remote_health_and_reachability() -> None:
    client = _client(_remote_service(FakeHttpMcpServer()))

    response = client.get("/v1/harness/status", headers=HEADERS)

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "remote"
    assert body["reachable"] is True
    assert body["health"] == {
        "status": "ok",
        "serverInfo": {"name": "harness", "version": "0.1.0"},
        "tools": ["run_backtest"],
    }
    assert body["tools"][0]["name"] == "run_backtest"


def test_status_marks_unreachable_harness_without_failing() -> None:
    client = _client(_remote_service(FakeHttpMcpServer(status=500)))

    response = client.get("/v1/harness/status", headers=HEADERS)

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "remote"
    assert body["reachable"] is False
    assert isinstance(body["error"], str) and body["error"]
    assert body["tools"] == []


def test_launch_backtest_invokes_run_backtest_tool() -> None:
    handler = FakeMcpHandler()
    client = _client(_local_service(handler))

    response = client.post(
        "/v1/backtests/jobs",
        headers=HEADERS,
        json={"config_path": "config/harness/backtest.yaml", "workspace": "alpha"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["action"] == "backtest"
    assert body["kind"] == "backtest"
    assert body["config_path"] == "config/harness/backtest.yaml"
    assert body["workspace"] == "alpha"
    job_id = body["job_id"]
    assert re.fullmatch(r"job-[0-9a-f]{32}", job_id)
    assert body["state"] == "queued"
    assert body["boundary"] == "remote"
    assert "result" not in body
    assert handler.calls == ["run_backtest"]
    assert handler.arguments[0]["job_id"] == job_id
    assert handler.arguments[0]["config_path"] == "config/harness/backtest.yaml"


def test_launch_backtest_rejects_a_mismatched_acknowledged_job_id() -> None:
    handler = FakeMcpHandler(acknowledged_job_id="job-other")
    client = _client(_local_service(handler))

    response = client.post(
        "/v1/backtests/jobs",
        headers=HEADERS,
        json={"config_path": "config/harness/backtest.yaml"},
    )

    assert response.status_code == 502
    error = response.json()["error"]
    assert error["code"] == "harness_protocol_error"
    submitted = handler.arguments[0]["job_id"]
    assert error["details"]["job_id"] == submitted
    assert handler.calls == ["run_backtest"]


def test_launch_backtest_rejects_an_absent_acknowledged_job_id() -> None:
    handler = FakeMcpHandler(omit_job_id=True)
    client = _client(_local_service(handler))

    response = client.post(
        "/v1/backtests/jobs",
        headers=HEADERS,
        json={"config_path": "config/harness/backtest.yaml"},
    )

    assert response.status_code == 502
    error = response.json()["error"]
    assert error["code"] == "harness_protocol_error"
    assert error["details"]["job_id"] == handler.arguments[0]["job_id"]
    assert handler.calls == ["run_backtest"]


def test_launch_transport_failure_retains_the_submitted_job_id_without_replay() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method != "POST" or request.url.path != "/mcp":
            return httpx.Response(404, json={})
        body = json.loads(request.content)
        if "id" not in body:
            return httpx.Response(202)
        requests.append(body)
        if body.get("method") == "initialize":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": body.get("id"),
                    "result": {"protocolVersion": "2024-11-05"},
                },
            )
        return httpx.Response(500, json={})

    topology = HarnessMcpTopology(
        thesis_location="local",
        harness_location="remote",
        harness_url=HARNESS_URL,
        bearer_token=BEARER_TOKEN,
    )
    service = HarnessDataService(
        topology,
        factory=lambda: open_harness_mcp_client(
            topology, http_transport=httpx.MockTransport(handler)
        ),
    )
    client = _client(service)

    response = client.post(
        "/v1/backtests/jobs",
        headers=HEADERS,
        json={"config_path": "config/harness/backtest.yaml"},
    )

    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "harness_mcp_unreachable"
    retained = error["details"]["job_id"]
    assert re.fullmatch(r"job-[0-9a-f]{32}", retained)
    assert error["details"]["executionCertainty"] == "unknown_outcome"
    tool_calls = [body for body in requests if body.get("method") == "tools/call"]
    assert len(tool_calls) == 1
    params = tool_calls[0]["params"]
    assert isinstance(params, dict)
    arguments = params["arguments"]
    assert isinstance(arguments, dict)
    assert arguments["job_id"] == retained


def test_job_status_returns_structured_payload() -> None:
    client = _client(_local_service(FakeMcpHandler()))

    response = client.get("/v1/jobs/job-1", headers=HEADERS)

    assert response.status_code == 200
    body = response.json()
    assert body["job_id"] == "job-1"
    assert body["state"] == "succeeded"
    assert body["error_message"] is None


def test_job_result_and_artifact_manifest_expose_canonical_shape() -> None:
    client = _client(_local_service(FakeMcpHandler()))

    result = client.get("/v1/jobs/job-1/result", headers=HEADERS)
    assert result.status_code == 200
    assert result.json()["job_id"] == "job-1"
    assert result.json()["state"] == "succeeded"

    artifacts = client.get("/v1/jobs/job-1/artifacts", headers=HEADERS)
    assert artifacts.status_code == 200
    manifest = artifacts.json()
    assert manifest["job_id"] == "job-1"
    assert manifest["artifacts"][0]["uri"].startswith("jobs/")
    assert manifest["artifacts"] == [
        {
            "name": "metrics.json",
            "uri": "jobs/job-1/artifacts/metrics.json",
            "sha256": SHA,
            "sizeBytes": 512,
            "sasUrl": None,
        }
    ]


def test_tool_failure_without_typed_not_found_remains_bad_gateway() -> None:
    client = _client(_local_service(FakeMcpHandler(missing=True)))

    response = client.get("/v1/jobs/job-1/result", headers=HEADERS)

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "harness_tool_error"


def test_harness_routes_require_authentication() -> None:
    client = _client(_local_service(FakeMcpHandler()))

    assert client.get("/v1/harness/status").status_code == 401
    assert client.get("/v1/jobs/job-1").status_code == 401
