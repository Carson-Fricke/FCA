from importlib import import_module
from inspect import signature
from pathlib import Path

import thesis
from harness.configuration import load_yaml_mapping
from thesis.persistence import GitCoordinator
from thesis.workspaces import load_workspace_manifest


def test_thesis_capability_facades_import() -> None:
    capabilities = (
        "agent",
        "artifacts",
        "auth",
        "chat",
        "cleo",
        "execution",
        "http",
        "integrations",
        "operations",
        "persistence",
        "providers",
        "strategies",
        "workspaces",
    )

    assert thesis.__all__ == ()
    assert all(import_module(f"thesis.{name}").__all__ for name in capabilities)


def test_workspace_runtime_and_publication_contracts_are_distinct() -> None:
    root = Path(__file__).parents[2]
    manifest = load_workspace_manifest(root / "config" / "thesis" / "workspace.yaml")
    runtime = load_yaml_mapping(root / "config" / "thesis" / "runtime.yaml")
    profiles = runtime["runtime_profiles"]

    assert isinstance(profiles, dict)
    assert runtime["default_runtime_profile"] == manifest.runtime_profile.profile_id
    assert profiles[manifest.runtime_profile.profile_id]["version"] == (
        manifest.runtime_profile.version
    )
    assert signature(GitCoordinator).parameters["manifest_path"].default == (
        ".thesis/publication.json"
    )
