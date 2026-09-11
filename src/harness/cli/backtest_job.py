"""Quant-harness backtest job process boundary.

Delegates to the Rust ``backtest-job`` binary, which drives the native engine
end to end over the configured ``strategy_instances``.
"""

from __future__ import annotations

from collections.abc import Sequence

from harness.cli.rust_entrypoints import run_entrypoint


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Rust ``backtest-job`` process and return its exit code."""
    return run_entrypoint("backtest-job", argv)


__all__ = ["main"]
