"""HTTP coverage for the backtest run materialization endpoint.

The UI calls ``GET /v1/workspaces/{ws}/backtests/runs/{run_id}`` once the
harness MCP tool returns a run id.  The route delegates to the composed
coordinator so the harness boundary (local or remote) stays behind the backend.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from thesis.http.application import create_app

TOKEN = "thesis-test-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}
WORKSPACE = "workspace-a"
RUN_ID = "momentum-spy-2024-01"


class _FakeCoordinator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None, str | None]] = []

    def materialize(
        self,
        workspace_id: str,
        run_id: str,
        *,
        chat_id: str | None = None,
        execution_id: str | None = None,
    ) -> dict[str, object]:
        self.calls.append((workspace_id, run_id, chat_id, execution_id))
        return {
            "status": "complete",
            "run_id": run_id,
            "state": "completed",
            "backtest_id": "bt-1",
            "backtest": {"id": "bt-1", "run_id": run_id},
        }


def test_materialize_route_delegates_to_the_coordinator() -> None:
    app = create_app(bearer_token=TOKEN)
    coordinator = _FakeCoordinator()
    app.state.backtest_run_coordinator = coordinator
    client = TestClient(app)

    response = client.get(
        f"/v1/workspaces/{WORKSPACE}/backtests/runs/{RUN_ID}?chat_id=chat-1",
        headers=HEADERS,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["backtest_id"] == "bt-1"
    assert body["run_id"] == RUN_ID
    assert coordinator.calls == [(WORKSPACE, RUN_ID, "chat-1", None)]


def test_materialize_route_fails_closed_without_a_coordinator() -> None:
    app = create_app(bearer_token=TOKEN)
    client = TestClient(app)

    response = client.get(
        f"/v1/workspaces/{WORKSPACE}/backtests/runs/{RUN_ID}", headers=HEADERS
    )

    assert response.status_code == 503
