"""Environment-gated flip of harness entrypoints onto Rust binaries.

The Rust migration ports the interfaces in waves. The ``api``, ``mcp``, and
``backtest-job`` processes are complete on the Rust stack. ``trader`` still
drives the Python supervisor: its Rust binary is a deterministic core demo, not
the configured paper/shadow supervision runner, so it is not yet parity-proven
and stays on Python. This module is the single switch that lets an operator run
the ported entrypoints from Rust without changing the Python console scripts.

Enable the flip with ``HARNESS_USE_RUST_ENTRYPOINTS=1`` (or ``true``/``yes``/
``on``). Binary discovery order:

1. ``HARNESS_RUST_BINARY_DIR`` when set.
2. ``src/harness/rust/target/release`` under the repository root.
3. ``src/harness/rust/target/debug`` under the repository root.

The flip is scoped to entrypoints whose Rust implementation is verified
complete:

* ``api``, ``mcp``, and ``backtest-job`` are flipped outright.
* The ``harness`` facade is flipped for its complete process groups only
  (:data:`PORTED_FACADE_GROUPS`): ``harness api``, ``harness mcp``, and
  ``harness backtest-job`` exec the Rust ``harness`` binary, while
  ``harness trader`` stays on the Python runtime.

``backtest-job --data-dir`` is not yet supported by the Rust engine, so that
invocation stays on Python (:func:`rust_entrypoint_supports`). ``trader`` is
wired to the same switch but is absent from :data:`PORTED_ENTRYPOINTS`, and
:data:`DEFERRED_ENTRYPOINTS` records why, so the Python implementation remains
authoritative until the decommission gate opens.

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
PORTED_ENTRYPOINTS = frozenset({"api", "mcp", "backtest-job"})

#: The facade binary that serves the ported process groups.
FACADE_ENTRYPOINT = "harness"

#: Facade process groups that are complete on the Rust stack. The facade
#: dispatches only these groups so unported groups keep the Python engine.
PORTED_FACADE_GROUPS = frozenset({"api", "mcp", "backtest-job"})

#: Entrypoints whose Rust command surface exists but whose engine path is not
#: yet parity-proven. They stay on the Python engine; the reason is recorded so
#: the decommission gate stays explicit.
DEFERRED_ENTRYPOINTS = {
    "trader": "Rust live trader supervision runner is not ported",
}

#: Entrypoints whose Rust engine supports only a subset of the CLI flags.
#: ``backtest-job --data-dir`` reads a local Parquet lake, which the native
#: engine does not yet wire, so that invocation stays on Python.
_DATA_DIR_ENTRYPOINTS = frozenset({"backtest-job"})

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def rust_entrypoints_enabled() -> bool:
    """Return whether the Rust entrypoint flip is enabled."""
    value = os.environ.get(RUST_ENTRYPOINTS_FLAG, "")
    return value.strip().lower() in _TRUTHY


def _requests_data_dir(argv: Sequence[str]) -> bool:
    """Return whether the invocation selects the local-lake ``--data-dir``."""
    return any(token == "--data-dir" or token.startswith("--data-dir=") for token in argv)


def rust_entrypoint_supports(name: str, argv: Sequence[str]) -> bool:
    """Return whether the Rust binary supports this invocation of ``name``.

    All ported entrypoints support their full flag surface except
    ``backtest-job --data-dir``, which the native engine does not yet read.
    """
    if name in _DATA_DIR_ENTRYPOINTS and _requests_data_dir(argv):
        return False
    return True



def _candidate_dirs() -> tuple[Path, ...]:
    configured = os.environ.get(RUST_BINARY_DIR_VARIABLE)
    dirs: list[Path] = []
    if configured:
        dirs.append(Path(configured))
    repository_root = Path(__file__).resolve().parents[3]
    dirs.append(repository_root / "src" / "harness" / "rust" / "target" / "release")
    dirs.append(repository_root / "src" / "harness" / "rust" / "target" / "debug")
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
    Python implementation instead. Only :data:`PORTED_ENTRYPOINTS` ever flip,
    and only when the Rust engine supports the invocation.
    """
    if name not in PORTED_ENTRYPOINTS or not rust_entrypoints_enabled():
        return None
    if not rust_entrypoint_supports(name, argv):
        return None
    return _exec(name, argv)


def exec_rust_facade(argv: Sequence[str]) -> int | None:
    """Exec the Rust ``harness`` binary for a ported facade group.

    Returns ``None`` when the flip is disabled, the invocation selects no
    ported group (for example bare ``harness`` or ``harness --help``), the Rust
    engine does not support the invocation, or the Rust binary is unavailable,
    so the caller runs the Python facade.
    """
    if not rust_entrypoints_enabled():
        return None
    group = facade_group(argv)
    if group not in PORTED_FACADE_GROUPS:
        return None
    if not rust_entrypoint_supports(group, argv):
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
    "rust_entrypoint_supports",
    "rust_entrypoints_enabled",
]
