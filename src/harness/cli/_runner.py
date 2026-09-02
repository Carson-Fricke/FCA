"""Internal support for console and module entrypoints."""

from collections.abc import Sequence

import typer


def run(
    app: typer.Typer,
    *,
    prog_name: str,
    argv: Sequence[str] | None = None,
) -> int:
    """Invoke a Typer application without leaking Click exceptions to callers."""
    try:
        app(args=None if argv is None else list(argv), prog_name=prog_name)
    except SystemExit as error:
        return error.code if isinstance(error.code, int) else 1
    return 0
