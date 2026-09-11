"""Resolve and exec the quant-harness Rust entrypoint binaries.

The engine and the harness process interfaces are native Rust. The Python
console scripts are thin shims that exec the matching Rust binary, so
``harness``, ``api``, ``backtest-job``, and ``trader`` all resolve to the Rust
process implementations.

``trader`` resolves to the Rust ``trader`` binary, which runs the configured
paper/shadow supervision runner (configuration loading, session lifecycle,
live-feed supervision with reconnect and backoff, continuous reconciliation,
kill-switch polling, and snapshot save/restore).

Binary discovery order:

1. ``HARNESS_RUST_BINARY_DIR`` when set.
2. ``src/harness/target/release`` under the repository root.
3. ``src/harness/target/debug`` under the repository root.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

#: Optional explicit directory holding the Rust binaries.
RUST_BINARY_DIR_VARIABLE = "HARNESS_RUST_BINARY_DIR"

#: The harness process entrypoints served by Rust binaries.
ENTRYPOINT_BINARIES: tuple[str, ...] = (
    "harness",
    "api",
    "mcp",
    "backtest-job",
    "trader",
)


def _candidate_dirs() -> tuple[Path, ...]:
    configured = os.environ.get(RUST_BINARY_DIR_VARIABLE)
    dirs: list[Path] = []
    if configured:
        dirs.append(Path(configured))
    repository_root = Path(__file__).resolve().parents[3]
    dirs.append(repository_root / "src" / "harness" / "target" / "release")
    dirs.append(repository_root / "src" / "harness" / "target" / "debug")
    return tuple(dirs)


def rust_binary(name: str) -> Path | None:
    """Return the path to a built Rust entrypoint binary, or ``None``."""
    executable = f"{name}.exe" if os.name == "nt" else name
    for directory in _candidate_dirs():
        candidate = directory / executable
        if candidate.is_file():
            return candidate
    return None


def resolved_argv(argv: Sequence[str] | None) -> list[str]:
    """Return the process arguments, defaulting to ``sys.argv[1:]``."""
    if argv is None:
        return list(sys.argv[1:])
    return list(argv)


def run_entrypoint(name: str, argv: Sequence[str] | None = None) -> int:
    """Exec the Rust binary for ``name`` and return its exit code.

    Returns exit code 2 with an actionable message when the binary has not
    been built, so the shim never silently falls back to a deleted Python
    implementation.
    """
    if name not in ENTRYPOINT_BINARIES:
        raise ValueError(f"unknown harness entrypoint {name!r}")
    binary = rust_binary(name)
    if binary is None:
        print(
            f"{name}: the Rust {name} binary is not built; run "
            "`cargo build --workspace` in src/harness",
            file=sys.stderr,
        )
        return 2
    completed = subprocess.run([str(binary), *resolved_argv(argv)], check=False)
    return completed.returncode


__all__ = [
    "ENTRYPOINT_BINARIES",
    "RUST_BINARY_DIR_VARIABLE",
    "resolved_argv",
    "run_entrypoint",
    "rust_binary",
]
