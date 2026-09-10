"""Tests for the environment-gated Rust entrypoint flip."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from harness.cli import rust_entrypoints


def test_flag_is_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(rust_entrypoints.RUST_ENTRYPOINTS_FLAG, raising=False)
    assert rust_entrypoints.rust_entrypoints_enabled() is False
    assert rust_entrypoints.exec_rust_entrypoint("api", ["--help"]) is None


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_flag_truthy_spellings(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(rust_entrypoints.RUST_ENTRYPOINTS_FLAG, value)
    assert rust_entrypoints.rust_entrypoints_enabled() is True


def test_unported_entrypoints_are_never_flipped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(rust_entrypoints.RUST_ENTRYPOINTS_FLAG, "1")
    monkeypatch.setenv(rust_entrypoints.RUST_BINARY_DIR_VARIABLE, str(tmp_path))
    (tmp_path / "backtest-job").write_text("", encoding="utf-8")
    assert rust_entrypoints.exec_rust_entrypoint("backtest-job", []) is None
    assert "backtest-job" not in rust_entrypoints.PORTED_ENTRYPOINTS


def test_missing_binary_falls_back_to_python(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(rust_entrypoints.RUST_ENTRYPOINTS_FLAG, "1")
    monkeypatch.setenv(rust_entrypoints.RUST_BINARY_DIR_VARIABLE, str(tmp_path))
    assert rust_entrypoints.rust_binary("api") is None
    assert rust_entrypoints.exec_rust_entrypoint("api", []) is None


def test_available_binary_is_detected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(rust_entrypoints.RUST_ENTRYPOINTS_FLAG, "1")
    monkeypatch.setenv(rust_entrypoints.RUST_BINARY_DIR_VARIABLE, str(tmp_path))
    name = "api.exe" if os.name == "nt" else "api"
    (tmp_path / name).write_text("", encoding="utf-8")
    assert rust_entrypoints.rust_binary("api") == tmp_path / name
    assert rust_entrypoints.rust_entrypoint_available("api") is True
