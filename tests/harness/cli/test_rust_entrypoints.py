"""Tests for the environment-gated Rust entrypoint flip."""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

import pytest

from harness.cli import rust_entrypoints


def test_flag_is_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(rust_entrypoints.RUST_ENTRYPOINTS_FLAG, raising=False)
    assert rust_entrypoints.rust_entrypoints_enabled() is False
    assert rust_entrypoints.exec_rust_entrypoint("api", ["--help"]) is None


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_flag_truthy_spellings(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
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
    monkeypatch.setattr(rust_entrypoints, "_candidate_dirs", lambda: (tmp_path,))
    assert rust_entrypoints.rust_binary("api") is None
    assert rust_entrypoints.exec_rust_entrypoint("api", []) is None


def test_available_binary_is_detected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(rust_entrypoints.RUST_ENTRYPOINTS_FLAG, "1")
    monkeypatch.setenv(rust_entrypoints.RUST_BINARY_DIR_VARIABLE, str(tmp_path))
    monkeypatch.setattr(rust_entrypoints, "_candidate_dirs", lambda: (tmp_path,))
    name = "api.exe" if os.name == "nt" else "api"
    (tmp_path / name).write_text("", encoding="utf-8")
    assert rust_entrypoints.rust_binary("api") == tmp_path / name
    assert rust_entrypoints.rust_entrypoint_available("api") is True


def test_api_main_dispatches_through_the_port(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ported `api` console script hands control to the Rust dispatcher."""
    from harness.cli import api

    calls: list[tuple[str, list[str]]] = []

    def fake(name: str, argv: Sequence[str] | None) -> int:
        calls.append((name, list(argv or [])))
        return 7

    monkeypatch.setattr(rust_entrypoints, "exec_rust_entrypoint", fake)

    assert api.main(["--config", "api.yaml"]) == 7
    assert calls == [("api", ["--config", "api.yaml"])]


def test_mcp_main_dispatches_through_the_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ported `mcp` console script hands control to the Rust dispatcher."""
    from harness.mcp import __main__ as mcp_main

    calls: list[tuple[str, list[str]]] = []

    def fake(name: str, argv: Sequence[str] | None) -> int:
        calls.append((name, list(argv or [])))
        return 9

    monkeypatch.setattr(rust_entrypoints, "exec_rust_entrypoint", fake)

    assert mcp_main.main(["--mode", "remote"]) == 9
    assert calls == [("mcp", ["--mode", "remote"])]


def test_api_main_falls_back_when_the_port_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `None` dispatch leaves `api` on the Python Typer command."""
    from harness.cli import api

    calls: list[str] = []

    def no_port(name: str, argv: Sequence[str] | None) -> None:
        calls.append(name)
        return None

    monkeypatch.setattr(rust_entrypoints, "exec_rust_entrypoint", no_port)

    assert api.main(["--help"]) == 0
    assert calls == ["api"]


def test_facade_and_engine_entrypoints_are_never_flipped() -> None:
    """Only the complete interface processes are flipped."""
    assert rust_entrypoints.PORTED_ENTRYPOINTS == frozenset({"api", "mcp"})
    for name in ("harness", "backtest-job", "trader"):
        assert not rust_entrypoints.rust_entrypoint_available(name)
        assert rust_entrypoints.exec_rust_entrypoint(name, []) is None
