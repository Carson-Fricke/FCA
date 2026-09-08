import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

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
        ("trader", "Run one supervised paper or shadow trading session"),
    ],
)
def test_process_group_help(command: str, description: str) -> None:
    result = runner.invoke(cli.app, [command, "--help"])

    assert result.exit_code == 0
    assert description in result.stdout


def test_trader_requires_a_configuration() -> None:
    result = runner.invoke(trader.app)

    assert result.exit_code != 0


def test_backtest_job_requires_a_configuration() -> None:
    result = runner.invoke(cli.app, ["backtest-job"])

    assert result.exit_code == 2
    assert "--config" in result.stderr


def test_api_requires_a_configuration() -> None:
    result = runner.invoke(cli.app, ["api"])

    assert result.exit_code == 2
    assert "--config" in result.stderr


def test_api_rejects_a_non_api_shell(tmp_path) -> None:
    config_path = tmp_path / "backtest.yaml"
    config_path.write_text(
        (Path(__file__).parents[3] / "config" / "harness" / "backtest.yaml").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )

    result = runner.invoke(
        cli.app,
        ["api", "--config", str(config_path)],
    )

    assert result.exit_code == 2
    assert "shell must be 'api'" in result.stderr


def test_api_fails_clearly_when_the_bearer_variable_is_missing(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "api.yaml"
    config_path.write_text(
        (Path(__file__).parents[3] / "config" / "harness" / "api.yaml").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("HARNESS_API_BEARER_TOKEN", raising=False)

    result = runner.invoke(cli.app, ["api", "--config", str(config_path)])

    assert result.exit_code == 2
    assert "HARNESS_API_BEARER_TOKEN" in result.stderr


def test_backtest_job_rejects_a_non_backtest_shell(tmp_path) -> None:
    config_path = tmp_path / "api.yaml"
    config_path.write_text(
        (Path(__file__).parents[3] / "config" / "harness" / "api.yaml").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )

    result = runner.invoke(
        cli.app,
        ["backtest-job", "--config", str(config_path)],
    )

    assert result.exit_code == 2
    assert "shell must be 'backtest'" in result.stderr


def test_backtest_job_runs_the_synthetic_benchmark_identically(tmp_path) -> None:
    config_path = tmp_path / "backtest.yaml"
    config_path.write_text(_benchmark_configuration(), encoding="utf-8")

    first = runner.invoke(
        cli.app,
        [
            "backtest-job",
            "--config",
            str(config_path),
            "--output-dir",
            str(tmp_path / "first"),
        ],
    )
    second = runner.invoke(
        cli.app,
        [
            "backtest-job",
            "--config",
            str(config_path),
            "--output-dir",
            str(tmp_path / "second"),
        ],
    )

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    assert "status: completed" in first.stdout
    first_digest = _stdout_value(first.stdout, "artifact_digest")
    second_digest = _stdout_value(second.stdout, "artifact_digest")
    assert first_digest == second_digest
    first_artifact = (tmp_path / "first" / "backtest.json").read_text(encoding="utf-8")
    second_artifact = (tmp_path / "second" / "backtest.json").read_text(
        encoding="utf-8"
    )
    assert first_artifact == second_artifact


def _stdout_value(stdout: str, key: str) -> str:
    for line in stdout.splitlines():
        name, _, value = line.partition(": ")
        if name == key:
            return value.strip()
    raise AssertionError(f"missing {key!r} in output: {stdout!r}")


def _benchmark_configuration() -> str:
    example = (
        Path(__file__).parents[3] / "config" / "harness" / "backtest.yaml"
    ).read_text(encoding="utf-8")
    return example.replace(
        "implementation: strategies.reference.momentum:MomentumStrategy",
        "implementation: harness.strategy.reference:ReferenceStrategy",
    )


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
