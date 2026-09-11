"""Quant-harness backtest job process boundary."""

from collections.abc import Sequence

from harness.cli._runner import run
from harness.cli.backtest import app

__all__ = ["app", "main"]


def main(argv: Sequence[str] | None = None) -> int:
    """Run the standalone backtest job process command.

    ``backtest-job`` is in ``PORTED_ENTRYPOINTS``: when the flip is enabled and
    a Rust binary is available, ``exec_rust_entrypoint`` runs the native engine
    and this function never reaches the Python Typer app. The Python engine is
    retained for the ``--data-dir`` local-lake fixture, which the native engine
    does not yet read; ``rust_entrypoint_supports`` keeps that invocation on
    Python.
    """
    from harness.cli.rust_entrypoints import exec_rust_entrypoint, resolved_argv

    args = resolved_argv(argv)
    rust_result = exec_rust_entrypoint("backtest-job", args)
    if rust_result is not None:
        return rust_result
    return run(app, prog_name="backtest-job", argv=args)
