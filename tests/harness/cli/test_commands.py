import subprocess
import sys
from collections.abc import Callable, Sequence

import pytest
from typer.testing import CliRunner

from harness import cli
from harness.cli import api, backtest_job, trader

runner = CliRunner()


def test_root_help_lists_process_groups() -> None:
    result = runner.invoke(cli.app, ["--help"])

    assert result.exit_code == 0
    assert "Run a quant-harness process" in result.stdout
    assert "api" in result.stdout
    assert "backtest-job" in result.stdout
    assert "trader" in result.stdout


def test_no_arguments_shows_root_help() -> None:
    result = runner.invoke(cli.app)

    assert result.exit_code == 0
    assert "backtest-job" in result.stdout


@pytest.mark.parametrize(
    ("command", "description"),
    [
        ("api", "Serve quant-harness results and job operations"),
        ("backtest-job", "Run one deterministic backtest job"),
        ("trader", "Run a paper, shadow, or capability-gated live trader"),
    ],
)
def test_process_group_help(command: str, description: str) -> None:
    result = runner.invoke(cli.app, [command, "--help"])

    assert result.exit_code == 0
    assert description in result.stdout


@pytest.mark.parametrize("command", ["api", "backtest-job", "trader"])
def test_unimplemented_process_fails_explicitly(command: str) -> None:
    result = runner.invoke(cli.app, [command])

    assert result.exit_code == 2
    assert "scaffolded but not implemented yet" in result.stderr


@pytest.mark.parametrize("entrypoint", [api.main, backtest_job.main, trader.main])
def test_standalone_process_help(
    entrypoint: Callable[[Sequence[str] | None], int],
) -> None:
    assert entrypoint(["--help"]) == 0


def test_module_entrypoint_help() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "harness", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "backtest-job" in result.stdout
    assert result.stderr == ""
