"""Join pinned cargo-chef workspace manifests for the combined harness build.

The harness lockfile remains authoritative. Both workspaces' local crates must
be skeletonized together, because lake HTTP depends back on the harness reader.
This is build metadata composition, never a runtime contract or source rewrite.
"""

import argparse
import json
import re
import tomllib
from pathlib import Path, PurePosixPath


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("harness", type=Path)
    parser.add_argument("lake", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    recipe = json.loads(args.harness.read_text(encoding="utf-8"))
    lake = json.loads(args.lake.read_text(encoding="utf-8"))
    primary, secondary = recipe["skeleton"], lake["skeleton"]
    if primary["config_file"] != secondary["config_file"]:
        raise ValueError("Rust workspaces have different Cargo configuration")
    toolchains = [skeleton["rust_toolchain_file"] for skeleton in (primary, secondary)]
    if any(toolchain[0] != "Toml" for toolchain in toolchains):
        raise ValueError("Rust workspaces require TOML toolchain configuration")
    if tomllib.loads(toolchains[0][1]) != tomllib.loads(toolchains[1][1]):
        raise ValueError("Rust workspaces have different toolchain configuration")
    manifests = primary["manifests"]
    # Chef normalizes workspace versions, while the harness lockfile retains
    # the lake path dependencies' version. Preserve that inherited version.
    inherited = set()
    for manifest in secondary["manifests"]:
        package = tomllib.loads(manifest["contents"]).get("package")
        if package is not None and package["version"] == {"workspace": True}:
            inherited.add(package["name"])
    locked = {
        package["name"]: package["version"]
        for package in tomllib.loads(primary["lock_file"])["package"]
        if "source" not in package
    }
    versions = {version for name, version in locked.items() if name in inherited}
    if len(versions) != 1:
        raise ValueError("Lake path dependencies require one workspace package version")
    inherited_version = versions.pop()
    for manifest in secondary["manifests"]:
        contents = manifest["contents"]
        package = tomllib.loads(contents).get("package")
        if manifest["relative_path"] == "Cargo.toml":
            contents = restore_version(contents, "workspace.package", inherited_version)
        elif (
            package is not None
            and package["name"] in locked
            and isinstance(package["version"], str)
        ):
            contents = restore_version(contents, "package", locked[package["name"]])
        manifests.append(
            {
                **manifest,
                "contents": contents,
                "relative_path": str(
                    PurePosixPath("../lake") / manifest["relative_path"]
                ),
            }
        )
    paths = [manifest["relative_path"] for manifest in manifests]
    if len(set(paths)) != len(paths):
        raise ValueError("Cargo recipe contains duplicate manifests")
    manifests.sort(key=lambda manifest: manifest["relative_path"])
    args.output.write_text(json.dumps(recipe, separators=(",", ":")), encoding="utf-8")


def restore_version(contents: str, table: str, expected: str) -> str:
    contents, replacements = re.subn(
        rf'(?ms)(^\[{re.escape(table)}\]\n.*?^version = ")[^"]+(")',
        rf"\g<1>{expected}\g<2>",
        contents,
        count=1,
    )
    actual = tomllib.loads(contents)
    for section in table.split("."):
        actual = actual[section]
    if replacements != 1 or actual["version"] != expected:
        raise ValueError("Cargo recipe package version is not canonical")
    return contents


if __name__ == "__main__":
    main()
