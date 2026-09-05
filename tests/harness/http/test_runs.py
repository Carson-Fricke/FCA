"""Focused tests for the quant-harness run query routes."""

from __future__ import annotations

from datetime import date
from typing import Final

import pytest
from fastapi.testclient import TestClient

from harness.http.application import create_app
from harness.results.store import (
    BacktestResultsStore,
    InMemoryRunBlobStore,
    InMemoryRunCatalogRepository,
    RunCatalogRecord,
    RunStatus,
)

BEARER: Final = {"Authorization": "Bearer test-secret"}


class RecordingSasIssuer:
    """Deterministic SAS issuer that records every scoped object path."""

    def __init__(self) -> None:
        self.paths: list[str] = []

    def issue(self, object_path: str) -> str:
        self.paths.append(object_path)
        return f"https://storage.example/{object_path}?sig=abc"


def make_record(
    run_id: str = "run-1",
    *,
    status: RunStatus = RunStatus.COMPLETE,
    strategy_id: str = "momentum",
) -> RunCatalogRecord:
    return RunCatalogRecord(
        run_id=run_id,
        status=status,
        strategy_id=strategy_id,
        data_start=date(2024, 1, 2),
        data_end=date(2024, 1, 3),
        metrics={"sharpe": "1.2345", "max_drawdown": "-0.0825"},
    )


def make_client(
    sas_issuer: RecordingSasIssuer | None = None,
    bearer_token: str | None = "test-secret",
) -> tuple[TestClient, BacktestResultsStore]:
    store = BacktestResultsStore(InMemoryRunBlobStore(), InMemoryRunCatalogRepository())
    store.put(
        make_record(),
        {"fills.parquet": b"fills-bytes", "actions.parquet": b"actions-bytes"},
    )
    store.put(
        make_record("run-2", status=RunStatus.FAILED, strategy_id="mean-reversion"),
        {"backtest.json": b"{}"},
    )
    application = create_app(
        bearer_token=bearer_token,
        results_store=store,
        sas_issuer=sas_issuer,
    )
    return TestClient(application), store


def test_health_is_unauthenticated_and_has_probe_alias() -> None:
    client, _ = make_client()

    for path in ("/health", "/healthz"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/v1/backtests/runs"),
        ("get", "/v1/backtests/runs/run-1"),
        ("get", "/v1/backtests/runs/run-1/artifacts"),
        ("delete", "/v1/backtests/runs/run-1"),
    ],
)
@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong-secret"},
        {"Authorization": "Basic test-secret"},
    ],
)
def test_routes_reject_missing_or_invalid_credentials(
    method: str, path: str, headers: dict[str, str]
) -> None:
    client, _ = make_client()

    response = getattr(client, method)(path, headers=headers)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {
        "error": {
            "code": "unauthorized",
            "message": "a valid bearer token is required",
        }
    }


def test_list_runs_returns_catalog_rows_sorted_by_run_id() -> None:
    client, _ = make_client()

    response = client.get("/v1/backtests/runs", headers=BEARER)

    assert response.status_code == 200
    runs = response.json()["runs"]
    assert [run["run_id"] for run in runs] == ["run-1", "run-2"]
    assert len(runs[0]["run_digest"]) == 64
    assert runs[0] == {
        "run_id": "run-1",
        "status": "complete",
        "strategy_id": "momentum",
        "data_start": "2024-01-02",
        "data_end": "2024-01-03",
        "run_digest": runs[0]["run_digest"],
        "configuration_digest": None,
        "artifact_count": 2,
        "total_bytes": len(b"fills-bytes") + len(b"actions-bytes"),
        "metrics": {"sharpe": "1.2345", "max_drawdown": "-0.0825"},
    }


def test_list_runs_filters_by_status_and_strategy() -> None:
    client, _ = make_client()

    failed = client.get(
        "/v1/backtests/runs", params={"status": "failed"}, headers=BEARER
    )
    momentum = client.get(
        "/v1/backtests/runs", params={"strategy_id": "momentum"}, headers=BEARER
    )
    both = client.get(
        "/v1/backtests/runs",
        params={"status": "failed", "strategy_id": "momentum"},
        headers=BEARER,
    )

    assert failed.status_code == 200
    assert [run["run_id"] for run in failed.json()["runs"]] == ["run-2"]
    assert momentum.status_code == 200
    assert [run["run_id"] for run in momentum.json()["runs"]] == ["run-1"]
    assert both.status_code == 200
    assert both.json()["runs"] == []


def test_list_runs_rejects_an_unknown_status_value() -> None:
    client, _ = make_client()

    response = client.get(
        "/v1/backtests/runs", params={"status": "archived"}, headers=BEARER
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_get_run_returns_the_catalog_row_or_404() -> None:
    client, _ = make_client()

    found = client.get("/v1/backtests/runs/run-1", headers=BEARER)
    missing = client.get("/v1/backtests/runs/unknown", headers=BEARER)

    assert found.status_code == 200
    assert found.json()["run_id"] == "run-1"
    assert missing.status_code == 404
    assert missing.json() == {
        "error": {
            "code": "run_not_found",
            "message": "the requested backtest run does not exist",
        }
    }


def test_list_artifacts_reports_names_sizes_and_object_paths() -> None:
    client, _ = make_client()

    response = client.get("/v1/backtests/runs/run-1/artifacts", headers=BEARER)

    assert response.status_code == 200
    artifacts = response.json()["artifacts"]
    assert [artifact["name"] for artifact in artifacts] == [
        "actions.parquet",
        "fills.parquet",
    ]
    assert artifacts[0]["size"] == len(b"actions-bytes")
    assert artifacts[0]["object_path"] == "backtests/run-1/actions.parquet"
    assert artifacts[0]["sas_url"] is None


def test_list_artifacts_issues_scoped_sas_urls_when_configured() -> None:
    issuer = RecordingSasIssuer()
    client, _ = make_client(sas_issuer=issuer)

    response = client.get("/v1/backtests/runs/run-1/artifacts", headers=BEARER)

    assert response.status_code == 200
    artifacts = response.json()["artifacts"]
    assert issuer.paths == [
        "backtests/run-1/actions.parquet",
        "backtests/run-1/fills.parquet",
    ]
    assert artifacts[1]["sas_url"] == (
        "https://storage.example/backtests/run-1/fills.parquet?sig=abc"
    )


def test_download_artifact_returns_exact_payload_bytes() -> None:
    client, _ = make_client()

    response = client.get(
        "/v1/backtests/runs/run-1/artifacts/fills.parquet", headers=BEARER
    )

    assert response.status_code == 200
    assert response.content == b"fills-bytes"
    assert response.headers["content-type"] == "application/octet-stream"


def test_download_artifact_rejects_unknown_runs_and_artifacts() -> None:
    client, _ = make_client()

    missing_run = client.get(
        "/v1/backtests/runs/unknown/artifacts/fills.parquet", headers=BEARER
    )
    missing_artifact = client.get(
        "/v1/backtests/runs/run-1/artifacts/missing.parquet", headers=BEARER
    )

    assert missing_run.status_code == 404
    assert missing_run.json()["error"]["code"] == "run_not_found"
    assert missing_artifact.status_code == 404
    assert missing_artifact.json()["error"]["code"] == "artifact_not_found"


def test_delete_run_removes_the_row_and_artifacts_together() -> None:
    client, store = make_client()

    removed = client.delete("/v1/backtests/runs/run-1", headers=BEARER)
    gone = client.get("/v1/backtests/runs/run-1", headers=BEARER)
    repeat = client.delete("/v1/backtests/runs/run-1", headers=BEARER)

    assert removed.status_code == 204
    assert removed.content == b""
    assert gone.status_code == 404
    assert repeat.status_code == 404
    assert [record.run_id for record in store.list()] == ["run-2"]


def test_app_reads_the_runtime_token_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HARNESS_API__BEARER_TOKEN", "environment-secret")
    store = BacktestResultsStore(InMemoryRunBlobStore(), InMemoryRunCatalogRepository())
    client = TestClient(create_app(results_store=store))

    response = client.get(
        "/v1/backtests/runs",
        headers={"Authorization": "Bearer environment-secret"},
    )

    assert response.status_code == 200


def test_authenticator_fails_closed_without_a_configured_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HARNESS_API__BEARER_TOKEN", raising=False)
    store = BacktestResultsStore(InMemoryRunBlobStore(), InMemoryRunCatalogRepository())
    client = TestClient(create_app(results_store=store))

    response = client.get(
        "/v1/backtests/runs",
        headers={"Authorization": "Bearer any-value"},
    )

    assert response.status_code == 401
