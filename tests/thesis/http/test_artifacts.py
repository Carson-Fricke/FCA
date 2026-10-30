"""Durable workspace artifact catalog and file reader HTTP contracts."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.thesis.workspaces._support import MemoryGitFactory

from thesis.http.artifacts import ArtifactCatalogService, create_artifact_router
from thesis.persistence.journal import content_hash
from thesis.strategies import StrategyArtifactError, WorkspaceStrategyArtifacts
from thesis.workspaces.local import LocalWorkspaceProvider
from thesis.workspaces.manifest import load_workspace_manifest
from thesis.workspaces.programs import WorkspacePrograms
from thesis.workspaces.sdk import SdkReferences
from thesis.workspaces.sdk.templates import INDICATOR_TEMPLATE, STUDY_TEMPLATE

SOURCE = """use harness_strategy_sdk::{StrategyProgram, export_strategy_program};
struct Demo;
impl StrategyProgram for Demo {
    fn new(_: harness_strategy_sdk::serde_json::Value) -> Self { Self }
}
export_strategy_program!(Demo);
"""
TEST_SOURCE = "#[test]\nfn arithmetic() { assert_eq!(2 + 2, 4); }\n"
CONFIG: dict[str, object] = {
    "start": "2024-01-01T00:00:00Z",
    "end": "2024-01-02T00:00:00Z",
    "subscriptions": [{"data_type": "bars", "symbols": ["SPY"], "interval": "1min"}],
}


def _design() -> dict[str, object]:
    return {
        "strategyId": "momentum",
        "version": "0.1.0",
        "entrypoint": "src/lib.rs",
        "files": {"src/lib.rs": SOURCE},
        "tests": {"tests/program.rs": TEST_SOURCE},
        "harnessConfiguration": {
            "subscriptions": [{"data_type": "quote", "symbols": ["SPY"]}]
        },
        "exitCriteria": [
            {
                "falsifierId": "f1",
                "observable": "signal",
                "condition": "lt",
                "threshold": 0.0,
                "description": "Flatten",
            }
        ],
        "limitations": ["quotes only"],
    }


@pytest.fixture(scope="module")
def published_strategy(
    tmp_path_factory: pytest.TempPathFactory,
) -> tuple[LocalWorkspaceProvider, str, dict[str, object]]:
    tmp_path = tmp_path_factory.mktemp("artifacts")
    provider = LocalWorkspaceProvider(tmp_path / "workspaces")
    manifest = load_workspace_manifest("config/thesis/workspace.yaml")
    provider.create(manifest)
    workspace_id = manifest.workspace_id
    root = provider.open(workspace_id).root
    (root / "unrelated.txt").write_text("staged")
    git(root, "add", "unrelated.txt")
    (root / "unrelated.txt").write_text("dirty")
    strategy_artifacts = WorkspaceStrategyArtifacts(provider)
    receipt = strategy_artifacts.author(
        workspace_id, "exec-1", _design(), "Momentum", "SPY", "Breakout rework"
    )
    return provider, workspace_id, receipt


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture(scope="module")
def workspace(
    published_strategy: tuple[LocalWorkspaceProvider, str, dict[str, object]],
) -> tuple[TestClient, str]:
    provider, workspace_id, _ = published_strategy
    strategy_artifacts = WorkspaceStrategyArtifacts(provider)
    # Strategy publication exercises real Git; program catalog projection
    # needs only the external Git port's committed documents.
    programs = WorkspacePrograms(
        provider, git_factory=MemoryGitFactory(), sdk=SdkReferences()
    )
    programs.author(
        workspace_id, "indicator", "rsi_value", INDICATOR_TEMPLATE, configuration=CONFIG
    )
    programs.author(
        workspace_id,
        "study",
        "band_study",
        STUDY_TEMPLATE,
        indicator_ids=["rsi_value"],
        configuration=CONFIG,
    )
    app = FastAPI()
    app.state.artifact_catalog_service = ArtifactCatalogService(
        strategy_artifacts, programs
    )
    app.include_router(create_artifact_router())
    return TestClient(app), workspace_id


def test_publication_restart_receipt_and_http_catalog(
    workspace: tuple[TestClient, str],
    published_strategy: tuple[LocalWorkspaceProvider, str, dict[str, object]],
) -> None:
    client, workspace_id = workspace
    provider, _, receipt = published_strategy
    root = provider.open(workspace_id).root
    assert receipt["bundleSha256"] != receipt["sourceSha256"]
    assert receipt["sourceSha256"] == content_hash(SOURCE)
    assert "unrelated.txt" not in git(
        root, "ls-tree", "-r", "--name-only", cast(str, receipt["gitCommit"])
    )
    assert git(root, "show", ":unrelated.txt") == "staged"
    assert (root / "unrelated.txt").read_text() == "dirty"
    restarted = WorkspaceStrategyArtifacts(provider)
    assert (
        restarted.author(
            workspace_id, "exec-1", _design(), "Momentum", "SPY", "Breakout rework"
        )
        == receipt
    )
    assert restarted.verify_completion(workspace_id, "exec-1", receipt) == receipt
    with pytest.raises(StrategyArtifactError, match="scope"):
        restarted.verify_completion(workspace_id, "other-exec", receipt)
    # One mismatch covers whole-receipt equality; the path case separately
    # exercises lookup of an unpublished artifact.
    for key in ("bundleSha256", "artifactPath"):
        with pytest.raises(StrategyArtifactError):
            restarted.verify_completion(
                workspace_id, "exec-1", {**receipt, key: "forged"}
            )
    response = client.get(f"/v1/workspaces/{workspace_id}/artifacts")
    assert response.status_code == 200
    document = response.json()
    assert document["workspace_id"] == workspace_id
    assert document["sources"] == {
        "strategies": "ok",
        "studies": "ok",
        "indicators": "ok",
    }
    items = {item["id"]: item for item in document["items"]}
    assert set(items) == {
        "strategy:momentum",
        "study:band_study",
        "indicator:rsi_value",
    }

    strategy = items["strategy:momentum"]
    assert strategy["title"] == "Momentum"
    assert strategy["version"] == "0.1.0"
    assert strategy["subtitle"] == "momentum · SPY"
    assert strategy["inputs"] == [
        {"data_type": "quote", "symbols": ["SPY"], "interval": None}
    ]
    assert strategy["preview"]["type"] == "strategy"
    assert strategy["preview"]["exit_criteria"][0]["falsifier_id"] == "f1"
    assert strategy["preview"]["limitations"] == ["quotes only"]
    assert strategy["preview"]["signals"] is None
    assert strategy["history"] == [
        {
            "label": "v0.1.0",
            "created_at": strategy["history"][0]["created_at"],
            "note": "root",
        }
    ]
    assert {file["path"] for file in strategy["files"]} == {
        "src/lib.rs",
        "tests/program.rs",
        "manifest.json",
        "publication.json",
    }
    assert strategy["digests"]["source_sha256"] == content_hash(SOURCE)
    assert strategy["file_count"] == len(strategy["files"])
    assert strategy["size_bytes"] > 0

    study = items["study:band_study"]
    assert study["preview"]["type"] == "study"
    assert study["preview"]["indicator_ids"] == ["rsi_value"]
    assert study["preview"]["window"] == {
        "start": "2024-01-01T00:00:00Z",
        "end": "2024-01-02T00:00:00Z",
    }
    assert study["inputs"] == [
        {"data_type": "bars", "symbols": ["SPY"], "interval": "1min"}
    ]
    assert {file["path"] for file in study["files"]} == {
        "program.rs",
        "manifest.json",
    }
    assert study["history"]

    indicator = items["indicator:rsi_value"]
    assert indicator["preview"]["type"] == "indicator"
    assert indicator["preview"]["value_series"] is None
    assert indicator["preview"]["used_by"] == [
        {"id": "study:band_study", "kind": "study", "title": "band_study"}
    ]


def test_file_reader_serves_committed_text(workspace: tuple[TestClient, str]) -> None:
    client, workspace_id = workspace
    strategy_file = client.get(
        f"/v1/workspaces/{workspace_id}/artifacts/strategy:momentum/files/src/lib.rs"
    )
    assert strategy_file.status_code == 200
    document = strategy_file.json()
    assert document["text"] == SOURCE
    assert document["sha256"] == content_hash(SOURCE)
    assert document["truncated"] is False
    assert document["size_bytes"] == len(SOURCE.encode())

    study_file = client.get(
        f"/v1/workspaces/{workspace_id}/artifacts/study:band_study/files/program.rs"
    )
    assert study_file.status_code == 200
    assert study_file.json()["text"] == STUDY_TEMPLATE

    indicator_manifest = client.get(
        f"/v1/workspaces/{workspace_id}/artifacts/indicator:rsi_value/files/manifest.json"
    )
    assert indicator_manifest.status_code == 200
    assert json.loads(indicator_manifest.json()["text"])["kind"] == "indicator"


def test_file_reader_rejects_traversal_and_unknown_names(
    workspace: tuple[TestClient, str],
) -> None:
    client, workspace_id = workspace
    traversal = client.get(
        f"/v1/workspaces/{workspace_id}/artifacts/strategy:momentum"
        "/files/%2e%2e%2f%2e%2e%2fmanifest.json"
    )
    assert traversal.status_code in (404, 422)
    unknown_file = client.get(
        f"/v1/workspaces/{workspace_id}/artifacts/strategy:momentum/files/secrets.txt"
    )
    assert unknown_file.status_code == 404
    unknown_artifact = client.get(
        f"/v1/workspaces/{workspace_id}/artifacts/strategy:missing/files/src/lib.rs"
    )
    assert unknown_artifact.status_code == 404
    malformed = client.get(
        f"/v1/workspaces/{workspace_id}/artifacts/momentum/files/src/lib.rs"
    )
    assert malformed.status_code == 404


def test_catalog_missing_workspace_is_not_fabricated(tmp_path: Path) -> None:
    provider = LocalWorkspaceProvider(tmp_path / "workspaces")
    service = ArtifactCatalogService(WorkspaceStrategyArtifacts(provider), None)
    with pytest.raises(KeyError, match="ghost"):
        service.catalog("ghost")


def test_router_fails_closed_without_composed_service() -> None:
    app = FastAPI()
    app.include_router(create_artifact_router())
    with TestClient(app) as client:
        response = client.get("/v1/workspaces/alpha/artifacts")
    assert response.status_code == 503
