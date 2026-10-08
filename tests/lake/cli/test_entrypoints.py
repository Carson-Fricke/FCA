"""The lake console script is a thin shim over the Rust ``lake`` binary."""

import os
from types import SimpleNamespace

import pytest
from pytest import MonkeyPatch

from lake import cli
from lake.cli import rust_entrypoints


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
