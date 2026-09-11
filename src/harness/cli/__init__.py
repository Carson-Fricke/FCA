"""Quant-harness command-line facade.

The facade delegates to the Rust ``harness`` binary, which dispatches the
``api``, ``mcp``, and ``backtest-job`` process groups.
"""

from __future__ import annotations

from collections.abc import Sequence

from harness.cli.rust_entrypoints import run_entrypoint


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Rust ``harness`` facade and return its exit code."""
    return run_entrypoint("harness", argv)


__all__ = ["main"]
