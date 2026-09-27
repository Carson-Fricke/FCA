"""Resolve and exec the market-lake Rust entrypoint binary.

The lake service is native Rust. The Python console script is a thin shim that
execs the matching Rust binary, so ``lake`` resolves to the Rust process
implementation and never falls back to a deleted Python service.

Binary discovery order:

1. ``LAKE_RUST_BINARY_DIR`` when set.
2. ``src/lake/target/release`` under the repository root.
3. ``src/lake/target/debug`` under the repository root.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

#: Optional explicit directory holding the Rust binary.
RUST_BINARY_DIR_VARIABLE = "LAKE_RUST_BINARY_DIR"

#: The lake process entrypoints served by Rust binaries.
ENTRYPOINT_BINARIES: tuple[str, ...] = ("lake",)


def _candidate_dirs() -> tuple[Path, ...]:
    configured = os.environ.get(RUST_BINARY_DIR_VARIABLE)
    dirs: list[Path] = []
    if configured:
        dirs.append(Path(configured))
    repository_root = Path(__file__).resolve().parents[3]
    dirs.append(repository_root / "src" / "lake" / "target" / "release")
    dirs.append(repository_root / "src" / "lake" / "target" / "debug")
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
        raise ValueError(f"unknown lake entrypoint {name!r}")
    binary = rust_binary(name)
    if binary is None:
        print(
            f"{name}: the Rust {name} binary is not built; run "
            "`cargo build --workspace` in src/lake",
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
