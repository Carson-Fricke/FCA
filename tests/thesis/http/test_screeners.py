"""HTTP tests for the thin screener routes (definitions, runs, results).

The routes are exercised through :func:`create_app` with a fake MCP tool
provider composed into the local workspace bundle, so the harness boundary is
never opened and market data is never touched.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

from fastapi.testclient import TestClient

from thesis.http.application import create_app
from thesis.http.workspaces import WorkspaceDataBundle, build_local_workspace_data
from thesis.integrations.mcp import McpToolResult
from thesis.screening import ScreenerDefinition

BEARER_TOKEN = "thesis-test-token"
HEADERS = {"Authorization": f"Bearer {BEARER_TOKEN}"}
WORKSPACE = "alpha-lab"
RUN_ID = "scr-run-001"
AS_OF = "2026-09-28"

DEFINITION_PAYLOAD: dict[str, object] = {
    "name": "Momentum leaders",
    "lookback_days": 252,
    "bar_interval": "1d",
    "rank_metric": "momentum_12_1",
    "universe_revision": "sp500-2026-09",
    "source": "fn screen(ctx) { ctx.rows() }",
    "parameters": {"threshold": 0.05},
}


class _FakeScreeningTools:
    """Fake typed MCP tool provider serving one complete screening run."""

    def __init__(
        self,
        *,
        states: Sequence[str] = ("running", "complete"),
        artifact_present: bool = True,
    ) -> None:
        self.states = list(states)
        self.artifact_present = artifact_present
        self.calls: list[tuple[str, Mapping[str, object]]] = []
        self.definition = ScreenerDefinition(
            name="Momentum leaders",
            lookback_days=252,
            bar_interval="1d",
            rank_metric="momentum_12_1",
            universe_revision="sp500-2026-09",
            source="fn screen(ctx) { ctx.rows() }",
            parameters={"threshold": 0.05},
        )

    def run_screener(
        self, *, config: Mapping[str, object], workspace: str | None = None
    ) -> McpToolResult:
        self.calls.append(("run_screener", {"config": dict(config)}))
        return McpToolResult(
            tool="run_screener",
            is_error=False,
            structured={"run_id": RUN_ID, "state": "running"},
        )

    def get_screener_status(self, *, job_id: str) -> McpToolResult:
        self.calls.append(("get_screener_status", {"job_id": job_id}))
        state = self.states.pop(0) if self.states else "complete"
        return McpToolResult(
            tool="get_screener_status",
            is_error=False,
            structured={"job_id": job_id, "state": state},
        )

    def get_run(self, *, run_id: str) -> McpToolResult:
        self.calls.append(("get_run", {"run_id": run_id}))
        return McpToolResult(
            tool="get_run",
            is_error=False,
            structured={"run_id": run_id, "status": "complete"},
        )

    def list_artifacts(self, *, run_id: str) -> McpToolResult:
        self.calls.append(("list_artifacts", {"run_id": run_id}))
        artifacts = [{"name": "config.yaml", "sha256": "b" * 64}]
        if self.artifact_present:
            artifacts.append({"name": "screen.json", "sha256": "a" * 64})
        return McpToolResult(
            tool="list_artifacts",
            is_error=False,
            structured={"run_id": run_id, "artifacts": artifacts},
        )

    def read_artifact(self, *, run_id: str, name: str) -> McpToolResult:
        self.calls.append(("read_artifact", {"run_id": run_id, "name": name}))
        definition = self.definition
        document = {
            "schema": "fca-harness-screen-v1",
            "status": "complete",
            "run_id": RUN_ID,
            "as_of": AS_OF,
            "definition_id": definition.definition_id,
            "definition_hash": definition.definition_sha256,
            "bundle_hash": definition.source_sha256,
            "rank_metric": definition.rank_metric,
            "bar_interval": definition.bar_interval,
            "data_window": {"start": "2025-09-28", "end": AS_OF},
            "universe_revision": definition.universe_revision,
            "rows": [{"symbol": "AAPL", "metrics": {"momentum_12_1": "0.44"}}],
            "provenance": {
                "source_digest": definition.source_sha256,
                "toolchain_digest": "c" * 64,
                "run_digest": "d" * 64,
            },
        }
        return McpToolResult(
            tool="read_artifact",
            is_error=False,
            structured={"encoding": "utf-8", "content": json.dumps(document)},
        )


def _bundle(tools: _FakeScreeningTools | None) -> WorkspaceDataBundle:
    return build_local_workspace_data(screener_tools=tools)


def _client(bundle: WorkspaceDataBundle) -> TestClient:
    return TestClient(create_app(bearer_token=BEARER_TOKEN, workspace_data=bundle))


def test_save_and_list_definitions_are_content_address_stable() -> None:
    client = _client(_bundle(_FakeScreeningTools()))

    first = client.post(
        f"/v1/workspaces/{WORKSPACE}/screeners",
        headers=HEADERS,
        json=DEFINITION_PAYLOAD,
    )
    assert first.status_code == 201
    saved = first.json()
    assert saved["name"] == "Momentum leaders"
    assert saved["definitionId"].startswith("sha256:")
    assert len(saved["sourceSha256"]) == 64
    assert len(saved["definitionSha256"]) == 64
    assert saved["sourceSha256"] != saved["definitionSha256"]

    second = client.post(
        f"/v1/workspaces/{WORKSPACE}/screeners",
        headers=HEADERS,
        json=DEFINITION_PAYLOAD,
    )
    assert second.status_code == 201
    assert second.json()["definitionId"] == saved["definitionId"]

    listed = client.get(f"/v1/workspaces/{WORKSPACE}/screeners", headers=HEADERS)
    assert listed.status_code == 200
    items = listed.json()["items"]
    assert [item["definitionId"] for item in items] == [saved["definitionId"]]
    assert items[0]["source"] == DEFINITION_PAYLOAD["source"]


def test_run_returns_a_record_and_result_lookup_round_trips() -> None:
    tools = _FakeScreeningTools(states=("running", "complete"))
    client = _client(_bundle(tools))
    saved = client.post(
        f"/v1/workspaces/{WORKSPACE}/screeners",
        headers=HEADERS,
        json=DEFINITION_PAYLOAD,
    ).json()

    response = client.post(
        f"/v1/workspaces/{WORKSPACE}/screeners/run",
        headers=HEADERS,
        json={"definition_id": saved["definitionId"], "as_of": AS_OF},
    )

    assert response.status_code == 200
    record = response.json()
    assert record["run_id"] == RUN_ID
    assert record["definition_id"] == saved["definitionId"]
    assert record["status"] == "complete"
    assert record["as_of"] == AS_OF
    assert record["rows"] == [{"symbol": "AAPL", "metrics": {"momentum_12_1": "0.44"}}]

    fetched = client.get(
        f"/v1/workspaces/{WORKSPACE}/screeners/results/{record['result_id']}",
        headers=HEADERS,
    )
    assert fetched.status_code == 200
    assert fetched.json() == record

    missing = client.get(
        f"/v1/workspaces/{WORKSPACE}/screeners/results/sha256:missing",
        headers=HEADERS,
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "screener_result_not_found"


def test_run_without_a_harness_boundary_is_503() -> None:
    client = _client(_bundle(None))
    saved = client.post(
        f"/v1/workspaces/{WORKSPACE}/screeners",
        headers=HEADERS,
        json=DEFINITION_PAYLOAD,
    ).json()

    response = client.post(
        f"/v1/workspaces/{WORKSPACE}/screeners/run",
        headers=HEADERS,
        json={"definition_id": saved["definitionId"], "as_of": AS_OF},
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "harness_unavailable"
    assert (
        client.get(f"/v1/workspaces/{WORKSPACE}/screeners", headers=HEADERS).status_code
        == 200
    )


def test_unknown_definition_is_404() -> None:
    client = _client(_bundle(_FakeScreeningTools()))

    response = client.post(
        f"/v1/workspaces/{WORKSPACE}/screeners/run",
        headers=HEADERS,
        json={"definition_id": "sha256:missing", "as_of": AS_OF},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "screener_not_found"


def test_invalid_definition_is_422() -> None:
    client = _client(_bundle(_FakeScreeningTools()))
    base = f"/v1/workspaces/{WORKSPACE}/screeners"

    for payload in (
        {**DEFINITION_PAYLOAD, "source": ""},
        {**DEFINITION_PAYLOAD, "bar_interval": "5min"},
        {**DEFINITION_PAYLOAD, "lookback_days": 0},
    ):
        response = client.post(base, headers=HEADERS, json=payload)
        assert response.status_code == 422
        assert response.json()["error"]["code"] in {
            "validation_error",
            "invalid_screener_definition",
        }


def test_invalid_run_request_is_422() -> None:
    client = _client(_bundle(_FakeScreeningTools()))

    missing_as_of = client.post(
        f"/v1/workspaces/{WORKSPACE}/screeners/run",
        headers=HEADERS,
        json={"definition_id": "sha256:x"},
    )
    assert missing_as_of.status_code == 422

    extra_field = client.post(
        f"/v1/workspaces/{WORKSPACE}/screeners/run",
        headers=HEADERS,
        json={"definition_id": "sha256:x", "as_of": AS_OF, "extra": True},
    )
    assert extra_field.status_code == 422


def test_artifact_failure_is_502_and_stores_no_result() -> None:
    tools = _FakeScreeningTools(states=("complete",), artifact_present=False)
    bundle = _bundle(tools)
    client = _client(bundle)
    saved = client.post(
        f"/v1/workspaces/{WORKSPACE}/screeners",
        headers=HEADERS,
        json=DEFINITION_PAYLOAD,
    ).json()

    response = client.post(
        f"/v1/workspaces/{WORKSPACE}/screeners/run",
        headers=HEADERS,
        json={"definition_id": saved["definitionId"], "as_of": AS_OF},
    )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "screener_run_failed"
    assert bundle.screeners is not None
    assert bundle.screeners.list_results(WORKSPACE) == ()


def test_screener_routes_require_bearer_token() -> None:
    client = _client(_bundle(_FakeScreeningTools()))

    listed = client.get(f"/v1/workspaces/{WORKSPACE}/screeners")
    assert listed.status_code == 401
    assert listed.json()["error"]["code"] == "unauthorized"

    saved = client.post(
        f"/v1/workspaces/{WORKSPACE}/screeners", json=DEFINITION_PAYLOAD
    )
    assert saved.status_code == 401
