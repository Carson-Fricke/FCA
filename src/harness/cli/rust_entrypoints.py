"""Environment-gated flip of ported harness entrypoints onto Rust binaries.

The Rust migration ports the interfaces in waves. The ``api`` and ``mcp``
processes are complete on the Rust stack, while ``backtest-job``, ``trader``,
and the bare ``harness`` facade still drive the Python engine. This module is
the single switch that lets an operator run the ported entrypoints from Rust
without changing the Python console scripts.

Enable the flip with ``HARNESS_USE_RUST_ENTRYPOINTS=1`` (or ``true``/``yes``/
``on``). Binary discovery order:

1. ``HARNESS_RUST_BINARY_DIR`` when set.
2. ``rust/target/release`` under the repository root.
3. ``rust/target/debug`` under the repository root.

When the flag is disabled, the entrypoint is unported, or no binary is found,
:func:`exec_rust_entrypoint` returns ``None`` so the caller falls back to the
Python implementation. The flip never raises and never partially execs.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Sequence

#: Enable the Rust entrypoint flip.
RUST_ENTRYPOINTS_FLAG = "HARNESS_USE_RUST_ENTRYPOINTS"

#: Optional explicit directory holding the Rust binaries.
RUST_BINARY_DIR_VARIABLE = "HARNESS_RUST_BINARY_DIR"

#: Entrypoints whose Rust implementations are complete.
PORTED_ENTRYPOINTS = frozenset({"api", "mcp"})

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def rust_entrypoints_enabled() -> bool:
    """Return whether the Rust entrypoint flip is enabled."""
    value = os.environ.get(RUST_ENTRYPOINTS_FLAG, "")
    return value.strip().lower() in _TRUTHY


def _candidate_dirs() -> tuple[Path, ...]:
    configured = os.environ.get(RUST_BINARY_DIR_VARIABLE)
    dirs: list[Path] = []
    if configured:
        dirs.append(Path(configured))
    repository_root = Path(__file__).resolve().parents[3]
    dirs.append(repository_root / "rust" / "target" / "release")
    dirs.append(repository_root / "rust" / "target" / "debug")
    return tuple(dirs)


def rust_binary(name: str) -> Path | None:
    """Return the path to a Rust entrypoint binary, or ``None``."""
    executable = f"{name}.exe" if os.name == "nt" else name
    for directory in _candidate_dirs():
        candidate = directory / executable
        if candidate.is_file():
            return candidate
    return None


def rust_entrypoint_available(name: str) -> bool:
    """Return whether a ported Rust binary is available for ``name``."""
    return name in PORTED_ENTRYPOINTS and rust_binary(name) is not None


def exec_rust_entrypoint(name: str, argv: Sequence[str]) -> int | None:
    """Exec the Rust binary for ``name`` when the flip is active.

    Returns the process exit code, or ``None`` when the caller should run the
    Python implementation instead.
    """
    if name not in PORTED_ENTRYPOINTS or not rust_entrypoints_enabled():
        return None
    binary = rust_binary(name)
    if binary is None:
        return None
    completed = subprocess.run([str(binary), *argv], check=False)
    return completed.returncode


__all__ = [
    "PORTED_ENTRYPOINTS",
    "RUST_BINARY_DIR_VARIABLE",
    "RUST_ENTRYPOINTS_FLAG",
    "exec_rust_entrypoint",
    "rust_binary",
    "rust_entrypoint_available",
    "rust_entrypoints_enabled",
]
