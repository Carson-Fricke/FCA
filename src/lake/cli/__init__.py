"""Market-lake command-line facade.

The lake process implementation is native Rust; this package only discovers
and execs the ``lake`` binary.
"""

from lake.cli.rust_entrypoints import run_entrypoint


def main(argv: list[str] | None = None) -> int:
    """Exec the Rust ``lake`` binary with the process arguments."""
    return run_entrypoint("lake", argv)


__all__ = ["main"]
