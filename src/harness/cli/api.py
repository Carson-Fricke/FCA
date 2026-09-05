"""Quant-harness API process boundary."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Annotated

import typer
import uvicorn

from harness.cli._runner import run
from harness.configuration import ApiConfig, load_configuration
from harness.configuration.loader import resolve_environment_reference
from harness.http.application import create_app

app = typer.Typer(
    add_completion=False,
    help="Serve quant-harness results and job operations.",
    invoke_without_command=True,
    no_args_is_help=False,
)


@app.callback()
def serve(
    config: Annotated[
        Path | None,
        typer.Option(
            "--config",
            help="Path to a typed API configuration document.",
        ),
    ] = None,
    host: Annotated[
        str | None,
        typer.Option("--host", help="Override the configured bind host."),
    ] = None,
    port: Annotated[
        int | None,
        typer.Option("--port", help="Override the configured bind port."),
    ] = None,
) -> None:
    """Serve the quant-harness results and job API with Uvicorn."""
    if config is None:
        typer.echo("An API configuration is required: api --config <path>", err=True)
        raise typer.Exit(code=2)

    configuration = load_configuration(config)
    if not isinstance(configuration, ApiConfig):
        typer.echo(
            f"configuration shell must be 'api', not {configuration.shell!r}",
            err=True,
        )
        raise typer.Exit(code=2)
    if configuration.server.workers != 1:
        typer.echo(
            "the api process serves one worker per process;"
            " scale by starting additional processes",
            err=True,
        )
        raise typer.Exit(code=2)

    try:
        bearer = resolve_environment_reference(configuration.auth.bearer_token)
    except ValueError as error:
        typer.echo(f"bearer token could not be resolved: {error}", err=True)
        raise typer.Exit(code=2) from error

    uvicorn.run(
        create_app(bearer_token=bearer.get_secret_value()),
        host=configuration.server.host if host is None else host,
        port=configuration.server.port if port is None else port,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the standalone API process command."""
    return run(app, prog_name="api", argv=argv)
