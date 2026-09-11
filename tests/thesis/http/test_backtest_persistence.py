"""Focused route tests for backtest launch persistence (T290).

A fake :class:`HarnessBoundary` returns inert :class:`ActivationOutcome`
values, so launch, list, and detail can be exercised without any harness
process.  The artifact-import test uses the real
:class:`thesis.artifacts.importer.ArtifactImporter` over a real temporary
filesystem with an injected fake Git boundary, so content-hash references and
the immutable workspace publication are covered end to end.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from thesis.artifacts.importer import ArtifactImporter
from thesis.http.application import create_app
from thesis.http.backtests import BacktestDataService, InMemoryBacktestStore
from thesis.http.workspaces import build_local_workspace_data
from thesis.integrations.harness_activation import (
    ActivationOutcome,
    HarnessActivationRequest,
    HarnessCapabilities,
)

BEARER_TOKEN = "thesis-test-token"
HEADERS = {"Authorization": f"Bearer {BEARER_TOKEN}"}
WORKSPACE = "alpha-lab"
NOW = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
FIXED_CREATED_AT = "2026-09-09T12:00:00Z"


def clock() -> datetime:
    return NOW


def counting_ids() -> Callable[[str], str]:
    counters: dict[str, int] = {}

    def factory(prefix: str) -> str:
        counters[prefix] = counters.get(prefix, 0) + 1
        return f"{prefix}-{counters[prefix]:03d}"

    return factory


class FakeHarnessBoundary:
    """Inert boundary returning a canned outcome and recording activations."""

    def __init__(
        self,
        *,
        raw: Mapping[str, object] | None = None,
        exit_code: int | None = 0,
        state: str | None = None,
        job_id: str | None = None,
    ) -> None:
        self.raw: dict[str, object] = dict(raw or {})
        self.exit_code = exit_code
        self.state = state
        self.job_id = job_id
        self.requests: list[HarnessActivationRequest] = []

    @property
    def capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities()

    def run_backtest(
        self, request: HarnessActivationRequest, *, bearer_token: str | None = None
    ) -> ActivationOutcome:
        del bearer_token
        self.requests.append(request)
        return ActivationOutcome(
            action=request.action,
            bundle_ref=request.bundle_ref,
            environment=request.environment,
            request_payload=request.to_payload(),
            exit_code=self.exit_code,
            job_id=self.job_id,
            state=self.state,
            raw=dict(self.raw),
        )

    def launch_trader(
        self, request: HarnessActivationRequest, *, bearer_token: str | None = None
    ) -> ActivationOutcome:
        raise NotImplementedError("backtest routes never launch a trader")


class FakeGitBoundary:
    """Deterministic local Git boundary returning sequential object IDs."""

    def __init__(self) -> None:
        self.commits: list[tuple[tuple[str, ...], str]] = []

    def head(self) -> str | None:
        return None

    def commit(
        self,
        paths: Sequence[str],
        message: str,
        *,
        expected_head: str | None,
    ) -> str:
        del message, expected_head
        commit = f"{len(self.commits) + 1:040x}"
        self.commits.append((tuple(paths), commit))
        return commit


def make_client(service: BacktestDataService) -> TestClient:
    app = create_app(bearer_token=BEARER_TOKEN)
    app.state.backtest_service = service
    return TestClient(app)


def launch(
    client: TestClient,
    *,
    bundle_ref: str = "bundle-1",
    config_path: str = "runs/bt-204.yaml",
):
    return client.post(
        f"/v1/workspaces/{WORKSPACE}/backtests/launch",
        headers=HEADERS,
        json={"bundle_ref": bundle_ref, "config_path": config_path},
    )


def test_launch_persists_record_visible_in_list_and_detail() -> None:
    harness = FakeHarnessBoundary()
    service = BacktestDataService(
        InMemoryBacktestStore(),
        harness=harness,
        clock=clock,
        id_factory=counting_ids(),
    )
    client = make_client(service)

    response = launch(client)
    assert response.status_code == 200
    body = response.json()
    assert body["backtest_id"] == "bt-001"
    assert body["action"] == "backtest"
    record = body["backtest"]
    assert record["id"] == "bt-001"
    assert record["workspace_id"] == WORKSPACE
    assert record["status"] == "completed"
    assert record["bundle_ref"] == "bundle-1"
    assert record["config_path"] == "runs/bt-204.yaml"
    assert record["manifest"]["launch"]["bundle_ref"] == "bundle-1"
    assert "import" not in record["manifest"]
    assert harness.requests[0].action == "backtest"

    listed = client.get(f"/v1/workspaces/{WORKSPACE}/backtests", headers=HEADERS)
    assert listed.status_code == 200
    items = listed.json()["items"]
    assert [row["id"] for row in items] == ["bt-001"]
    assert items[0]["workspace_id"] == WORKSPACE
    assert items[0]["created_at"] == FIXED_CREATED_AT

    detail = client.get(f"/v1/workspaces/{WORKSPACE}/backtests/bt-001", headers=HEADERS)
    assert detail.status_code == 200
    assert detail.json()["id"] == "bt-001"
    assert detail.json()["status"] == "completed"
    assert detail.json()["bundle_ref"] == "bundle-1"


def test_launch_without_manifest_skips_artifact_import(tmp_path: Path) -> None:
    git = FakeGitBoundary()
    importer = ArtifactImporter(tmp_path / "workspace", git=git)
    service = BacktestDataService(
        InMemoryBacktestStore(),
        harness=FakeHarnessBoundary(),
        importer=importer,
        clock=clock,
        id_factory=counting_ids(),
    )
    client = make_client(service)

    response = launch(client)
    assert response.status_code == 200
    assert "import" not in response.json()["backtest"]["manifest"]
    assert git.commits == []


def test_build_local_workspace_data_injects_backtest_boundaries(
    tmp_path: Path,
) -> None:
    store = InMemoryBacktestStore()
    importer = ArtifactImporter(tmp_path / "workspace", git=FakeGitBoundary())

    bundle = build_local_workspace_data(
        harness_boundary=FakeHarnessBoundary(),
        backtest_store=store,
        artifact_importer=importer,
    )

    assert bundle.backtests.store is store
    assert bundle.backtests.importer is importer


def test_launch_imports_manifest_artifacts_by_content_hash(tmp_path: Path) -> None:
    results = tmp_path / "results"
    results.mkdir()
    payload = b'{"sharpe": 1.4}'
    (results / "metrics.json").write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    manifest: dict[str, object] = {
        "manifestId": "manifest-run-1",
        "strategyVersion": "1.2.3",
        "experimentId": "experiment-1",
        "workflowExecutionId": "execution-1",
        "artifacts": [
            {
                "name": "metrics.json",
                "uri": "metrics.json",
                "sha256": digest,
                "sizeBytes": len(payload),
                "mediaType": "application/json",
                "reproducibilityCritical": True,
            }
        ],
    }
    metrics: dict[str, object] = {
        "total_return": 0.12,
        "sharpe": 1.4,
        "max_drawdown": -0.05,
        "win_rate": 0.6,
        "trades": 42,
        "final_capital": 112000.0,
    }

    git = FakeGitBoundary()
    workspace = tmp_path / "workspace"
    importer = ArtifactImporter(workspace, git=git)
    harness = FakeHarnessBoundary(
        raw={
            "manifest": manifest,
            "metrics": metrics,
            "result_root": str(results),
        }
    )
    service = BacktestDataService(
        InMemoryBacktestStore(),
        harness=harness,
        importer=importer,
        clock=clock,
        id_factory=counting_ids(),
    )
    client = make_client(service)

    response = launch(client)
    assert response.status_code == 200
    record = response.json()["backtest"]
    assert record["manifest"]["metrics"]["sharpe"] == 1.4
    imported = record["manifest"]["import"]
    assert imported["experimentId"] == "experiment-1"
    assert imported["strategyVersion"] == "1.2.3"
    assert imported["harnessManifest"]["id"] == "manifest-run-1"
    reference = imported["artifacts"][0]
    assert reference["artifactId"] == f"sha256:{digest}"
    assert reference["sha256"] == digest
    assert reference["uri"] == (
        f"backtests/experiment-1/artifacts/{digest}/metrics.json"
    )
    assert len(git.commits) == 2

    stored = (
        workspace / "backtests" / "experiment-1" / "artifacts" / digest / "metrics.json"
    )
    assert stored.read_bytes() == payload

    listed = client.get(f"/v1/workspaces/{WORKSPACE}/backtests", headers=HEADERS)
    assert listed.status_code == 200
    row = listed.json()["items"][0]
    assert row["id"] == "bt-001"
    assert row["total_return"] == 0.12
    assert row["sharpe"] == 1.4
    assert row["trades"] == 42

    detail = client.get(f"/v1/workspaces/{WORKSPACE}/backtests/bt-001", headers=HEADERS)
    assert detail.status_code == 200
    detail_body = detail.json()
    assert detail_body["stats"] == {
        "sharpe": 1.4,
        "max_drawdown": -0.05,
        "win_rate": 0.6,
    }
    assert detail_body["final_capital"] == 112000.0
    assert detail_body["manifest"]["import"]["artifacts"][0]["sha256"] == digest
