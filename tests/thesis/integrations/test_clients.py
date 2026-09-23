from __future__ import annotations

import json
from collections.abc import Mapping

import httpx
import pytest

from thesis.integrations.capabilities import CapabilityStatus
from thesis.integrations.harness import (
    BacktestRequest,
    HarnessCapabilityUnavailable,
    HarnessCredentials,
    LocalHarnessApiAdapter,
    LocalHarnessProcessAdapter,
    ProcessResult,
    RemoteHarnessApiAdapter,
    ThesisHarnessClient,
)
from thesis.integrations.mcp import (
    TOOL_GET_BACKTEST_STATUS,
    TOOL_GET_RUN,
    TOOL_LIST_ARTIFACTS,
    TOOL_RUN_BACKTEST,
    TOOL_RUN_SWEEP,
    HarnessMcpClient,
    InProcessMcpTransport,
)


def _capability_body() -> dict[str, object]:
    return {
        "capabilities": {
            "backtest": {"available": True, "reason": "available"},
            "result_query": {"available": True, "reason": "available"},
            "manifests": {"available": True, "reason": "available"},
            "metrics": {"available": True, "reason": "available"},
            "result_artifacts": {"available": True, "reason": "available"},
        }
    }


def _backtest_body() -> dict[str, object]:
    return {
        "job_id": "run-17",
        "state": "complete",
        "manifest": {"configuration_hash": "abc"},
        "metrics": {"total_return": "0.12"},
        "result_artifacts": [{"name": "equity.parquet", "sha256": "def"}],
        "capabilities": _capability_body()["capabilities"],
    }


class FakeProcessTransport:
    def __init__(self, *, implemented: bool = True) -> None:
        self.implemented = implemented
        self.calls: list[tuple[str, Mapping[str, object], float]] = []

    def run(
        self,
        command: str,
        payload: Mapping[str, object],
        *,
        timeout: float,
    ) -> ProcessResult:
        self.calls.append((command, payload, timeout))
        if not self.implemented:
            return ProcessResult(2, "", "scaffolded but not implemented yet")
        if payload["operation"] == "capabilities":
            return ProcessResult(0, json.dumps(_capability_body()))
        return ProcessResult(0, json.dumps(_backtest_body()))


def test_local_harness_process_adapter_launches_and_queries_without_engine_imports() -> (
    None
):
    transport = FakeProcessTransport()
    client = ThesisHarnessClient(LocalHarnessProcessAdapter(transport, timeout=4))
    request = BacktestRequest(
        "run-17", {"shell": "backtest"}, metadata={"source": "test"}
    )

    launched = client.launch_backtest(request)
    queried = client.query_backtest("run-17")

    assert launched.manifest == _backtest_body()["manifest"]
    assert launched.metrics == _backtest_body()["metrics"]
    assert launched.result_artifacts == _backtest_body()["result_artifacts"]
    assert queried.job_id == "run-17"
    assert transport.calls[0][0] == "api"
    assert transport.calls[2][0] == "backtest-job"
    assert transport.calls[2][1]["request"] == request.to_payload()
    assert transport.calls[5][0] == "api"


def test_unimplemented_local_harness_is_unavailable_and_never_emulated() -> None:
    transport = FakeProcessTransport(implemented=False)
    client = ThesisHarnessClient(LocalHarnessProcessAdapter(transport))

    report = client.capabilities()
    with pytest.raises(HarnessCapabilityUnavailable) as caught:
        client.launch_backtest(BacktestRequest("run-17", {}))

    assert not report.status("backtest").available
    assert report.status("backtest").reason == "not_implemented"
    assert caught.value.capability == "backtest"
    assert [call[1]["operation"] for call in transport.calls] == [
        "capabilities",
        "capabilities",
    ]


def _artifact_manifest() -> list[dict[str, object]]:
    return [
        {
            "name": "backtest.json",
            "uri": "backtests/run-17/backtest.json",
            "sha256": "a" * 64,
            "sizeBytes": 2,
            "sas_url": None,
        }
    ]


def _canonical_handler(seen: list[tuple[str, str | None, object | None]]):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        seen.append((path, request.headers.get("Authorization"), body))
        if path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        if request.method == "POST" and path == "/v1/backtests/jobs":
            return httpx.Response(202, json={"job_id": "run-17", "state": "succeeded"})
        if path == "/v1/backtests/jobs/run-17":
            return httpx.Response(
                200,
                json={
                    "job_id": "run-17",
                    "state": "succeeded",
                    "created_at": "2026-09-08T15:04:05Z",
                    "updated_at": "2026-09-08T15:06:09Z",
                    "error_message": None,
                },
            )
        if path == "/v1/backtests/runs/run-17":
            return httpx.Response(
                200,
                json={
                    "run_id": "run-17",
                    "status": "complete",
                    "strategy_id": "spy",
                    "run_digest": None,
                    "configuration_digest": None,
                    "artifact_count": 1,
                    "total_bytes": 2,
                    "metrics": {"total_return": "0.12"},
                },
            )
        if path == "/v1/backtests/runs/run-17/artifacts":
            return httpx.Response(
                200,
                json={"run_id": "run-17", "artifacts": _artifact_manifest()},
            )
        return httpx.Response(404, json={})

    return handler


def test_remote_harness_api_uses_canonical_routes_and_manifest() -> None:
    seen: list[tuple[str, str | None, object | None]] = []
    transport = httpx.MockTransport(_canonical_handler(seen))
    client = ThesisHarnessClient(
        RemoteHarnessApiAdapter(
            "https://harness.test",
            HarnessCredentials("harness-secret"),
            transport=transport,
        )
    )

    result = client.query_backtest("run-17")

    assert [call[0] for call in seen] == [
        "/healthz",
        "/healthz",
        "/v1/backtests/jobs/run-17",
        "/v1/backtests/runs/run-17",
        "/v1/backtests/runs/run-17/artifacts",
    ]
    assert seen[2][1] == "Bearer harness-secret"
    assert result.job_id == "run-17"
    assert result.state == "succeeded"
    assert result.metrics == {"total_return": "0.12"}
    assert result.result_artifacts == _artifact_manifest()
    assert result.manifest == {
        "manifestId": "run-17",
        "artifacts": _artifact_manifest(),
    }
    assert (
        repr(HarnessCredentials("harness-secret"))
        == "HarnessCredentials(bearer_token='<redacted>')"
    )


def test_local_harness_api_launches_the_canonical_configuration_document() -> None:
    seen: list[tuple[str, str | None, object | None]] = []
    transport = httpx.MockTransport(_canonical_handler(seen))
    client = ThesisHarnessClient(
        LocalHarnessApiAdapter("http://127.0.0.1:9000", transport=transport)
    )

    result = client.launch_backtest(BacktestRequest("run-17", {"seed": 7}))

    assert [call[0] for call in seen] == [
        "/healthz",
        "/healthz",
        "/v1/backtests/jobs",
    ]
    assert seen[2][2] == {"run_id": "run-17", "seed": 7}
    assert result.state == "succeeded"
    assert result.manifest is None
    assert result.metrics is None
    assert result.result_artifacts is None
    assert result.capabilities.status("metrics").available
    assert result.capabilities.status("result_artifacts").available


def test_remote_harness_missing_health_capability_is_not_emulated() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        RemoteHarnessApiAdapter("http://harness.test", HarnessCredentials("secret"))

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(404, json={})

    client = ThesisHarnessClient(
        RemoteHarnessApiAdapter(
            "https://harness.test",
            HarnessCredentials("secret"),
            transport=httpx.MockTransport(handler),
        )
    )
    with pytest.raises(HarnessCapabilityUnavailable) as caught:
        client.launch_backtest(BacktestRequest("run-17", {}))

    assert caught.value.report.status("backtest") == CapabilityStatus(
        "backtest", False, "not_implemented"
    )


class _FakeMcpHandler:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def handle(self, request: object) -> dict[str, object] | None:
        if not isinstance(request, Mapping):
            raise TypeError("request must be a mapping")
        message = dict(request)
        self.calls.append(message)
        method = message.get("method")
        if method == "notifications/initialized":
            return None
        result: dict[str, object]
        if method == "tools/list":
            result = {
                "tools": [
                    {"name": name, "description": "", "inputSchema": {}}
                    for name in (
                        TOOL_RUN_BACKTEST,
                        TOOL_GET_BACKTEST_STATUS,
                        TOOL_GET_RUN,
                        TOOL_LIST_ARTIFACTS,
                        TOOL_RUN_SWEEP,
                    )
                ]
            }
        elif method == "tools/call":
            params = message["params"]
            assert isinstance(params, dict)
            result = {
                "isError": False,
                "structuredContent": {
                    "tool": params["name"],
                    "arguments": params["arguments"],
                },
            }
        else:
            result = {"serverInfo": {}}
        return {"jsonrpc": "2.0", "id": message.get("id"), "result": result}


def test_harness_mcp_client_exposes_the_canonical_query_tools() -> None:
    client = HarnessMcpClient(InProcessMcpTransport(_FakeMcpHandler()))
    client.initialize()
    names = {tool.name for tool in client.list_tools()}
    assert names == {
        TOOL_RUN_BACKTEST,
        TOOL_GET_BACKTEST_STATUS,
        TOOL_GET_RUN,
        TOOL_LIST_ARTIFACTS,
        TOOL_RUN_SWEEP,
    }

    status = client.get_backtest_status(job_id="run-17")
    run = client.get_run(run_id="run-17")
    artifacts = client.list_artifacts(run_id="run-17")
    sweep = client.run_sweep(spec_path="spec.json")

    assert status.structured["arguments"] == {"job_id": "run-17"}
    assert run.structured["arguments"] == {"run_id": "run-17"}
    assert artifacts.structured["arguments"] == {"run_id": "run-17"}
    assert sweep.structured["arguments"] == {"spec_path": "spec.json"}
