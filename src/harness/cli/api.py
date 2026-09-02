"""Quant-harness API process boundary."""

from collections.abc import Sequence

import typer

from harness.cli._runner import run

app = typer.Typer(
    add_completion=False,
    help="Serve quant-harness results and job operations (planned).",
    invoke_without_command=True,
    no_args_is_help=False,
)


@app.callback()
def launch() -> None:
    """Launch the quant-harness API process when its application is available."""
    typer.echo(
        "The quant-harness API process is scaffolded but not implemented yet.",
        err=True,
    )
    raise typer.Exit(code=2)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the standalone API process command."""
    return run(app, prog_name="api", argv=argv)
