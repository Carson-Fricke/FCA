"""Quant-harness backtest job process boundary."""

from collections.abc import Sequence

from harness.cli._runner import run
from harness.cli.backtest import app

__all__ = ["app", "main"]


def main(argv: Sequence[str] | None = None) -> int:
    """Run the standalone backtest job process command."""
    return run(app, prog_name="backtest-job", argv=argv)
