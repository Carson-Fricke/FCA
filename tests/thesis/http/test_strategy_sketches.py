"""Committed sketch authoring, revision isolation and HTTP inspection behavior."""

import copy
from typing import cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from thesis.http.artifacts import ArtifactCatalogService, create_artifact_router
from thesis.persistence.git import GitPublicationError
from thesis.workspaces.local import LocalWorkspaceProvider
from thesis.workspaces.manifest import load_workspace_manifest
from thesis.workspaces.programs import WorkspacePrograms


def test_sketch_revision_handoff_is_immutable_and_inspectable(tmp_path):
    provider = LocalWorkspaceProvider(tmp_path / "workspaces")
    manifest = load_workspace_manifest("config/thesis/workspace.yaml")
    provider.create(manifest)
    programs = WorkspacePrograms(provider)
    document = {
        "title": "Compression release",
        "premise": "Investigate compression",
        "mechanism": "Liquidity replenishment",
        "profitSource": "Untested directional release",
        "conditioning": "Slow context",
        "entry": "Confirmed release",
        "exit": "Failed release",
        "abstention": "Uncertain regime",
        "limitations": "Untested",
        "representations": [
            {
                "nodeId": "slow",
                "description": "Context",
                "inputs": [],
                "timeframes": ["1d"],
                "informationTime": "After session close",
            },
            {
                "nodeId": "fast",
                "description": "Release",
                "inputs": ["slow"],
                "timeframes": ["1min", "15min"],
                "informationTime": "Completed bars only",
            },
        ],
        "assumptions": [
            {
                "assumptionId": "release",
                "claim": "Conditioning adds information",
                "status": "untested",
                "investigation": "Compare conditional paths",
                "evidence": [],
            }
        ],
        "alternatives": ["Ordinary volatility expansion"],
    }
    original = programs.sketches.save(manifest.workspace_id, "compression", document)
    original_reference = cast(dict[str, str], original["reference"])
    revised = copy.deepcopy(document)
    revised["conditioning"] = "Slow context and spread quality"
    latest = programs.sketches.save(
        manifest.workspace_id,
        "compression",
        revised,
        expected_version=original_reference["version"],
    )
    latest_reference = cast(dict[str, str], latest["reference"])
    with pytest.raises(ValueError, match="stale"):
        programs.sketches.save(
            manifest.workspace_id,
            "compression",
            document,
            expected_version=original_reference["version"],
        )
    tampered = {**original_reference, "version": "0" * 64}
    with pytest.raises(GitPublicationError):
        programs.verify_strategy_sketch(manifest.workspace_id, tampered)
    app = FastAPI()
    app.state.artifact_catalog_service = ArtifactCatalogService(programs=programs)
    app.include_router(create_artifact_router())
    client = TestClient(app)
    pinned = client.get(
        f"/v1/workspaces/{manifest.workspace_id}/sketches/compression",
        params={
            "version": original_reference["version"],
            "git_commit": original_reference["gitCommit"],
        },
    )
    assert pinned.status_code == 200
    assert pinned.json()["sketch"] == document
    gallery = client.get(f"/v1/workspaces/{manifest.workspace_id}/artifacts").json()
    sketch = next(
        item for item in gallery["items"] if item["id"] == "sketch:compression"
    )
    assert sketch["preview"]["sketch"] == revised
    assert sketch["preview"]["reference"] == latest_reference
