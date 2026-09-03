from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from thesis.workspaces import (
    LocalWorkspaceProvider,
    WorkspaceAlreadyExistsError,
    WorkspacePathError,
    WorkspaceRepositoryError,
    load_workspace_manifest,
)
from thesis.workspaces.local import WORKSPACE_DIRECTORIES

ROOT = Path(__file__).parents[3]
EXAMPLE = ROOT / "config" / "thesis" / "workspace.yaml"


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    return result.stdout.strip()


def _manifest(workspace_id: str):
    manifest = load_workspace_manifest(EXAMPLE)
    repository = manifest.repository_identity.model_copy(
        update={"repository_id": workspace_id}
    )
    return manifest.model_copy(
        update={"workspace_id": workspace_id, "repository_identity": repository}
    )


def test_create_builds_and_commits_the_documented_scaffold(tmp_path: Path) -> None:
    provider = LocalWorkspaceProvider(tmp_path / "workspaces")
    workspace = provider.create(_manifest("workspace-one"))

    assert workspace.root.parent == provider.root
    assert workspace.manifest.workspace_id == "workspace-one"
    for relative in WORKSPACE_DIRECTORIES:
        assert workspace.resolve_path(relative, must_exist=True).is_dir()
    for relative in (
        ".thesis/workspace.yaml",
        ".thesis/platform.lock",
        "AGENTS.md",
        "README.md",
        ".gitignore",
    ):
        assert workspace.resolve_path(relative, must_exist=True).is_file()

    assert Path(_git(workspace.root, "rev-parse", "--show-toplevel")).resolve() == (
        workspace.root
    )
    assert _git(workspace.root, "log", "-1", "--format=%s") == (
        "Initialize Thesis workspace"
    )
    assert _git(workspace.root, "status", "--porcelain") == ""

    (workspace.root / ".env").write_text("BROKER_TOKEN=raw-secret\n", encoding="utf-8")
    assert _git(workspace.root, "status", "--porcelain") == ""


def test_workspaces_are_separate_repositories_and_reopen_by_identity(
    tmp_path: Path,
) -> None:
    provider = LocalWorkspaceProvider(tmp_path / "workspaces")
    first = provider.create(_manifest("workspace-one"))
    second = provider.create(_manifest("workspace-two"))

    (first.root / "strategies" / "first.py").write_text("FIRST = True\n")

    reopened = provider.open("workspace-one")
    assert reopened.root == first.root
    assert first.root != second.root
    assert (first.root / ".git").is_dir()
    assert (second.root / ".git").is_dir()
    assert not (second.root / "strategies" / "first.py").exists()
    assert _git(first.root, "rev-parse", "--git-dir") == ".git"
    assert _git(second.root, "rev-parse", "--git-dir") == ".git"

    with pytest.raises(WorkspaceAlreadyExistsError):
        provider.create(_manifest("workspace-one"))


@pytest.mark.parametrize(
    "relative",
    ["../workspace-two/README.md", "strategies/../../workspace-two", "C:\\outside"],
)
def test_provider_refuses_traversal_and_absolute_paths(
    tmp_path: Path, relative: str
) -> None:
    provider = LocalWorkspaceProvider(tmp_path / "workspaces")
    provider.create(_manifest("workspace-one"))
    provider.create(_manifest("workspace-two"))

    with pytest.raises(WorkspacePathError):
        provider.resolve_path("workspace-one", relative)

    with pytest.raises(WorkspacePathError):
        provider.open("../workspace-two")


def test_provider_refuses_symlink_escape_to_another_workspace(
    tmp_path: Path,
) -> None:
    provider = LocalWorkspaceProvider(tmp_path / "workspaces")
    first = provider.create(_manifest("workspace-one"))
    second = provider.create(_manifest("workspace-two"))
    link = first.root / "strategies" / "other"
    try:
        link.symlink_to(second.root / "strategies", target_is_directory=True)
    except OSError as error:
        pytest.skip(f"directory symlinks unavailable: {error}")

    with pytest.raises(WorkspacePathError, match="escapes"):
        first.resolve_path("strategies/other/new.py")


def test_provider_refuses_workspace_root_inside_another_git_repository(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "tracked"
    parent.mkdir()
    _git(parent, "init", "--quiet")

    with pytest.raises(WorkspacePathError, match="outside"):
        LocalWorkspaceProvider(parent / "workspaces")


def test_open_rejects_manifest_identity_mismatch(tmp_path: Path) -> None:
    provider = LocalWorkspaceProvider(tmp_path / "workspaces")
    workspace = provider.create(_manifest("workspace-one"))
    manifest_path = workspace.root / ".thesis" / "workspace.yaml"
    values = json.loads(manifest_path.read_text(encoding="utf-8"))
    values["workspace_id"] = "workspace-two"
    manifest_path.write_text(json.dumps(values), encoding="utf-8")

    with pytest.raises(WorkspaceRepositoryError, match="identities do not match"):
        provider.open("workspace-one")


def test_create_rejects_a_different_local_repository_identity(tmp_path: Path) -> None:
    provider = LocalWorkspaceProvider(tmp_path / "workspaces")
    manifest = _manifest("workspace-one")
    mismatched = manifest.model_copy(
        update={
            "repository_identity": manifest.repository_identity.model_copy(
                update={"repository_id": "another-repository"}
            )
        }
    )

    with pytest.raises(WorkspaceRepositoryError, match="must match workspace_id"):
        provider.create(mismatched)


def test_git_environment_cannot_redirect_workspace_initialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    redirected = tmp_path / "redirected"
    redirected.mkdir()
    _git(redirected, "init", "--quiet")
    monkeypatch.setenv("GIT_DIR", str(redirected / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(redirected))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(redirected))

    provider = LocalWorkspaceProvider(tmp_path / "workspaces")
    workspace = provider.create(_manifest("workspace-one"))

    assert (workspace.root / ".git").is_dir()
    monkeypatch.delenv("GIT_DIR")
    monkeypatch.delenv("GIT_WORK_TREE")
    assert Path(_git(workspace.root, "rev-parse", "--show-toplevel")).resolve() == (
        workspace.root
    )
