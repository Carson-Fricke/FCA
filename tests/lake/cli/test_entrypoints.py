"""The lake console script is a thin shim over the Rust ``lake`` binary."""

import os
import subprocess
import sys
from importlib import import_module
from importlib.metadata import entry_points
from pathlib import Path
from types import SimpleNamespace

import pytest
from pytest import MonkeyPatch

import lake
from lake import cli
from lake.cli import rust_entrypoints


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
        "lake.operations",
        "lake.options",
        "lake.providers.alpaca",
        "lake.query",
        "lake.queue",
        "lake.storage",
    )

    assert [import_module(name).__name__ for name in modules] == list(modules)


def test_entrypoint_binaries_cover_the_console_script() -> None:
    assert rust_entrypoints.ENTRYPOINT_BINARIES == ("lake",)
    assert rust_entrypoints.RUST_BINARY_DIR_VARIABLE == "LAKE_RUST_BINARY_DIR"


def test_main_execs_the_resolved_binary(tmp_path, monkeypatch: MonkeyPatch) -> None:
    binary = tmp_path / ("lake.exe" if os.name == "nt" else "lake")
    binary.write_bytes(b"binary")
    monkeypatch.setenv("LAKE_RUST_BINARY_DIR", str(tmp_path))
    received: list[list[str]] = []

    def fake_run(argv, check):  # noqa: ANN001, ANN202
        received.append(list(argv))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(rust_entrypoints.subprocess, "run", fake_run)

    assert cli.main(["local", "--help"]) == 0
    assert received == [[str(binary), "local", "--help"]]
    assert rust_entrypoints.rust_binary("lake") == binary


def test_missing_binary_reports_actionably(
    tmp_path, monkeypatch: MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # Only the empty directory is consulted: the repository may legitimately
    # have a built binary in src/lake/target, which this test must not exec.
    monkeypatch.setattr(rust_entrypoints, "_candidate_dirs", lambda: (tmp_path,))
    assert cli.main(["api"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "cargo build --workspace" in captured.err
    assert "src/lake" in captured.err


def test_unknown_entrypoint_name_is_rejected() -> None:
    with pytest.raises(ValueError):
        rust_entrypoints.run_entrypoint("harness")


@pytest.mark.skipif(
    rust_entrypoints.rust_binary("lake") is None,
    reason="the Rust lake binary is not built in this environment",
)
def test_module_help_comes_from_the_rust_binary() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "lake", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "api" in result.stdout
    assert "local" in result.stdout
