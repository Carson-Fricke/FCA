"""Quant-harness backtest job process boundary."""

from collections.abc import Sequence

from harness.cli._runner import run
from harness.cli.backtest import app

__all__ = ["app", "main"]


def main(argv: Sequence[str] | None = None) -> int:
    """Run the standalone backtest job process command.

    ``backtest-job`` is wired to the Rust entrypoint switch but stays on the
    Python engine: the Rust backtest engine is not yet wired to this command or
    parity-proven, so it is absent from ``PORTED_ENTRYPOINTS`` and
    ``exec_rust_entrypoint`` returns ``None``. Wiring it here keeps the flip a
    one-line gate change once the decommission criteria are met.
    """
    from harness.cli.rust_entrypoints import exec_rust_entrypoint, resolved_argv

    args = resolved_argv(argv)
    rust_result = exec_rust_entrypoint("backtest-job", args)
    if rust_result is not None:
        return rust_result
    return run(app, prog_name="backtest-job", argv=args)
