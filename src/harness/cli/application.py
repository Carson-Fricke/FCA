"""Top-level quant-harness command composition."""

from collections.abc import Sequence

import typer

from harness.cli._runner import run
from harness.cli.api import app as api_app
from harness.cli.backtest_job import app as backtest_job_app
from harness.cli.trader import app as trader_app

app = typer.Typer(
    add_completion=False,
    help="Run a quant-harness process from the shared FCA runtime image.",
    invoke_without_command=True,
    no_args_is_help=False,
)
app.add_typer(api_app, name="api")
app.add_typer(backtest_job_app, name="backtest-job")
app.add_typer(trader_app, name="trader")


@app.callback()
def show_help(context: typer.Context) -> None:
    """Show the process groups when no group is selected."""
    if context.invoked_subcommand is None:
        typer.echo(context.get_help())


def main(argv: Sequence[str] | None = None) -> int:
    """Run the quant-harness command line."""
    return run(app, prog_name="harness", argv=argv)
