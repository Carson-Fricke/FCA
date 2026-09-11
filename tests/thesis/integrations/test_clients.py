from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any, cast

import httpx
import pytest

from lakeclient import (
    AssetClass,
    CoverageResponse,
    DatasetRequest,
    DataType,
    FileQueryFile,
    FileQueryResponse,
    FileQueryResults,
    FileQueryState,
    JobStatus,
    LakeProtocolError,
)
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
from thesis.integrations.lake import (
    LakeCredentials,
    LocalLakeApiAdapter,
    PointInTimeLakeRequest,
    RemoteLakeApiAdapter,
    ThesisLakeClient,
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


def _lake_file(symbol: str, session_date: date) -> FileQueryFile:
    return FileQueryFile(
        asset_class=AssetClass.EQUITY,
        data_type=DataType.QUOTE,
        symbol=symbol,
        session_date=session_date,
        object_path=f"equity/quote/{session_date}/{symbol}.parquet",
        url=f"https://storage.test/{symbol}/{session_date}",
        sas_url=f"https://storage.test/read/{symbol}/{session_date}",
        row_count=2,
        byte_size=128,
    )


class FakeLakePort:
    def __init__(self) -> None:
        self.requests: list[
            tuple[str, DatasetRequest | tuple[DatasetRequest, bool]]
        ] = []

    def ensure(self, request: Any) -> CoverageResponse:
        self.requests.append(("ensure", request))
        return CoverageResponse(
            asset_class=request.asset_class,
            data_type=request.data_type,
            start_date=request.start_date,
            end_date=request.end_date,
            entries=(),
            symbol=request.symbol,
        )

    def wait_for_job(self, job_id: str, **kwargs: object) -> JobStatus:
        raise AssertionError(f"unexpected job poll: {job_id}, {kwargs}")

    def query_files(self, request: Any, *, ensure: bool = False) -> FileQueryResponse:
        self.requests.append(("query", (request, ensure)))
        file = _lake_file(request.symbol, request.start_date)
        return FileQueryResponse(
            files=(file,),
            results=FileQueryResults(1, 1, 1, 0, FileQueryState.COMPLETE),
            limitations=("complete partitions only",),
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


def test_point_in_time_lake_client_uses_published_requests_and_preserves_files() -> (
    None
):
    port = FakeLakePort()
    loaded = ThesisLakeClient(port, reader=lambda files: tuple(files)).load(
        PointInTimeLakeRequest(
            AssetClass.EQUITY,
            DataType.QUOTE,
            (" msft ", "aapl"),
            date(2024, 1, 19),
            date(2024, 1, 19),
            as_of=datetime(2024, 1, 19, 20, 0, tzinfo=UTC),
        )
    )

    assert loaded.request.symbols == ("AAPL", "MSFT")
    assert loaded.projected_bytes == 256
    assert tuple(file.symbol for file in loaded.files) == ("AAPL", "MSFT")
    assert [kind for kind, _ in port.requests] == [
        "ensure",
        "query",
        "ensure",
        "query",
    ]
    ensure_requests = [
        cast(DatasetRequest, request)
        for kind, request in port.requests
        if kind == "ensure"
    ]
    assert all(request.symbol in {"AAPL", "MSFT"} for request in ensure_requests)


def test_lake_request_rejects_a_date_after_point_in_time_boundary() -> None:
    with pytest.raises(ValueError, match="after the as_of date"):
        PointInTimeLakeRequest(
            AssetClass.EQUITY,
            DataType.QUOTE,
            "AAPL",
            date(2024, 1, 19),
            date(2024, 1, 20),
            as_of=datetime(2024, 1, 19, 12, 0, tzinfo=UTC),
        )


def test_option_lake_request_preserves_published_contract_selector() -> None:
    request = PointInTimeLakeRequest(
        AssetClass.OPTION,
        DataType.TRADE,
        "SPY240119C00500000",
        date(2024, 1, 19),
        date(2024, 1, 19),
    )

    published = request.dataset_requests()[0]

    assert published.symbol == "SPY240119C00500000"
    assert published.contract_symbols == ("SPY240119C00500000",)


def test_local_and_remote_lake_api_adapters_inject_transport_and_credentials() -> None:
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.headers.get("Authorization")))
        return httpx.Response(200, json={"unexpected": True})

    local = LocalLakeApiAdapter(
        "http://127.0.0.1:8000",
        LakeCredentials("local-secret"),
        transport=httpx.MockTransport(handler),
    )
    remote = RemoteLakeApiAdapter(
        "https://lake.test",
        LakeCredentials("lake-secret"),
        transport=httpx.MockTransport(handler),
    )
    request = PointInTimeLakeRequest(
        AssetClass.EQUITY,
        DataType.QUOTE,
        "AAPL",
        date(2024, 1, 19),
        date(2024, 1, 19),
    ).dataset_requests()[0]
    with pytest.raises(LakeProtocolError):
        local.ensure(request)
    with pytest.raises(LakeProtocolError):
        remote.ensure(request)
    local.close()
    remote.close()

    assert seen == [
        ("/v1/catalog/ensure", "Bearer local-secret"),
        ("/v1/catalog/ensure", "Bearer lake-secret"),
    ]
    assert (
        repr(LakeCredentials("lake-secret"))
        == "LakeCredentials(bearer_token='<redacted>')"
    )


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


def test_api_base_urls_reject_embedded_credentials() -> None:
    with pytest.raises(ValueError, match="service URL"):
        LocalLakeApiAdapter(
            "http://user:secret@127.0.0.1:8000",
            LakeCredentials("lake-secret"),
        )
