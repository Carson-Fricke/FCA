"""Public, mode-independent strategy contracts and plugin surface.

A strategy is a Python class deriving from :class:`~harness.strategy.contracts.Strategy`
and implementing the documented callbacks. The engine -- whether the Python
runtime or the native Rust engine through PyO3 -- supplies the strategy a
constrained context object matching
:class:`~harness.strategy.contracts.StrategyContext`. The plugin surface is
deliberately free of engine imports, so authoring a strategy only pulls in the
contract, the sandbox lint, and the reference fixture.
"""

from harness.strategy.contracts import CALLBACK_ORDER, Strategy, StrategyContext
from harness.strategy.lint import (
    ASYNC_SYNTAX_RULE,
    RULES,
    LintReport,
    SandboxRule,
    SandboxViolation,
    SandboxViolationError,
    StrategyLintError,
    StrategyTarget,
    ViolationCategory,
    assert_sandbox_clean,
    iter_strategy_files,
    lint_strategy,
    lint_strategy_class,
    lint_strategy_directory,
    lint_strategy_file,
    lint_strategy_module,
    lint_strategy_source,
)
from harness.strategy.reference import ReferenceStrategy

__all__ = [
    "ASYNC_SYNTAX_RULE",
    "CALLBACK_ORDER",
    "LintReport",
    "RULES",
    "ReferenceStrategy",
    "SandboxRule",
    "SandboxViolation",
    "SandboxViolationError",
    "Strategy",
    "StrategyContext",
    "StrategyLintError",
    "StrategyTarget",
    "ViolationCategory",
    "assert_sandbox_clean",
    "iter_strategy_files",
    "lint_strategy",
    "lint_strategy_class",
    "lint_strategy_directory",
    "lint_strategy_file",
    "lint_strategy_module",
    "lint_strategy_source",
]
