"""Quant-harness trader process boundary.

Delegates to the Rust ``trader`` binary. The Rust live core composes the
deterministic risk pipeline, OMS, simulated venue, and portfolio accounting,
but the configured paper/shadow supervision runner (config loading, session
resolution, feed supervision, reconciler, kill-switch polling, and state
snapshots) is not yet ported. The trader process is therefore deferred: the
Rust binary runs the deterministic live-core session demo, and the Python
supervision runtime has been removed with the rest of the Python engine.
"""

from __future__ import annotations

from collections.abc import Sequence

from harness.cli.rust_entrypoints import run_entrypoint


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Rust ``trader`` process and return its exit code."""
    return run_entrypoint("trader", argv)


__all__ = ["main"]
