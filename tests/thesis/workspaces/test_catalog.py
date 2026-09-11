"""Focused tests for the filesystem-backed local workspace catalog."""

from __future__ import annotations

from pathlib import Path

from thesis.workspaces.catalog import LocalWorkspaceCatalogStore
from thesis.workspaces.local import LocalWorkspaceProvider
from thesis.workspaces.manifest import load_workspace_manifest

TEMPLATE = Path(__file__).resolve().parents[3] / "config" / "thesis" / "workspace.yaml"


def _summary(workspace_id: str = "alpha-lab") -> dict[str, object]:
    return {
        "id": workspace_id,
        "name": "Alpha Lab",
        "description": "Momentum research",
        "initials": "AL",
        "created_at": "2026-09-11T00:00:00Z",
    }


def _catalog(root: Path) -> LocalWorkspaceCatalogStore:
    return LocalWorkspaceCatalogStore(
        LocalWorkspaceProvider(root / "workspaces"),
        load_workspace_manifest(TEMPLATE),
    )


def test_create_provisions_a_literal_folder(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)

    created = catalog.add(_summary())

    assert created["id"] == "alpha-lab"
    folder = tmp_path / "workspaces" / "alpha-lab"
    assert (folder / ".thesis" / "workspace.yaml").is_file()
    assert (folder / "strategies").is_dir()


def test_workspaces_survive_a_fresh_catalog(tmp_path: Path) -> None:
    _catalog(tmp_path).add(_summary())

    # A new store over the same root models a process restart.
    reopened = _catalog(tmp_path)
    listed = reopened.list()

    assert [row["id"] for row in listed] == ["alpha-lab"]
    assert reopened.get("alpha-lab") is not None
    assert reopened.get("alpha-lab")["name"] == "Alpha Lab"  # type: ignore[index]


def test_activity_is_persisted(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    catalog.add(_summary())

    catalog.add_activity(
        {
            "id": "act-1",
            "workspace_id": "alpha-lab",
            "title": "Created",
            "description": "workspace created",
            "type": "chat",
            "created_at": "2026-09-11T00:00:00Z",
        }
    )

    assert [entry["id"] for entry in _catalog(tmp_path).activity("alpha-lab")] == [
        "act-1"
    ]


def test_unknown_workspace_returns_none(tmp_path: Path) -> None:
    assert _catalog(tmp_path).get("missing-ws") is None
