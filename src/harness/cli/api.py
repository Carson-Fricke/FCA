"""Quant-harness API process boundary.

Delegates to the Rust ``api`` binary (axum HTTP boundary, bearer auth, and
provenance).
"""

from __future__ import annotations

from collections.abc import Sequence

from harness.cli.rust_entrypoints import run_entrypoint


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Rust ``api`` process and return its exit code."""
    return run_entrypoint("api", argv)


__all__ = ["main"]
