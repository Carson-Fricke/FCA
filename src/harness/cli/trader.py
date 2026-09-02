"""Quant-harness trader process boundary."""

from collections.abc import Sequence

import typer

from harness.cli._runner import run

app = typer.Typer(
    add_completion=False,
    help="Run a paper, shadow, or capability-gated live trader (planned).",
    invoke_without_command=True,
    no_args_is_help=False,
)


@app.callback()
def launch() -> None:
    """Launch a trader process when its runtime is available."""
    typer.echo(
        "The quant-harness trader process is scaffolded but not implemented yet.",
        err=True,
    )
    raise typer.Exit(code=2)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the standalone trader process command."""
    return run(app, prog_name="trader", argv=argv)
