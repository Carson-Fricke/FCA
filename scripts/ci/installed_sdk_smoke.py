"""Build-time smoke for the installed wheel, outside repository discovery."""

import tomllib
from pathlib import Path
from tempfile import TemporaryDirectory

from thesis.persistence.submissions import SqliteSubmissionStore
from thesis.workspaces.local import LocalWorkspaceProvider
from thesis.workspaces.programs import WorkspacePrograms
from thesis.workspaces.sdk import SdkReferences

with TemporaryDirectory() as directory:
    sdk = SdkReferences()
    programs = WorkspacePrograms(
        LocalWorkspaceProvider(Path(directory)),
        submissions=SqliteSubmissionStore(Path(":memory:")),
        sdk=sdk,
    )
    pending = [sdk.root]
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
        for source in [root / "Cargo.toml", *(root / "src").rglob("*.rs")]:
            assert b"\r\n" not in source.read_bytes(), source
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
    index = programs.read_sdk_reference("index")
    assert "strategy-sdk/src/building_blocks.rs" in index["topics"]
    for topic in (
        "study",
        "consumer",
        "research_views",
        "strategy-sdk/src/building_blocks.rs",
    ):
        reference = programs.read_sdk_reference(topic)
        assert isinstance(reference["sdkAbiVersion"], int)
        assert reference["sdkAbiVersion"] > 0
        assert isinstance(reference["sdkIdentity"], str)
        assert len(reference["sdkIdentity"]) == 64
        assert reference["source"]
print(
    "Installed SDK dependency graph, metadata, source references, and templates passed"
)
