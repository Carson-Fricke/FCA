"""Quant-harness trader process boundary.

Delegates to the Rust ``trader`` binary. The Rust supervision runner composes
the configured paper/shadow session: configuration loading, session resolution,
live-feed supervision with reconnect and backoff, continuous reconciliation,
kill-switch polling, and durable snapshot save/restore.
"""

from __future__ import annotations

from collections.abc import Sequence

from harness.cli.rust_entrypoints import run_entrypoint


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Rust ``trader`` process and return its exit code."""
    return run_entrypoint("trader", argv)


__all__ = ["main"]
