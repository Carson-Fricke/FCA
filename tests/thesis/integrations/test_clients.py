from __future__ import annotations

import json

import httpx

from thesis.integrations.harness import (
    HarnessCredentials,
    RemoteHarnessApiAdapter,
    ThesisHarnessClient,
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
