"""The strategy plugin surface must stay independent of the deleted engine.

The native Rust engine owns the strategy context, so `harness.strategy` is a
protocol-only surface. This test pins that boundary statically: no runtime
import in the package may reach an engine module that was deleted with the
Python engine.
"""

from __future__ import annotations

import ast
from pathlib import Path

STRATEGY_PACKAGE = Path(__file__).resolve().parents[3] / "src" / "harness" / "strategy"

DELETED_ENGINE_PREFIXES = (
    "harness.runtime",
    "harness.oms",
    "harness.fills",
    "harness.portfolio",
    "harness.risk",
    "harness.venues",
    "harness.results",
    "harness.sweeps",
    "harness.http",
    "harness.mcp",
    "harness.quant",
    "harness.signal",
    "harness.strategy.context",
    "harness.strategy.binding",
)


def _runtime_imports(source: str) -> set[str]:
    tree = ast.parse(source)
    targets: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            targets.add(node.module)
    return targets


def test_strategy_package_does_not_import_deleted_engine_modules() -> None:
    violations: list[str] = []
    for path in sorted(STRATEGY_PACKAGE.rglob("*.py")):
        targets = _runtime_imports(path.read_text(encoding="utf-8"))
        for target in sorted(targets):
            if any(
                target == prefix or target.startswith(prefix + ".")
                for prefix in DELETED_ENGINE_PREFIXES
            ):
                violations.append(f"{path.name}: imports {target}")
    assert violations == [], "\n".join(violations)


def test_strategy_package_exports_the_protocol_contract() -> None:
    from harness.strategy import CALLBACK_ORDER, Strategy, StrategyContext

    assert "on_start" in CALLBACK_ORDER
    assert "on_stop" in CALLBACK_ORDER
    assert StrategyContext is not None
    assert isinstance(Strategy, type)
