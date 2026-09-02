"""Quant-harness backtest job process boundary."""

from collections.abc import Sequence

import typer

from harness.cli._runner import run

app = typer.Typer(
    add_completion=False,
    help="Run one deterministic backtest job (planned).",
    invoke_without_command=True,
    no_args_is_help=False,
)


@app.callback()
def launch() -> None:
    """Launch a backtest job when its runtime is available."""
    typer.echo(
        "The quant-harness backtest job is scaffolded but not implemented yet.",
        err=True,
    )
    raise typer.Exit(code=2)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the standalone backtest job process command."""
    return run(app, prog_name="backtest-job", argv=argv)
