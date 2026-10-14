"""Static dependency boundaries which execution tests cannot prove."""

import ast
from pathlib import Path


def test_runtime_does_not_depend_on_fca_and_inner_layers_do_not_select_adapters() -> (
    None
):
    root = next(
        parent
        for parent in Path(__file__).resolve().parents
        if (parent / "pyproject.toml").is_file()
    )
    runtime = root / "src/thesis_runtime"
    assert (runtime / "sessions/session.py").is_file()
    assert (runtime / "workflows/extensions.py").is_file()
    violations = []
    for path in runtime.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id
                in {
                    "investigation_id",
                    "investigation_ids",
                    "strategy_ids",
                    "experiment_ids",
                    "knowledge_scope",
                    "phase_knowledge",
                }
            ):
                violations.append(
                    f"{path.name}:{node.lineno}: FCA field {node.target.id}"
                )
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            names = (
                [node.module or ""]
                if isinstance(node, ast.ImportFrom)
                else [item.name for item in node.names]
                if isinstance(node, ast.Import)
                else []
            )
            for name in names:
                if name == "thesis" or name.startswith("thesis."):
                    violations.append(
                        f"{path.name}:{node.lineno}: FCA dependency {name}"
                    )
                if path.relative_to(runtime).parts[0] != "adapters" and name.startswith(
                    "thesis_runtime.adapters."
                ):
                    violations.append(
                        f"{path.name}:{node.lineno}: inner layer selects {name}"
                    )
                if "adapters/cosmos" in path.as_posix() and name.startswith(
                    "thesis_runtime.adapters.filesystem"
                ):
                    violations.append(
                        f"{path.name}:{node.lineno}: Cosmos depends on filesystem"
                    )
    assert violations == []
