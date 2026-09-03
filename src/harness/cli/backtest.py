"""Deterministic local backtest command composition."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Annotated

import typer

from harness.cli._runner import run
from harness.configuration import BacktestConfig, load_configuration
from harness.runtime.backtest import BacktestError, BacktestStatus, run_backtest

app = typer.Typer(
    add_completion=False,
    help="Run one deterministic backtest job.",
    invoke_without_command=True,
    no_args_is_help=False,
)


@app.callback()
def run_job(
    config: Annotated[
        Path | None,
        typer.Option(
            "--config",
            help="Path to a typed backtest configuration document.",
        ),
    ] = None,
    data_dir: Annotated[
        Path | None,
        typer.Option(
            "--data-dir",
            help=(
                "Optional local Parquet lake directory "
                "(date=/asset_class=/data_type=/SYMBOL.parquet). "
                "Omit for the deterministic synthetic benchmark fixture."
            ),
        ),
    ] = None,
    output_dir: Annotated[
        Path | None,
        typer.Option(
            "--output-dir",
            help=(
                "Directory for the canonical run artifact document. "
                "Defaults to .local/backtests/<run_id>."
            ),
        ),
    ] = None,
) -> None:
    """Run one deterministic backtest and write its canonical artifacts."""
    if config is None:
        typer.echo(
            "A backtest configuration is required: backtest-job --config <path>",
            err=True,
        )
        raise typer.Exit(code=2)

    configuration = load_configuration(config)
    if not isinstance(configuration, BacktestConfig):
        typer.echo(
            f"configuration shell must be 'backtest', not {configuration.shell!r}",
            err=True,
        )
        raise typer.Exit(code=2)

    try:
        result = run_backtest(
            configuration,
            data_source=None if data_dir is None else data_dir,
        )
    except BacktestError as error:
        typer.echo(f"Backtest failed: {error}", err=True)
        raise typer.Exit(code=1) from error

    destination = (
        Path(".local") / "backtests" / configuration.run_id
        if output_dir is None
        else output_dir
    )
    destination.mkdir(parents=True, exist_ok=True)
    artifact_path = destination / "backtest.json"
    artifact_path.write_text(result.artifact_json() + "\n", encoding="utf-8")

    typer.echo(f"run_id: {configuration.run_id}")
    typer.echo(f"status: {result.status.value}")
    typer.echo(f"manifest_hash: {result.manifest.manifest_hash}")
    typer.echo(f"artifact_digest: {result.artifact_digest()}")
    typer.echo(f"outcome_digest: {result.outcome_digest()}")
    typer.echo(f"market_events: {result.market_event_count}")
    typer.echo(f"orders: {result.order_count}")
    typer.echo(f"fills: {len(result.fills)}")
    typer.echo(f"artifacts: {artifact_path}")

    if result.status is not BacktestStatus.COMPLETED:
        typer.echo(f"reason: {result.reason}", err=True)
        raise typer.Exit(code=1)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the standalone backtest job process command."""
    return run(app, prog_name="backtest-job", argv=argv)
