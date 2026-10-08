"""Real-Git focused authoring, recovery, confinement and receipt contracts."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import cast

import pytest

from thesis.persistence.journal import content_hash
from thesis.strategies import (
    StrategyArtifactError,
    WorkspaceStrategyArtifacts,
)
from thesis.workspaces.local import LocalWorkspaceProvider
from thesis.workspaces.manifest import load_workspace_manifest

pytestmark = pytest.mark.slow

SOURCE = """use harness_strategy_sdk::{StrategyProgram, export_strategy_program};
struct Demo;
impl StrategyProgram for Demo {
    fn new(_: harness_strategy_sdk::serde_json::Value) -> Self { Self }
}
export_strategy_program!(Demo);
"""
TEST = "#[test]\nfn arithmetic() { assert_eq!(2 + 2, 4); }\n"


def design(**overrides: object) -> dict[str, object]:
    return {
        "strategyId": "momentum",
        "version": "0.1.0",
        "entrypoint": "src/lib.rs",
        "files": {"src/lib.rs": SOURCE},
        "tests": {"tests/program.rs": TEST},
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
        "limitations": [],
        **overrides,
    }


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def workspace(tmp_path: Path) -> tuple[LocalWorkspaceProvider, str, Path]:
    provider = LocalWorkspaceProvider(tmp_path / "workspaces")
    manifest = load_workspace_manifest("config/thesis/workspace.yaml")
    opened = provider.create(manifest)
    return provider, manifest.workspace_id, opened.root


def test_author_restart_projection_receipt_and_exact_commit(workspace):
    provider, workspace_id, root = workspace
    (root / "unrelated.txt").write_text("staged")
    git(root, "add", "unrelated.txt")
    (root / "unrelated.txt").write_text("dirty")
    service = WorkspaceStrategyArtifacts(provider)
    receipt = service.author(
        workspace_id, "exec-1", design(), "Momentum", "AAPL", "Quotes"
    )
    assert receipt["bundleSha256"] != receipt["sourceSha256"]
    assert receipt["sourceSha256"] == content_hash(SOURCE)
    commit = cast(str, receipt["gitCommit"])
    assert "unrelated.txt" not in git(root, "ls-tree", "-r", "--name-only", commit)
    assert git(root, "show", ":unrelated.txt") == "staged"
    assert (root / "unrelated.txt").read_text() == "dirty"
    restarted = WorkspaceStrategyArtifacts(provider)
    assert (
        restarted.author(workspace_id, "exec-1", design(), "Momentum", "AAPL", "Quotes")
        == receipt
    )
    assert restarted.verify_completion(workspace_id, "exec-1", receipt) == receipt
    (summary,) = restarted.list(workspace_id)
    assert summary["id"] == "momentum"
    assert summary["code"] == SOURCE
    assert summary["status"] == "draft"
    assert set(summary) == {
        "id",
        "workspace_id",
        "name",
        "symbol",
        "status",
        "description",
        "pnl",
        "return_pct",
        "win_rate",
        "capital",
        "code",
        "versions",
        "created_at",
    }
    with pytest.raises(StrategyArtifactError, match="scope"):
        restarted.verify_completion(workspace_id, "other-exec", receipt)
    for key in (
        "bundleSha256",
        "sourceSha256",
        "manifestSha256",
        "gitCommit",
        "artifactPath",
    ):
        with pytest.raises(StrategyArtifactError):
            restarted.verify_completion(
                workspace_id, "exec-1", {**receipt, key: "forged"}
            )
