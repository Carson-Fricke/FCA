"""Join pinned cargo-chef workspace manifests for either Rust image build.

The selected workspace's lockfile remains authoritative. Both workspaces' local
crates must be skeletonized together, because lake HTTP depends back on the
harness reader.
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
    parser.add_argument("--workspace", choices=("harness", "lake"), required=True)
    args = parser.parse_args()
    recipes = {
        "harness": json.loads(args.harness.read_text(encoding="utf-8")),
        "lake": json.loads(args.lake.read_text(encoding="utf-8")),
    }
    sibling = "lake" if args.workspace == "harness" else "harness"
    recipe = recipes[args.workspace]
    primary, secondary = recipe["skeleton"], recipes[sibling]["skeleton"]
    if primary["config_file"] != secondary["config_file"]:
        raise ValueError("Rust workspaces have different Cargo configuration")
    toolchains = [skeleton["rust_toolchain_file"] for skeleton in (primary, secondary)]
    if any(toolchain[0] != "Toml" for toolchain in toolchains):
        raise ValueError("Rust workspaces require TOML toolchain configuration")
    if tomllib.loads(toolchains[0][1]) != tomllib.loads(toolchains[1][1]):
        raise ValueError("Rust workspaces have different toolchain configuration")
    manifests = primary["manifests"]
    # Chef normalizes workspace members, while the primary lockfile retains
    # external path dependencies' versions. Restore those secondary versions.
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
    if inherited and len(versions) != 1:
        raise ValueError("Path dependencies require one workspace package version")
    inherited_version = versions.pop() if versions else None
    for manifest in secondary["manifests"]:
        contents = manifest["contents"]
        package = tomllib.loads(contents).get("package")
        if manifest["relative_path"] == "Cargo.toml" and inherited_version is not None:
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
                    PurePosixPath("..") / sibling / manifest["relative_path"]
                ),
            }
        )
    paths = [manifest["relative_path"] for manifest in manifests]
    if len(set(paths)) != len(paths):
        raise ValueError("Cargo recipe contains duplicate manifests")
    # Production images build binaries only. Chef records auto-discovered test
    # targets in both the target list and TOML, making a new test invalidate an
    # otherwise identical dependency layer. Retain the locked dependency graph
    # and all production targets; tests still compile in the dedicated CI jobs.
    for manifest in manifests:
        manifest["targets"] = [
            target
            for target in manifest["targets"]
            if not any(
                target["kind"] == excluded
                or isinstance(target["kind"], dict)
                and excluded in target["kind"]
                for excluded in ("Test", "Bench", "Example")
            )
        ]
        contents = re.sub(
            r"(?ms)^\[\[(?:test|bench|example)\]\]\n.*?(?=^\[|\Z)",
            "",
            manifest["contents"],
        )
        contents = re.sub(r"\n{3,}", "\n\n", contents).strip() + "\n"
        expected = tomllib.loads(manifest["contents"])
        for target in ("test", "bench", "example"):
            expected.pop(target, None)
        if tomllib.loads(contents) != expected:
            raise ValueError("Production recipe changed non-target Cargo metadata")
        manifest["contents"] = contents
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
