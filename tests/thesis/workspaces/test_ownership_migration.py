"""Focused tests for local-operator workspace ownership migration."""

from __future__ import annotations

from pathlib import Path

from thesis.identity.owner import derive_owner_account_id
from thesis.workspaces.catalog import (
    LocalWorkspaceCatalogStore,
    migrate_workspace_ownership,
)
from thesis.workspaces.local import LocalWorkspaceProvider
from thesis.workspaces.manifest import (
    WorkspaceManifest,
    load_workspace_manifest,
)

TEMPLATE = Path(__file__).resolve().parents[3] / "config" / "thesis" / "workspace.yaml"
OWNER_ACCOUNT_ID = derive_owner_account_id("owner@example.com")


def _summary(workspace_id: str, name: str) -> dict[str, object]:
    return {
        "id": workspace_id,
        "name": name,
        "description": "Momentum research",
        "initials": "ML",
        "created_at": "2026-09-11T00:00:00Z",
    }


def _catalog(root: Path) -> LocalWorkspaceCatalogStore:
    return LocalWorkspaceCatalogStore(
        LocalWorkspaceProvider(root / "workspaces"),
        load_workspace_manifest(TEMPLATE),
    )


def _manifest_path(root: Path, workspace_id: str) -> Path:
    return root / "workspaces" / workspace_id / ".thesis" / "workspace.yaml"


def _owner_id(root: Path, workspace_id: str) -> str:
    return load_workspace_manifest(_manifest_path(root, workspace_id)).owner.owner_id


def _set_owner(
    catalog: LocalWorkspaceCatalogStore, workspace_id: str, owner_id: str
) -> None:
    manifest = catalog.provider.open(workspace_id).manifest
    values = manifest.model_dump(mode="json")
    values["owner"]["owner_id"] = owner_id
    catalog.replace_manifest(workspace_id, WorkspaceManifest.model_validate(values))


def test_dry_run_lists_workspaces_without_writing(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    catalog.add(_summary("alpha-lab", "Alpha Lab"))
    catalog.add(_summary("beta-lab", "Beta Lab"))
    before = _manifest_path(tmp_path, "alpha-lab").read_text(encoding="utf-8")

    report = migrate_workspace_ownership(
        catalog, owner_account_id=OWNER_ACCOUNT_ID, dry_run=True
    )

    assert report.dry_run is True
    assert report.scanned == 2
    assert report.changed == 2
    assert report.workspace_ids == ("alpha-lab", "beta-lab")
    assert [entry.old_owner_id for entry in report.entries] == [
        "local-operator",
        "local-operator",
    ]
    assert [entry.new_owner_id for entry in report.entries] == [
        OWNER_ACCOUNT_ID,
        OWNER_ACCOUNT_ID,
    ]
    assert _manifest_path(tmp_path, "alpha-lab").read_text(encoding="utf-8") == before
    assert _owner_id(tmp_path, "alpha-lab") == "local-operator"


def test_real_run_reowns_default_workspaces(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    catalog.add(_summary("alpha-lab", "Alpha Lab"))
    catalog.add(_summary("beta-lab", "Beta Lab"))

    report = migrate_workspace_ownership(catalog, owner_account_id=OWNER_ACCOUNT_ID)

    assert report.dry_run is False
    assert report.scanned == 2
    assert report.changed == 2
    assert _owner_id(tmp_path, "alpha-lab") == OWNER_ACCOUNT_ID
    assert _owner_id(tmp_path, "beta-lab") == OWNER_ACCOUNT_ID


def test_second_run_is_a_noop(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    catalog.add(_summary("alpha-lab", "Alpha Lab"))
    migrate_workspace_ownership(catalog, owner_account_id=OWNER_ACCOUNT_ID)
    before = _manifest_path(tmp_path, "alpha-lab").read_text(encoding="utf-8")

    report = migrate_workspace_ownership(catalog, owner_account_id=OWNER_ACCOUNT_ID)

    assert report.changed == 0
    assert report.entries == ()
    assert _manifest_path(tmp_path, "alpha-lab").read_text(encoding="utf-8") == before


def test_migration_preserves_workspace_contents(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    catalog.add(_summary("alpha-lab", "Alpha Lab"))
    workspace_root = tmp_path / "workspaces" / "alpha-lab"
    readme_before = (workspace_root / "README.md").read_text(encoding="utf-8")
    manifest_before = load_workspace_manifest(_manifest_path(tmp_path, "alpha-lab"))

    migrate_workspace_ownership(catalog, owner_account_id=OWNER_ACCOUNT_ID)

    manifest_after = load_workspace_manifest(_manifest_path(tmp_path, "alpha-lab"))
    expected = manifest_before.model_dump(mode="json")
    expected["owner"]["owner_id"] = OWNER_ACCOUNT_ID
    assert manifest_after.model_dump(mode="json") == expected
    assert (workspace_root / "README.md").read_text(encoding="utf-8") == readme_before
    assert (workspace_root / "strategies").is_dir()


def test_other_owners_are_untouched(tmp_path: Path) -> None:
    catalog = _catalog(tmp_path)
    catalog.add(_summary("alpha-lab", "Alpha Lab"))
    catalog.add(_summary("beta-lab", "Beta Lab"))
    _set_owner(catalog, "beta-lab", "other-owner")
    before = _manifest_path(tmp_path, "beta-lab").read_text(encoding="utf-8")

    report = migrate_workspace_ownership(catalog, owner_account_id=OWNER_ACCOUNT_ID)

    assert report.workspace_ids == ("alpha-lab",)
    assert _owner_id(tmp_path, "beta-lab") == "other-owner"
    assert _manifest_path(tmp_path, "beta-lab").read_text(encoding="utf-8") == before
