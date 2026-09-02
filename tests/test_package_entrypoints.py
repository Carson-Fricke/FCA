import subprocess
import sys
from collections.abc import Sequence
from importlib.metadata import entry_points
from pathlib import Path

import pytest
from pytest import MonkeyPatch

import lake
from lake import cli


def test_package_import_and_console_script() -> None:
    assert Path(lake.__file__).parent.name == "lake"
    scripts = entry_points(group="console_scripts", name="lake")
    assert [script.value for script in scripts] == ["lake.cli:main"]


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ([], "{api,ingest}"),
        (["--help"], "{api,ingest}"),
        (["api", "--help"], "Run the market-lake API process."),
        (["ingest", "--help"], "Run the market-lake ingest process."),
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


@pytest.mark.parametrize("command", ["api", "ingest"])
def test_command_dispatch(command: str, monkeypatch: MonkeyPatch) -> None:
    received: list[str] = []

    def handler(argv: Sequence[str] | None = None) -> int:
        received.extend(argv or [])
        return 17

    monkeypatch.setitem(cli._COMMANDS, command, handler)

    assert cli.main([command, "--probe"]) == 17
    assert received == ["--probe"]


def test_api_command_starts_uvicorn(monkeypatch: MonkeyPatch) -> None:
    received: dict[str, object] = {}

    def run(application: str, *, host: str, port: int) -> None:
        received.update(application=application, host=host, port=port)

    monkeypatch.setattr("lake.api.uvicorn.run", run)

    assert cli.main(["api", "--host", "127.0.0.1", "--port", "9000"]) == 0
    assert received == {
        "application": "lake.app:app",
        "host": "127.0.0.1",
        "port": 9000,
    }
