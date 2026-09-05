"""Focused tests for the quant-harness launch and job status routes."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

from fastapi.testclient import TestClient

from harness.configuration.loader import load_yaml_mapping
from harness.http.application import create_app
from harness.http.launcher import (
    BacktestJobLauncher,
    BacktestLaunchJob,
    parse_backtest_configuration,
    parse_backtest_configuration_json,
)
from harness.results.store import (
    BacktestResultsStore,
    InMemoryRunBlobStore,
    InMemoryRunCatalogRepository,
)

BEARER: Final = {"Authorization": "Bearer test-secret"}
CONFIG_ROOT: Final = Path(__file__).parents[3] / "config" / "harness"
RUN_ID: Final = "momentum-spy-2024-01"
JSON_HEADERS: Final = {**BEARER, "Content-Type": "application/json"}


def _json_ready(value: Any) -> Any:
    """Map the YAML scalar conventions onto their JSON client spellings.

    Typed decimals travel as JSON numbers and string-exact parameter values
    stay strings, so the benchmark fixture survives the exact-decimal JSON
    decoding that the launch route applies.
    """
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    return value


def configuration_document() -> dict[str, Any]:
    """Return the benchmark configuration with the reference strategy."""
    values = load_yaml_mapping(CONFIG_ROOT / "backtest.yaml")
    values["strategy_instances"][0]["implementation"] = (
        "harness.strategy.reference:ReferenceStrategy"
    )
    return values


def configuration_body() -> bytes:
    """Return the benchmark configuration as one JSON request body."""
    return json.dumps(_json_ready(configuration_document())).encode("utf-8")


def configuration_payload() -> dict[str, Any]:
    """Return the benchmark configuration as a decoded client payload."""
    return json.loads(configuration_body())


def make_client(
    launcher: BacktestJobLauncher | None = None,
) -> tuple[TestClient, BacktestResultsStore]:
    store = BacktestResultsStore(InMemoryRunBlobStore(), InMemoryRunCatalogRepository())
    if launcher is None:
        application = create_app(bearer_token="test-secret", results_store=store)
    else:
        application = create_app(
            bearer_token="test-secret",
            results_store=store,
            job_launcher=launcher,
        )
    return TestClient(application), store


def test_health_is_unauthenticated() -> None:
    client, _ = make_client()

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_launch_and_job_routes_reject_missing_credentials() -> None:
    client, _ = make_client()

    denied_launch = client.post("/v1/backtests/jobs", json={})
    denied_status = client.get(f"/v1/backtests/jobs/{RUN_ID}")

    assert denied_launch.status_code == 401
    assert denied_status.status_code == 401


def test_dev_launch_completes_and_becomes_queryable_end_to_end() -> None:
    client, store = make_client()

    launch = client.post(
        "/v1/backtests/jobs",
        content=configuration_body(),
        headers=JSON_HEADERS,
    )

    assert launch.status_code == 202, launch.text
    assert launch.json() == {"job_id": RUN_ID, "state": "succeeded"}
    assert launch.headers["location"] == f"/v1/backtests/jobs/{RUN_ID}"

    status = client.get(f"/v1/backtests/jobs/{RUN_ID}", headers=BEARER)
    assert status.status_code == 200
    job = status.json()
    assert job["state"] == "succeeded"
    assert job["error_message"] is None

    run = client.get(f"/v1/backtests/runs/{RUN_ID}", headers=BEARER)
    assert run.status_code == 200
    row = run.json()
    assert row["run_id"] == RUN_ID
    assert row["status"] == "complete"
    assert row["strategy_id"] == "spy-momentum"
    assert row["configuration_digest"] is not None
    assert row["artifact_count"] == 1
    assert store.get(RUN_ID) is not None

    artifacts = client.get(f"/v1/backtests/runs/{RUN_ID}/artifacts", headers=BEARER)
    assert artifacts.status_code == 200
    assert [a["name"] for a in artifacts.json()["artifacts"]] == ["backtest.json"]

    artifact = client.get(
        f"/v1/backtests/runs/{RUN_ID}/artifacts/backtest.json", headers=BEARER
    )
    assert artifact.status_code == 200
    document = json.loads(artifact.content)
    assert document["manifest"]["run_id"] == RUN_ID
    assert document["status"] == "completed"


def test_failed_launch_is_recorded_on_the_job_and_leaves_no_run() -> None:
    payload = configuration_payload()
    payload["strategy_instances"][0]["implementation"] = (
        "harness.broken_module:MissingStrategy"
    )
    client, store = make_client()

    launch = client.post("/v1/backtests/jobs", json=payload, headers=BEARER)

    assert launch.status_code == 202, launch.text
    assert launch.json() == {"job_id": RUN_ID, "state": "failed"}

    status = client.get(f"/v1/backtests/jobs/{RUN_ID}", headers=BEARER)
    assert status.status_code == 200
    job = status.json()
    assert job["state"] == "failed"
    assert "StrategyLoadError" in job["error_message"]

    run = client.get(f"/v1/backtests/runs/{RUN_ID}", headers=BEARER)
    assert run.status_code == 404
    assert store.get(RUN_ID) is None


def test_launch_rejects_unknown_fields_and_non_object_documents() -> None:
    client, _ = make_client()

    payload = configuration_payload()
    payload["unexpected"] = True
    extra = client.post("/v1/backtests/jobs", json=payload, headers=BEARER)
    malformed = client.post(
        "/v1/backtests/jobs", content=b"[1, 2]", headers=JSON_HEADERS
    )

    assert extra.status_code == 422
    assert extra.json()["error"]["code"] == "validation_error"
    assert malformed.status_code == 422
    assert malformed.json()["error"]["code"] == "validation_error"


def test_launch_rejects_a_wrong_shell_document() -> None:
    client, _ = make_client()

    response = client.post(
        "/v1/backtests/jobs",
        json={"schema_version": 1, "shell": "trader"},
        headers=BEARER,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_parse_backtest_configuration_rejects_other_shells() -> None:
    document = load_yaml_mapping(CONFIG_ROOT / "api.yaml")

    try:
        parse_backtest_configuration(document)
    except ValueError as error:
        assert "shell must be 'backtest'" in str(error)
    else:
        raise AssertionError("an api document must not parse as a backtest")


def test_numeric_json_values_decode_as_exact_decimals() -> None:
    payload = configuration_payload()
    payload["strategy_instances"][0]["parameters"]["threshold"] = 0.05

    configuration = parse_backtest_configuration_json(json.dumps(payload).encode())

    assert configuration.strategy_instances[0].parameters["threshold"] == Decimal(
        "0.05"
    )


def test_injected_launcher_keeps_asynchronous_job_semantics() -> None:
    received: list[BacktestLaunchJob] = []

    async def launcher(job: BacktestLaunchJob) -> None:
        received.append(job)

    client, _ = make_client(launcher=launcher)

    launch = client.post(
        "/v1/backtests/jobs",
        content=configuration_body(),
        headers=JSON_HEADERS,
    )

    assert launch.status_code == 202
    assert launch.json() == {"job_id": RUN_ID, "state": "queued"}
    assert [job.configuration.run_id for job in received] == [RUN_ID]


def test_job_status_returns_404_for_an_unknown_job() -> None:
    client, _ = make_client()

    response = client.get("/v1/backtests/jobs/unknown", headers=BEARER)

    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "job_not_found",
            "message": "the requested job does not exist",
        }
    }


def test_parse_backtest_configuration_json_is_exact() -> None:
    from harness.http.launcher import parse_backtest_configuration_json

    configuration = parse_backtest_configuration_json(configuration_body())

    assert configuration.shell == "backtest"
    assert configuration.run_id == RUN_ID
    assert configuration.initial_cash == Decimal("100000.00")
    assert configuration.strategy_instances[0].parameters["threshold"] == "0.05"
