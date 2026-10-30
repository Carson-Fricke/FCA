"""Protect SDK/lifecycle dependency direction, which behavior tests cannot prove."""

import ast
from pathlib import Path


def imports(path: Path) -> set[str]:
    """Read static dependencies without importing implementation modules."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        module
        for node in ast.walk(tree)
        for module in (
            [node.module or ""]
            if isinstance(node, ast.ImportFrom)
            else [item.name for item in node.names]
            if isinstance(node, ast.Import)
            else []
        )
    }


def test_sdk_has_no_dependency_on_program_lifecycle_or_submission_owners() -> None:
    root = Path("src/thesis/workspaces/sdk")
    assert root.is_dir(), "the SDK capability must exist"
    assert (root / "references.py").is_file(), "the SDK reference owner must exist"
    forbidden = (
        "thesis.workspaces.programs",
        "thesis.workspaces.repository",
        "thesis.workspaces.local",
        "thesis.persistence",
        "thesis.application",
        "thesis_runtime.conversations",
        "thesis_runtime.workflows",
    )
    violations = {
        path.name: sorted(
            module
            for module in imports(path)
            if any(
                module == prefix or module.startswith(prefix + ".")
                for prefix in forbidden
            )
        )
        for path in root.glob("*.py")
    }
    assert not {name: modules for name, modules in violations.items() if modules}


def test_program_lifecycle_consumes_sdk_port_and_stateless_source_read_only() -> None:
    dependencies = {
        module
        for module in imports(Path("src/thesis/workspaces/programs.py"))
        if module == "thesis.workspaces.sdk"
        or module.startswith("thesis.workspaces.sdk.")
    }
    assert dependencies <= {
        "thesis.workspaces.sdk.ports",
        "thesis.workspaces.sdk.reading",
    }, (
        "SDK construction, source discovery and response decoding belong outside lifecycle policy"
    )
