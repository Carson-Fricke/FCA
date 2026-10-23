"""Workspace catalogs expose retained executions and authored strategy programs."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.thesis.submissions import MemorySubmissions
from tests.thesis.workspaces._support import MemoryGitFactory, Repositories

from thesis.http.application import create_app
from thesis.http.harness import HarnessDataService
from thesis.http.workspaces import build_local_workspace_data
from thesis.integrations.mcp import HarnessMcpTopology, open_harness_mcp_client
from thesis.strategies import artifacts as strategy_artifacts
from thesis.workspaces import programs as program_module

HEADERS = {"Authorization": "Bearer catalog-test"}
WORKSPACE = "catalog"
SOURCE = "// exact frozen source\nstruct Strategy;"
CONFIG = {
    "shell": "backtest",
    "start": "2026-01-01T00:00:00Z",
    "end": "2026-08-01T00:00:00Z",
    "strategy_instances": [
        {
            "instance_id": "slot-1",
            "bundle_hash": "a" * 64,
            "parameters": {"source": SOURCE, "lookback": 4},
        }
    ],
}


class Harness:
    """Fake only the external MCP boundary; all HTTP/catalog services are real."""

    def __init__(self) -> None:
        self.states = {
            "job-queued": "queued",
            "job-running": "running",
            "job-failed": "failed",
        }
        self.fail = False
        self.calls: list[tuple[str, str]] = []

    def handle(self, request: object) -> dict[str, object] | None:
        assert isinstance(request, Mapping)
        if "id" not in request:
            return None
        if request.get("method") == "initialize":
            result: dict[str, object] = {
                "protocolVersion": "2024-11-05",
                "serverInfo": {"name": "harness", "version": "test"},
            }
        elif request.get("method") == "tools/call":
            params = request["params"]
            assert isinstance(params, Mapping)
            name = str(params["name"])
            args = params["arguments"]
            assert isinstance(args, Mapping)
            job_id = str(args["job_id"])
            self.calls.append((name, job_id))
            assert name in {"get_job_status", "list_artifacts", "read_artifact"}, (
                "catalog reads must never submit or replay jobs"
            )
            if self.fail:
                raise OSError("status service unavailable")
            result = {
                "structuredContent": {
                    "job_id": job_id,
                    "kind": "backtest",
                    "state": self.states[job_id],
                    "created_at": "2026-10-20T20:00:00Z",
                    "updated_at": "2026-10-20T20:03:00Z",
                    "accepted_input": {**CONFIG, "job_id": job_id},
                    "strategy_id": "momentum",
                    "progress": []
                    if self.states[job_id] == "queued"
                    else [
                        {
                            "stage": "compiling",
                            "started_at": "2026-10-20T20:01:00Z",
                            "updated_at": "2026-10-20T20:01:00Z",
                        }
                    ],
                    "error_message": "ownership lost"
                    if self.states[job_id] == "failed"
                    else None,
                    "metrics": {},
                },
                "content": [],
            }
            if name == "list_artifacts":
                result["structuredContent"] = {
                    "job_id": job_id,
                    "artifacts": [
                        {
                            "name": "backtest.json",
                            "sizeBytes": 1024,
                            "sha256": "b" * 64,
                            "uri": "jobs/" + job_id + "/backtest.json",
                        }
                    ],
                }
            elif name == "read_artifact":
                assert args["name"] == "backtest.json"
                result["structuredContent"] = {
                    "job_id": job_id,
                    "name": "backtest.json",
                    "content": json.dumps(
                        {
                            "portfolio": {
                                "positions": [{"symbol": "MSFT", "quantity": "0"}]
                            },
                            "custom_metrics": {"episodes": "1"},
                            "fills": [
                                {"order_id": "entry", "quantity": "4"},
                                {"order_id": "entry", "quantity": "117"},
                            ],
                        }
                    ),
                    "encoding": "utf-8",
                }
        else:
            result = {"tools": []}
        return {"jsonrpc": "2.0", "id": request["id"], "result": result}


def _client(harness: Harness) -> tuple[TestClient, MemorySubmissions]:
    submissions = MemorySubmissions()
    for job_id in harness.states:
        submissions.retain(
            WORKSPACE,
            job_id,
            "run_backtest",
            {
                "config": {**CONFIG, "job_id": job_id},
                "attribution": {"programId": "momentum"},
            },
        )
    submissions.retain(WORKSPACE, "job-study", "run_study", {"config": {}})
    submissions.retain("another", "job-foreign", "run_backtest", {"config": {}})
    topology = HarnessMcpTopology(thesis_location="local", harness_location="local")
    data = build_local_workspace_data(submissions=submissions)
    service = HarnessDataService(
        topology,
        factory=lambda: open_harness_mcp_client(topology, in_process_handler=harness),
    )
    client = TestClient(
        create_app(
            bearer_token="catalog-test",
            workspace_data=data,
            harness_data_service=service,
        )
    )
    client.post(
        "/v1/workspaces", headers=HEADERS, json={"name": "Catalog"}
    ).raise_for_status()
    return client, submissions


def test_backtests_are_visible_before_report_import_with_actual_state_and_frozen_inputs() -> (
    None
):
    harness = Harness()
    client, submissions = _client(harness)
    page = client.get(f"/v1/workspaces/{WORKSPACE}/backtests", headers=HEADERS)
    assert page.status_code == 200
    payload = page.json()
    assert payload["total"] == 3
    rows = {row["job_id"]: row for row in payload["items"]}
    assert {row["state"] for row in rows.values()} == {"queued", "running", "failed"}
    assert rows["job-failed"]["error_message"] == "ownership lost"
    assert rows["job-running"]["queue_seconds"] == 60
    assert rows["job-running"]["programs"][0]["parameters"] == {"lookback": 4}
    assert rows["job-running"]["programs"][0]["strategy_id"] == "momentum"
    assert rows["job-running"]["programs"][0]["instance_id"] == "slot-1"
    assert "source" not in rows["job-running"]["programs"][0]
    assert client.app.state.backtest_service.store.list(WORKSPACE) == ()
    assert (
        client.get(f"/v1/workspaces/{WORKSPACE}/snapshot", headers=HEADERS).json()[
            "counts"
        ]["backtests"]
        == 3
    )
    filtered = client.get(
        f"/v1/workspaces/{WORKSPACE}/backtests?strategy_id=momentum", headers=HEADERS
    ).json()
    assert filtered["total"] == 3
    inspected = client.get(
        f"/v1/workspaces/{WORKSPACE}/backtests/jobs/job-running/inspection",
        headers=HEADERS,
    ).json()
    assert inspected["programs"][0]["source"] == SOURCE
    assert inspected["accepted_input"]["job_id"] == "job-running"
    assert (
        client.get(
            f"/v1/workspaces/{WORKSPACE}/backtests/jobs/job-foreign/inspection",
            headers=HEADERS,
        ).status_code
        == 404
    )
    second = client.get(
        f"/v1/workspaces/{WORKSPACE}/backtests?offset=1&limit=1", headers=HEADERS
    ).json()
    assert second["total"] == 3 and len(second["items"]) == 1
    assert (
        client.get(
            f"/v1/workspaces/{WORKSPACE}/backtests/job-foreign", headers=HEADERS
        ).status_code
        == 404
    )
    submissions.retain(
        WORKSPACE,
        "job-anonymous",
        "run_backtest",
        {
            "config": {
                **CONFIG,
                "job_id": "job-anonymous",
                "strategy_instances": [
                    {"instance_id": "momentum", "parameters": {"source": SOURCE}}
                ],
            }
        },
    )
    # An execution-local instance name cannot invent an authored-program link.
    assert (
        client.get(
            f"/v1/workspaces/{WORKSPACE}/backtests?strategy_id=momentum",
            headers=HEADERS,
        ).json()["total"]
        == 3
    )


def test_status_failure_preserves_last_observation_and_never_asserts_execution_failure() -> (
    None
):
    harness = Harness()
    client, _ = _client(harness)
    path = f"/v1/workspaces/{WORKSPACE}/backtests/jobs/job-running/inspection"
    first = client.get(path, headers=HEADERS).json()
    harness.fail = True
    second = client.get(path, headers=HEADERS).json()
    assert first["state"] == second["state"] == "running"
    assert second["observed_at"] == first["observed_at"]
    assert second["status_error"]
    assert second["error_message"] is None


def test_strategy_page_lists_authored_programs_and_reads_their_actual_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git = MemoryGitFactory()
    repositories = Repositories(tmp_path, git)
    monkeypatch.setattr(program_module, "GitCoordinator", git)
    monkeypatch.setattr(strategy_artifacts, "GitCoordinator", git)
    data = build_local_workspace_data(workspace_repositories=repositories)
    assert data.programs is not None
    receipt = data.programs.author(
        WORKSPACE,
        "strategy",
        "momentum",
        configuration={
            "start": CONFIG["start"],
            "end": CONFIG["end"],
            "subscriptions": [
                {"data_type": "bars", "symbols": ["MSFT"], "interval": "15min"}
            ],
        },
    )
    client = TestClient(create_app(bearer_token="catalog-test", workspace_data=data))
    client.post(
        "/v1/workspaces", headers=HEADERS, json={"name": "Catalog"}
    ).raise_for_status()
    items = client.get(
        f"/v1/workspaces/{WORKSPACE}/strategies", headers=HEADERS
    ).json()["items"]
    assert [item["id"] for item in items] == ["momentum"]
    assert items[0]["version"] == receipt["version"]
    assert items[0]["status"] == "draft"
    assert items[0]["return_pct"] is None
    assert items[0]["inputs"][0]["symbols"] == ["MSFT"]
    # The shared artifact view must not mutate cached program identities.
    assert (
        client.get(f"/v1/workspaces/{WORKSPACE}/artifacts", headers=HEADERS).status_code
        == 200
    )
    again = client.get(
        f"/v1/workspaces/{WORKSPACE}/strategies", headers=HEADERS
    ).json()["items"]
    assert [item["id"] for item in again] == ["momentum"]
    detail = client.get(
        f"/v1/workspaces/{WORKSPACE}/strategies/momentum", headers=HEADERS
    )
    assert detail.status_code == 200
    assert "impl StrategyProgram" in detail.json()["code"]
    assert json.loads(detail.json()["manifest_text"])["id"] == "momentum"
    assert (
        client.get(
            f"/v1/workspaces/{WORKSPACE}/strategies/unknown", headers=HEADERS
        ).status_code
        == 404
    )


def test_execution_inspection_returns_actual_fills_and_artifacts_without_a_report() -> (
    None
):
    harness = Harness()
    harness.states = {"job-published": "succeeded"}
    client, _ = _client(harness)
    response = client.get(
        f"/v1/workspaces/{WORKSPACE}/backtests/jobs/job-published/inspection",
        headers=HEADERS,
    )
    assert response.status_code == 200
    detail = response.json()
    assert detail["state"] == "succeeded"
    assert [fill["quantity"] for fill in detail["fills"]] == ["4", "117"]
    assert detail["portfolio"]["positions"][0]["quantity"] == "0"
    assert detail["custom_metrics"] == {"episodes": "1"}
    assert detail["artifacts"][0]["sizeBytes"] == 1024
    assert client.app.state.backtest_service.store.list(WORKSPACE) == ()
