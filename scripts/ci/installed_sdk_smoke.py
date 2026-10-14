"""Build-time smoke for the installed wheel, outside repository discovery."""

import tomllib
from pathlib import Path
from tempfile import TemporaryDirectory

from thesis.persistence.submissions import SqliteSubmissionStore
from thesis.workspaces.local import LocalWorkspaceProvider
from thesis.workspaces.programs import WorkspacePrograms

with TemporaryDirectory() as directory:
    programs = WorkspacePrograms(
        LocalWorkspaceProvider(Path(directory)),
        submissions=SqliteSubmissionStore(Path(":memory:")),
    )
    pending = [programs.sdk_root]
    crates = set()
    while pending:
        root = pending.pop().resolve()
        manifest = tomllib.loads((root / "Cargo.toml").read_text(encoding="utf-8"))
        name = manifest["package"]["name"]
        if name in crates:
            continue
        crates.add(name)
        assert isinstance(manifest["package"]["version"], str)
        assert (root / "src" / "lib.rs").is_file()
        for section in ("dependencies", "dev-dependencies", "build-dependencies"):
            for dependency in manifest.get(section, {}).values():
                if isinstance(dependency, dict):
                    assert not dependency.get("workspace")
                    if "path" in dependency:
                        pending.append(root / dependency["path"])
    assert crates == {
        "harness-strategy-sdk",
        "harness-contracts",
        "num",
        "lake-contracts",
    }
    for topic in ("study", "consumer", "research_views"):
        reference = programs.read_sdk_reference(topic)
        assert reference["sdkAbiVersion"] > 0
        assert len(reference["sdkIdentity"]) == 64
        assert reference["source"]
print(
    "Installed SDK dependency graph, metadata, source references, and templates passed"
)
