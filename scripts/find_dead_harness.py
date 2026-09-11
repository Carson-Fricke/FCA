"""Reproduce the harness dead-module analysis.

The Rust engine now owns everything under ``src/harness/rust``; only the Python
plugin/product surface should remain in ``src/harness``. This stdlib-only tool
builds an import graph over the repository and reports harness modules that no
product, entrypoint, or kept plugin surface imports.

A module is *prunable* when it is not reachable from the product roots
(``src/thesis``, ``src/lake``, ``src/lakeclient``, ``strategies``, ``scripts``),
the console-script entrypoints, or the explicitly retained plugin surface, and
it is not an ancestor package of a reachable module.

Usage::

    python scripts/find_dead_harness.py            # product-surface analysis
    python scripts/find_dead_harness.py --tests    # also treat tests as roots

Exit status is 1 when prunable modules are found, 0 otherwise, so the check can
guard the tree after the prune.
"""

from __future__ import annotations

import argparse
import ast
import sys
import tomllib
from collections import defaultdict, deque
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
EXCLUDED_DIRS = {
    ".git",
    ".venv",
    ".svelte-kit",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "target",
}
EXCLUDED_ROOTS = (SRC / "harness" / "rust", SRC / "ui")

# The Python plugin/product surface intentionally retained after the Rust port.
KEEP_PREFIXES = (
    "harness.indicators",
    "harness.strategy",
    "harness.screening",
    "harness.testing",
    "harness.cli",
    "harness._native",
)

PRODUCT_PACKAGES = ("thesis", "lake", "lakeclient", "strategies", "scripts")


def iter_python_files() -> list[Path]:
    files: list[Path] = []
    for path in REPO_ROOT.rglob("*.py"):
        if any(part in EXCLUDED_DIRS for part in path.parts):
            continue
        if any(path.is_relative_to(root) for root in EXCLUDED_ROOTS):
            continue
        files.append(path)
    return files


def module_name(path: Path) -> str | None:
    try:
        parts = list(path.relative_to(SRC).parts)
    except ValueError:
        parts = list(path.relative_to(REPO_ROOT).parts)
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = parts[-1][:-3]
    return ".".join(parts) if parts else None


def relative_base(module: str, is_package: bool, level: int) -> str:
    parts = module.split(".")
    if not is_package:
        parts = parts[:-1]
    if level > 1:
        drop = level - 1
        parts = parts[:-drop] if drop <= len(parts) else []
    return ".".join(parts)


def imported_targets(path: Path, module: str) -> set[str]:
    is_package = path.name == "__init__.py"
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError:
        return set()
    targets: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = relative_base(module, is_package, node.level)
                if node.module:
                    base = f"{base}.{node.module}" if base else node.module
                targets.add(base)
                targets.update(
                    f"{base}.{alias.name}" if base else alias.name
                    for alias in node.names
                )
            elif node.module:
                targets.add(node.module)
                targets.update(f"{node.module}.{alias.name}" for alias in node.names)
    return targets


def load_entrypoints() -> set[str]:
    entrypoints = {
        "harness.cli",
        "harness.cli.api",
        "harness.cli.backtest_job",
        "harness.cli.trader",
    }
    pyproject = REPO_ROOT / "pyproject.toml"
    if pyproject.is_file():
        scripts = (
            tomllib.loads(pyproject.read_text(encoding="utf-8"))
            .get("project", {})
            .get("scripts", {})
        )
        for target in scripts.values():
            entrypoints.add(target.split(":", 1)[0])
    return entrypoints


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tests",
        action="store_true",
        help="also treat test modules as import roots",
    )
    args = parser.parse_args(argv)

    file_of_module: dict[str, Path] = {}
    for path in iter_python_files():
        name = module_name(path)
        if name:
            file_of_module[name] = path

    graph: dict[str, set[str]] = defaultdict(set)
    importers: dict[str, set[str]] = defaultdict(set)
    for name, path in file_of_module.items():
        for target in imported_targets(path, name):
            candidate = target
            while candidate:
                if candidate in file_of_module:
                    graph[name].add(candidate)
                    importers[candidate].add(name)
                    break
                candidate = candidate.rpartition(".")[0]

    product_roots = {
        name for name in file_of_module if name.split(".", 1)[0] in PRODUCT_PACKAGES
    }
    product_roots |= load_entrypoints()
    product_roots |= {
        name for name, path in file_of_module.items() if path.name == "__main__.py"
    }
    roots = set(product_roots)
    if args.tests:
        roots |= {name for name in file_of_module if name.startswith("tests.")}

    reachable: set[str] = set()
    queue = deque(roots)
    while queue:
        current = queue.popleft()
        if current in reachable:
            continue
        reachable.add(current)
        queue.extend(graph.get(current, ()))

    # A package is live when any reachable submodule needs it.
    live = set(reachable)
    for name in reachable:
        parts = name.split(".")
        for index in range(1, len(parts)):
            ancestor = ".".join(parts[:index])
            if ancestor in file_of_module:
                live.add(ancestor)

    harness_modules = sorted(
        name
        for name in file_of_module
        if name == "harness" or name.startswith("harness.")
    )
    prunable = [
        name
        for name in harness_modules
        if name not in live
        and not any(
            name == keep or name.startswith(keep + ".") for keep in KEEP_PREFIXES
        )
    ]

    print(f"harness modules analysed : {len(harness_modules)}")
    print(f"reachable from roots     : {len(live & set(harness_modules))}")
    print(f"prunable (dead) modules  : {len(prunable)}")
    for name in prunable:
        others = sorted(importers.get(name, ()))
        print(f"  - {name}  importers={others}")

    if args.tests:
        print("(test modules were treated as import roots)")

    return 1 if prunable else 0


if __name__ == "__main__":
    sys.exit(main())
