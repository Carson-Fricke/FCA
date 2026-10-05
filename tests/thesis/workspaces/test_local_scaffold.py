"""Local workspace scaffolding keeps durable workspace artifacts."""

from pathlib import Path

from thesis.workspaces.local import WORKSPACE_DIRECTORIES, LocalWorkspaceProvider
from thesis.workspaces.manifest import load_workspace_manifest


def test_scaffold_contains_only_supported_workspace_directories(tmp_path: Path) -> None:
    manifest = load_workspace_manifest("config/thesis/workspace.yaml")
    LocalWorkspaceProvider._write_scaffold(tmp_path, manifest)
    assert {entry.name for entry in tmp_path.iterdir() if entry.is_dir()} == {
        ".thesis",
        "chats",
        "investigations",
        "strategies",
        "experiments",
        "indicators",
        "models",
        "knowledge",
        "backtests",
        "uploads",
        "activations",
    }
    for relative in WORKSPACE_DIRECTORIES:
        assert (tmp_path / relative / ".gitkeep").is_file()
    assert (tmp_path / ".thesis/workspace.yaml").is_file()
    assert (tmp_path / ".thesis/platform.lock").is_file()
