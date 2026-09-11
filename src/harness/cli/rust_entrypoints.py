"""Environment-gated flip of harness entrypoints onto Rust binaries.

The Rust migration ports the interfaces in waves. The ``api`` and ``mcp``
processes are complete on the Rust stack, while ``backtest-job`` and ``trader``
still drive the Python engine and their Rust engine paths are not yet
parity-proven. This module is the single switch that lets an operator run the
ported entrypoints from Rust without changing the Python console scripts.

Enable the flip with ``HARNESS_USE_RUST_ENTRYPOINTS=1`` (or ``true``/``yes``/
``on``). Binary discovery order:

1. ``HARNESS_RUST_BINARY_DIR`` when set.
2. ``rust/target/release`` under the repository root.
3. ``rust/target/debug`` under the repository root.

The flip is scoped to entrypoints whose Rust implementation is verified
complete:

* ``api`` and ``mcp`` are flipped outright.
* The ``harness`` facade is flipped for its complete process groups only
  (:data:`PORTED_FACADE_GROUPS`): ``harness api`` and ``harness mcp`` exec the
  Rust ``harness`` binary, while ``harness backtest-job`` and ``harness trader``
  stay on the Python engine.

``backtest-job`` and ``trader`` are wired to the same switch but are absent from
:data:`PORTED_ENTRYPOINTS` and :data:`DEFERRED_ENTRYPOINTS` records why, so the
Python implementation remains authoritative until the decommission gate opens.

When the flag is disabled, the entrypoint is unported, or no binary is found,
:func:`exec_rust_entrypoint` and :func:`exec_rust_facade` return ``None`` so the
caller falls back to the Python implementation. The flip never raises and never
partially execs.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

#: Enable the Rust entrypoint flip.
RUST_ENTRYPOINTS_FLAG = "HARNESS_USE_RUST_ENTRYPOINTS"

#: Optional explicit directory holding the Rust binaries.
RUST_BINARY_DIR_VARIABLE = "HARNESS_RUST_BINARY_DIR"

#: Entrypoints whose Rust implementations are complete and parity-verified.
PORTED_ENTRYPOINTS = frozenset({"api", "mcp"})

#: The facade binary that serves the ported process groups.
FACADE_ENTRYPOINT = "harness"

#: Facade process groups that are complete on the Rust stack. The facade
#: dispatches only these groups so unported groups keep the Python engine.
PORTED_FACADE_GROUPS = frozenset({"api", "mcp"})

#: Entrypoints whose Rust command surface exists but whose engine path is not
#: yet parity-proven. They stay on the Python engine; the reason is recorded so
#: the decommission gate stays explicit.
DEFERRED_ENTRYPOINTS = {
    "backtest-job": "Rust backtest engine is not wired or parity-proven",
    "trader": "Rust live trader supervision runner is not ported",
}

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


def resolved_argv(argv: Sequence[str] | None) -> list[str]:
    """Return the process arguments, defaulting to ``sys.argv[1:]``.

    Console scripts invoke ``main()`` with no arguments, so the dispatcher must
    read the real process arguments rather than treating a ``None`` as an empty
    invocation.
    """
    if argv is None:
        return list(sys.argv[1:])
    return list(argv)


def facade_group(argv: Sequence[str]) -> str | None:
    """Return the first positional facade token (the selected process group).

    Returns ``None`` for help-only or empty invocations, which the Python
    facade renders rather than handing to a Rust binary.
    """
    for token in argv:
        if token == "--":
            return None
        if token.startswith("-"):
            continue
        return token
    return None


def _exec(name: str, argv: Sequence[str]) -> int | None:
    binary = rust_binary(name)
    if binary is None:
        return None
    completed = subprocess.run([str(binary), *argv], check=False)
    return completed.returncode


def exec_rust_entrypoint(name: str, argv: Sequence[str]) -> int | None:
    """Exec the Rust binary for ``name`` when the flip is active.

    Returns the process exit code, or ``None`` when the caller should run the
    Python implementation instead. Only :data:`PORTED_ENTRYPOINTS` ever flip.
    """
    if name not in PORTED_ENTRYPOINTS or not rust_entrypoints_enabled():
        return None
    return _exec(name, argv)


def exec_rust_facade(argv: Sequence[str]) -> int | None:
    """Exec the Rust ``harness`` binary for a ported facade group.

    Returns ``None`` when the flip is disabled, the invocation selects no
    ported group (for example bare ``harness`` or ``harness --help``), or the
    Rust binary is unavailable, so the caller runs the Python facade.
    """
    if not rust_entrypoints_enabled():
        return None
    if facade_group(argv) not in PORTED_FACADE_GROUPS:
        return None
    return _exec(FACADE_ENTRYPOINT, argv)


__all__ = [
    "DEFERRED_ENTRYPOINTS",
    "FACADE_ENTRYPOINT",
    "PORTED_ENTRYPOINTS",
    "PORTED_FACADE_GROUPS",
    "RUST_BINARY_DIR_VARIABLE",
    "RUST_ENTRYPOINTS_FLAG",
    "exec_rust_entrypoint",
    "exec_rust_facade",
    "facade_group",
    "resolved_argv",
    "rust_binary",
    "rust_entrypoint_available",
    "rust_entrypoints_enabled",
]
