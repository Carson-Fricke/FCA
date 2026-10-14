"""Architecture test: the screening package never touches market data.

Thesis is a thin screener client: definitions and imported results only.  No
module under ``src/thesis/screening`` may import the lake, the lake client, or
the harness package, and no evaluation happens in process.
"""

from __future__ import annotations

import ast
import pathlib

_FORBIDDEN_ROOTS = frozenset({"lake", "lakeclient", "harness"})
_PACKAGE = (
    next(
        parent
        for parent in pathlib.Path(__file__).resolve().parents
        if (parent / "pyproject.toml").is_file()
    )
    / "src"
    / "thesis"
    / "screening"
)


def _imported_modules(path: pathlib.Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.append(node.module)
    return modules


def test_screening_package_has_no_market_data_or_harness_imports() -> None:
    modules = sorted(_PACKAGE.glob("*.py"))
    assert modules, f"no modules found under {_PACKAGE}"

    violations: list[str] = []
    for path in modules:
        for module in _imported_modules(path):
            if module.split(".")[0] in _FORBIDDEN_ROOTS:
                violations.append(f"{path.name} imports {module!r}")

    assert violations == []
