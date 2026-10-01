"""Real-Git focused authoring, recovery, confinement and receipt contracts."""

from __future__ import annotations

import gc
import json
import shutil
import subprocess
from pathlib import Path
from typing import cast

import pytest

from thesis.persistence.git import GitCoordinator, GitFenceError, GitPublicationConflict
from thesis.persistence.journal import ArtifactRef, CommandJournal, content_hash
from thesis.strategies import (
    StrategyArtifactError,
    StrategyBundle,
    StrategyBundleError,
    StrategyDesign,
    StrategyValidationError,
    StrategyValidationResult,
    WorkspaceStrategyArtifacts,
)
from thesis.strategies.validation import StrategyValidationRequest
from thesis.workspaces.local import LocalWorkspaceProvider
from thesis.workspaces.manifest import load_workspace_manifest

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
        "harnessConfiguration": {},
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


def test_conflicting_version_and_slug_collision(workspace):
    provider, wid, root = workspace
    service = WorkspaceStrategyArtifacts(provider)
    service.author(wid, "exec-1", design(strategyId="Demo.One"), "Demo", "SPY")
    before = git(root, "rev-parse", "HEAD")
    for value in (
        design(strategyId="Demo.One", files={"src/lib.rs": SOURCE + "// different\n"}),
        design(strategyId="demo-one"),
    ):
        with pytest.raises((GitPublicationConflict, StrategyArtifactError)):
            service.author(wid, "exec-1", value, "Demo", "SPY")
    assert git(root, "rev-parse", "HEAD") == before


def test_provider_failure_recovery_durable_before_commit(workspace, monkeypatch):
    provider, wid, root = workspace
    original = provider.publish

    def fail(workspace_id):
        journal = CommandJournal(root / ".git/thesis-strategies.sqlite3")
        assert len(journal.entries()) == 1
        assert not journal.receipts()
        assert "Thesis-Publication-Id" in git(root, "log", "-1", "--format=%B")
        raise RuntimeError("remote unavailable")

    monkeypatch.setattr(provider, "publish", fail)
    with pytest.raises(RuntimeError, match="remote unavailable"):
        WorkspaceStrategyArtifacts(provider).author(
            wid, "exec-1", design(), "Demo", "SPY"
        )
    commit = git(root, "rev-parse", "HEAD")
    assert WorkspaceStrategyArtifacts(provider).list(wid) == ()
    monkeypatch.setattr(provider, "publish", original)
    receipt = WorkspaceStrategyArtifacts(provider).author(
        wid, "exec-1", design(), "Demo", "SPY"
    )
    assert receipt["gitCommit"] == commit
    assert len(WorkspaceStrategyArtifacts(provider).list(wid)) == 1


def test_tampering_hidden_and_completion_rejected(workspace):
    provider, wid, root = workspace
    service = WorkspaceStrategyArtifacts(provider)
    receipt = service.author(wid, "exec-1", design(), "Demo", "SPY")
    source = root / receipt["artifactPath"] / "src/lib.rs"
    source.write_text(SOURCE + "// tampered")
    assert service.list(wid) == ()
    with pytest.raises(StrategyArtifactError, match="hash"):
        service.verify_completion(wid, "exec-1", receipt)


@pytest.mark.parametrize(
    "value",
    [
        design(entrypoint="src/main.rs"),
        design(files={"src/lib.rs": SOURCE, "src/helper.rs": "fn helper() {}"}),
        design(files={"src/lib.rs": "print('python')"}),
        design(files={"src/lib.rs": SOURCE + "#[cfg(test)] mod tests {}"}),
        design(tests={"src/tests.rs": TEST}),
        design(tests={"tests/check.rs": "// not a runnable test"}),
        design(tests={"tests/check.rs": TEST, "tests/CHECK.rs": TEST}),
        design(tests={"tests/a.rs": TEST, "tests/a.rs/nested.rs": TEST}),
        design(tests={"tests/manifest.json/check.rs": TEST}),
        design(tests={"tests/../check.rs": TEST}),
        design(tests={"tests/NUL.rs": TEST}),
        design(workspaceId="untrusted"),
        design(executionId="untrusted"),
    ],
)
def test_rejected_contract_writes_nothing(workspace, value):
    provider, wid, root = workspace
    before = git(root, "rev-parse", "HEAD")
    with pytest.raises((StrategyBundleError, StrategyValidationError)):
        WorkspaceStrategyArtifacts(provider).author(wid, "exec-1", value, "Demo", "SPY")
    assert git(root, "rev-parse", "HEAD") == before
    assert not (root / "strategies/momentum").exists()
    assert not (root / ".git/thesis-strategies.sqlite3").exists()


def test_injected_validator_seam(workspace):
    provider, wid, root = workspace

    class Reject:
        def validate(
            self, request: StrategyValidationRequest
        ) -> StrategyValidationResult:
            assert (
                request.bundle_sha256
                == StrategyDesign.from_mapping(design()).bundle.bundle_sha256
            )
            return StrategyValidationResult(False, ("compile failure",))

    with pytest.raises(StrategyValidationError, match="compile failure"):
        WorkspaceStrategyArtifacts(provider, Reject()).author(
            wid, "exec-1", design(), "Demo", "SPY"
        )
    assert not (root / "strategies/momentum").exists()


def test_symlink_and_case_confinement(workspace):
    provider, wid, root = workspace
    (root / "strategies/Momentum").mkdir()
    with pytest.raises(StrategyArtifactError, match="case"):
        WorkspaceStrategyArtifacts(provider).author(
            wid, "exec-1", design(), "Demo", "SPY"
        )
    (root / "strategies/Momentum").rmdir()
    try:
        (root / "strategies/momentum").symlink_to(
            root / "uploads", target_is_directory=True
        )
    except OSError:
        pytest.skip("OS does not permit symlink creation")
    with pytest.raises(StrategyArtifactError, match="symlink"):
        WorkspaceStrategyArtifacts(provider).author(
            wid, "exec-1", design(), "Demo", "SPY"
        )


def test_bundle_digest_covers_tests_and_design_metadata():
    first = StrategyDesign.from_mapping(design()).bundle
    second = StrategyDesign.from_mapping(
        design(tests={"tests/program.rs": TEST + "// changed"})
    ).bundle
    third = StrategyDesign.from_mapping(
        design(
            exitCriteria=[
                {
                    "falsifierId": "f1",
                    "observable": "signal",
                    "condition": "gt",
                    "threshold": 1.0,
                    "description": "Flatten",
                }
            ]
        )
    ).bundle
    assert len({first.bundle_sha256, second.bundle_sha256, third.bundle_sha256}) == 3
    assert first.source[0].sha256 == second.source[0].sha256 == third.source[0].sha256
    with pytest.raises(StrategyBundleError, match="overlap"):
        StrategyBundle.create(
            strategy_id="demo",
            version="1.0.0",
            source={"a.rs": SOURCE},
            tests={"A.rs": TEST},
        )


def test_git_artifact_recovery_fence_and_binary_integrity(workspace):
    _, _, root = workspace
    journal = CommandJournal(root / ".git/focused.sqlite3")
    payload = b"\xff\x00\r\nbinary"
    files = {"artifacts/exact.bin": payload}
    entry = journal.accept(
        "test",
        {},
        {},
        workflow_hash=content_hash("w"),
        model_hash=content_hash("m"),
        artifacts=[
            ArtifactRef.from_bytes(path, value) for path, value in files.items()
        ],
    )
    coordinator = GitCoordinator(root)
    before = coordinator.head()
    receipt = coordinator.publish(entry, expected_head=before, artifacts=files)
    assert coordinator.commit_matches(receipt.commit_hash, entry, artifacts=files)
    assert coordinator.publish(entry, expected_head=before, artifacts=files).recovered
    with pytest.raises(GitPublicationConflict, match="bytes"):
        coordinator.publish(
            entry, expected_head=before, artifacts={"artifacts/exact.bin": b"wrong"}
        )
    (root / "artifacts/exact.bin").unlink()
    coordinator.publish(entry, expected_head=before, artifacts=files)
    assert (root / "artifacts/exact.bin").read_bytes() == payload
    next_entry = journal.accept(
        "next", {}, {}, workflow_hash=content_hash("w"), model_hash=content_hash("m")
    )
    with pytest.raises(GitFenceError):
        coordinator.publish(next_entry, expected_head=before)
    with coordinator.publication_lock():
        with pytest.raises(GitFenceError, match="lock"):
            GitCoordinator(root).publish(next_entry, expected_head=coordinator.head())


def test_manifest_matches_source_of_truth(workspace):
    provider, wid, root = workspace
    receipt = WorkspaceStrategyArtifacts(provider).author(
        wid, "exec-1", design(), "Demo", "SPY"
    )
    path = root / cast(str, receipt["artifactPath"]) / "manifest.json"
    manifest = json.loads(path.read_bytes())
    assert manifest["bundleSha256"] == receipt["bundleSha256"]
    assert content_hash(path.read_bytes()) == receipt["manifestSha256"]


def test_version_history_and_manifest_only_publication(workspace):
    provider, wid, root = workspace
    service = WorkspaceStrategyArtifacts(provider)
    first = service.author(wid, "exec-1", design(), "Demo", "SPY")
    second = service.author(
        wid, "exec-2", design(version="0.2.0", parentVersion="0.1.0"), "Demo 2", "SPY"
    )
    assert first["gitCommit"] != second["gitCommit"]
    assert service.verify_completion(wid, "exec-1", first) == first
    (summary,) = service.list(wid)
    assert summary["name"] == "Demo 2"
    versions = cast(list[dict[str, object]], summary["versions"])
    assert [version["version"] for version in versions] == ["0.1.0", "0.2.0"]
    assert versions[1]["parent_version"] == "0.1.0"
    journal = CommandJournal(root / ".git/manifest-only.sqlite3")
    entry = journal.accept(
        "manifest_only",
        {},
        {},
        workflow_hash=content_hash("w"),
        model_hash=content_hash("m"),
    )
    coordinator = GitCoordinator(root)
    result = coordinator.publish(entry, expected_head=coordinator.head())
    assert coordinator.commit_matches(result.commit_hash, entry)
    assert coordinator.find_publication(entry) == result.commit_hash
    assert len(service.list(wid)) == 1


def test_git_rejects_reserved_manifest_and_case_overlap(workspace):
    _, _, root = workspace
    journal = CommandJournal(root / ".git/paths.sqlite3")
    coordinator = GitCoordinator(root)
    for files in (
        {".thesis/publication.json": b"reserved"},
        {"artifacts/One.bin": b"one", "artifacts/one.bin": b"two"},
        {"artifacts/a.bin": b"one", "artifacts/a.bin/nested.bin": b"two"},
    ):
        entry = journal.accept(
            "paths",
            {},
            {},
            workflow_hash=content_hash("w"),
            model_hash=content_hash("m"),
            artifacts=[
                ArtifactRef.from_bytes(path, value) for path, value in files.items()
            ],
        )
        with pytest.raises(GitPublicationConflict, match="overlap"):
            coordinator.publish(
                entry, expected_head=coordinator.head(), artifacts=files
            )


def test_fresh_clone_projection_and_completion_without_journal(workspace, tmp_path):
    provider, wid, root = workspace
    service = WorkspaceStrategyArtifacts(provider)
    receipt = service.author(wid, "exec-1", design(), "Demo", "SPY")
    expected = service.list(wid)
    fresh = LocalWorkspaceProvider(tmp_path / "fresh")
    git(tmp_path, "clone", "--quiet", str(root), str(fresh.root / wid))
    cloned_root = fresh.open(wid).root
    assert not (cloned_root / ".git/thesis-strategies.sqlite3").exists()
    cloned = WorkspaceStrategyArtifacts(fresh)
    assert cloned.list(wid) == expected
    assert cloned.verify_completion(wid, "exec-1", receipt) == receipt
    assert not (cloned_root / ".git/thesis-strategies.sqlite3").exists()
    assert cloned.author(wid, "exec-1", design(), "Demo", "SPY") == receipt
    assert cloned.list(wid) == expected


def test_lost_journal_and_untracked_markers_do_not_control_projection(workspace):
    provider, wid, root = workspace
    service = WorkspaceStrategyArtifacts(provider)
    receipt = service.author(wid, "exec-1", design(), "Demo", "SPY")
    expected = service.list(wid)
    gc.collect()
    for path in (root / ".git").glob("thesis-strategies.sqlite3*"):
        path.unlink()
    assert service.list(wid) == expected
    assert service.verify_completion(wid, "exec-1", receipt) == receipt
    assert not (root / ".git/thesis-strategies.sqlite3").exists()
    copied = root / "strategies/forged/0.1.0"
    shutil.copytree(root / cast(str, receipt["artifactPath"]), copied)
    assert service.list(wid) == expected


def test_marker_publish_failure_attests_already_published_artifact(
    workspace, monkeypatch
):
    provider, wid, root = workspace
    original = provider.publish
    calls = 0

    def fail_second(workspace_id):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("marker push failed")
        return original(workspace_id)

    monkeypatch.setattr(provider, "publish", fail_second)
    with pytest.raises(RuntimeError, match="marker push failed"):
        WorkspaceStrategyArtifacts(provider).author(
            wid, "exec-1", design(), "Demo", "SPY"
        )
    # The original artifact was accepted on the first publish. The marker is a
    # tracked attestation of that fact, not a claim that its own push succeeded.
    assert len(WorkspaceStrategyArtifacts(provider).list(wid)) == 1
    before = git(root, "rev-parse", "HEAD")
    monkeypatch.setattr(provider, "publish", original)
    receipt = WorkspaceStrategyArtifacts(provider).author(
        wid, "exec-1", design(), "Demo", "SPY"
    )
    assert git(root, "rev-parse", "HEAD") == before
    assert (
        WorkspaceStrategyArtifacts(provider).verify_completion(wid, "exec-1", receipt)
        == receipt
    )


def test_initial_provider_failure_clone_has_no_published_projection(
    workspace, tmp_path, monkeypatch
):
    provider, wid, root = workspace

    def fail(workspace_id):
        raise RuntimeError("artifact push failed")

    monkeypatch.setattr(provider, "publish", fail)
    with pytest.raises(RuntimeError, match="artifact push failed"):
        WorkspaceStrategyArtifacts(provider).author(
            wid, "exec-1", design(), "Demo", "SPY"
        )
    assert not (root / "strategies/momentum/0.1.0/publication.json").exists()
    fresh = LocalWorkspaceProvider(tmp_path / "failed-clone")
    git(tmp_path, "clone", "--quiet", str(root), str(fresh.root / wid))
    assert WorkspaceStrategyArtifacts(fresh).list(wid) == ()
    assert not (fresh.open(wid).root / ".git/thesis-strategies.sqlite3").exists()


def test_git_environment_cannot_redirect_workspace_or_identity(
    workspace, tmp_path, monkeypatch
):
    provider, wid, root = workspace
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    git(foreign, "init", "--quiet")
    poisoned = tmp_path / "poisoned.gitconfig"
    poisoned.write_text(
        "[core]\n repositoryformatversion = 99\n[user]\n name = Ambient\n email = ambient@example.test\n"
    )
    foreign_index = tmp_path / "foreign.index"
    for key, value in {
        "GIT_DIR": str(foreign / ".git"),
        "GIT_WORK_TREE": str(foreign),
        "GIT_INDEX_FILE": str(foreign_index),
        "GIT_CONFIG_GLOBAL": str(poisoned),
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.repositoryformatversion",
        "GIT_CONFIG_VALUE_0": "99",
        "GIT_AUTHOR_NAME": "Ambient",
        "GIT_COMMITTER_NAME": "Ambient",
        "GIT_COMMON_DIR": str(foreign / ".git"),
    }.items():
        monkeypatch.setenv(key, value)
    receipt = WorkspaceStrategyArtifacts(provider).author(
        wid, "exec-1", design(), "Demo", "SPY"
    )
    coordinator = GitCoordinator(root)
    assert coordinator.is_ancestor(cast(str, receipt["gitCommit"]))
    assert not foreign_index.exists()
    assert not (foreign / "strategies").exists()
    for key in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_KEY_0",
        "GIT_CONFIG_VALUE_0",
        "GIT_AUTHOR_NAME",
        "GIT_COMMITTER_NAME",
        "GIT_COMMON_DIR",
    ):
        monkeypatch.delenv(key)
    assert (
        git(
            root,
            "show",
            "-s",
            "--format=%an <%ae>|%cn <%ce>",
            cast(str, receipt["gitCommit"]),
        )
        == "FCA Thesis <thesis@localhost>|FCA Thesis <thesis@localhost>"
    )


def test_clone_retry_recovers_missing_artifact_and_marker_without_journal(
    workspace, tmp_path
):
    provider, wid, root = workspace
    receipt = WorkspaceStrategyArtifacts(provider).author(
        wid, "exec-1", design(), "Demo", "SPY"
    )
    fresh = LocalWorkspaceProvider(tmp_path / "recovery")
    git(tmp_path, "clone", "--quiet", str(root), str(fresh.root / wid))
    cloned_root = fresh.open(wid).root
    directory = cloned_root / cast(str, receipt["artifactPath"])
    (directory / "src/lib.rs").unlink()
    (directory / "manifest.json").unlink()
    (directory / "publication.json").unlink()
    assert not (cloned_root / ".git/thesis-strategies.sqlite3").exists()
    service = WorkspaceStrategyArtifacts(fresh)
    assert service.author(wid, "exec-1", design(), "Demo", "SPY") == receipt
    assert service.verify_completion(wid, "exec-1", receipt) == receipt
    assert (directory / "src/lib.rs").read_text() == SOURCE
    marker = directory / "publication.json"
    expected = marker.read_bytes()
    marker.write_bytes(b"different")
    with pytest.raises(GitPublicationConflict, match="differs"):
        service.author(wid, "exec-1", design(), "Demo", "SPY")
    assert marker.read_bytes() == b"different"
    coordinator = GitCoordinator(cloned_root)
    with pytest.raises(GitPublicationConflict):
        coordinator.recover_artifacts(
            cast(str, coordinator.head()),
            {str(marker.relative_to(cloned_root)).replace("\\", "/"): expected},
        )
    assert marker.read_bytes() == b"different"


def test_unicode_git_paths_are_unquoted_and_verified(workspace):
    provider, wid, root = workspace
    path = "tests/caf\u00e9.rs"
    receipt = WorkspaceStrategyArtifacts(provider).author(
        wid, "exec-1", design(tests={path: TEST}), "Demo", "SPY"
    )
    coordinator = GitCoordinator(root)
    tracked = f"{receipt['artifactPath']}/{path}"
    assert tracked in coordinator.tracked_paths("strategies")
    assert coordinator.file_matches(
        cast(str, receipt["gitCommit"]), tracked, TEST.encode()
    )
    assert len(WorkspaceStrategyArtifacts(provider).list(wid)) == 1
    assert (
        WorkspaceStrategyArtifacts(provider).verify_completion(wid, "exec-1", receipt)
        == receipt
    )


def test_projection_uses_topology_not_timestamp_or_lexical_version(
    workspace, monkeypatch
):
    provider, wid, root = workspace
    monkeypatch.setattr(
        GitCoordinator, "commit_time", lambda self, commit: "2026-01-01T00:00:00+00:00"
    )
    service = WorkspaceStrategyArtifacts(provider)
    first = service.author(wid, "exec-1", design(version="0.9.0"), "Old", "SPY")
    second = service.author(
        wid, "exec-2", design(version="0.10.0", parentVersion="0.9.0"), "New", "SPY"
    )
    (summary,) = service.list(wid)
    assert summary["name"] == "New"
    versions = cast(list[dict[str, object]], summary["versions"])
    assert [version["version"] for version in versions] == ["0.9.0", "0.10.0"]
    order = GitCoordinator(root).commit_order()
    assert order.index(cast(str, first["gitCommit"])) < order.index(
        cast(str, second["gitCommit"])
    )


def test_parent_version_requires_same_published_lineage(workspace):
    provider, wid, root = workspace
    service = WorkspaceStrategyArtifacts(provider)
    before = GitCoordinator(root).head()
    for value in (design(parentVersion="0.1.0"), design(parentVersion="9.9.9")):
        with pytest.raises(StrategyArtifactError, match="parentVersion"):
            service.author(wid, "exec-1", value, "Demo", "SPY")
    assert GitCoordinator(root).head() == before
    service.author(wid, "exec-1", design(strategyId="Demo.One"), "Demo", "SPY")
    with pytest.raises(StrategyArtifactError, match="lineage"):
        service.author(
            wid,
            "exec-2",
            design(strategyId="demo-one", version="0.2.0", parentVersion="0.1.0"),
            "Demo",
            "SPY",
        )
