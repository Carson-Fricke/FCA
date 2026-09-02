import subprocess
import sys
from collections.abc import Sequence
from importlib import import_module
from importlib.metadata import entry_points
from pathlib import Path

import pytest
from pytest import MonkeyPatch

import lake
from lake import cli
from lake.cli import commands


def test_package_import_and_console_script() -> None:
    assert Path(lake.__file__).parent.name == "lake"
    scripts = entry_points(group="console_scripts", name="lake")
    assert [script.value for script in scripts] == ["lake.cli:main"]


def test_published_capability_facades_import() -> None:
    modules = (
        "lake.app",
        "lake.catalog",
        "lake.cli",
        "lake.configuration",
        "lake.contracts",
        "lake.coverage",
        "lake.crypto",
        "lake.local",
        "lake.options",
        "lake.providers.alpaca",
        "lake.query",
        "lake.queue",
        "lake.storage",
    )

    assert [import_module(name).__name__ for name in modules] == list(modules)


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ([], "{api,ingest,local}"),
        (["--help"], "{api,ingest,local}"),
        (["api", "--help"], "Run the market-lake API process."),
        (["ingest", "--help"], "Run the market-lake ingest process."),
        (
            ["local", "--help"],
            "Run the market lake with local files and no Azure resources.",
        ),
    ],
)
def test_module_command_help(arguments: list[str], expected: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "lake", *arguments],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert expected in result.stdout
    assert result.stderr == ""


@pytest.mark.parametrize("command", ["api", "ingest", "local"])
def test_command_dispatch(command: str, monkeypatch: MonkeyPatch) -> None:
    received: list[str] = []

    def handler(argv: Sequence[str] | None = None) -> int:
        received.extend(argv or [])
        return 17

    monkeypatch.setitem(commands._COMMANDS, command, handler)

    assert cli.main([command, "--probe"]) == 17
    assert received == ["--probe"]


def test_api_command_starts_uvicorn(monkeypatch: MonkeyPatch) -> None:
    received: dict[str, object] = {}

    def run(application: str, *, host: str, port: int) -> None:
        received.update(application=application, host=host, port=port)

    monkeypatch.setattr("lake.cli.api.uvicorn.run", run)

    assert cli.main(["api", "--host", "127.0.0.1", "--port", "9000"]) == 0
    assert received == {
        "application": "lake.app:app",
        "host": "127.0.0.1",
        "port": 9000,
    }
