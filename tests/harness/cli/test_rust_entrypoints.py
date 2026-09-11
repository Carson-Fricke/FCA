"""Tests for the environment-gated Rust entrypoint flip."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from harness.cli import backtest_job, rust_entrypoints, trader


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


def test_deferred_entrypoints_stay_on_python() -> None:
    """The engine entrypoints are wired but gated until parity is proven."""
    assert rust_entrypoints.PORTED_ENTRYPOINTS == frozenset({"api", "mcp"})
    assert set(rust_entrypoints.DEFERRED_ENTRYPOINTS) == {"backtest-job", "trader"}
    for name in ("harness", "backtest-job", "trader"):
        assert not rust_entrypoints.rust_entrypoint_available(name)
        assert rust_entrypoints.exec_rust_entrypoint(name, []) is None


def test_facade_group_finds_the_first_positional_token() -> None:
    assert rust_entrypoints.facade_group([]) is None
    assert rust_entrypoints.facade_group(["--help"]) is None
    assert rust_entrypoints.facade_group(["api", "--config", "x"]) == "api"
    assert (
        rust_entrypoints.facade_group(["backtest-job", "--config", "x"])
        == "backtest-job"
    )


def test_resolved_argv_reads_the_process_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "argv", ["harness", "api", "--config", "x"])
    assert rust_entrypoints.resolved_argv(None) == ["api", "--config", "x"]
    assert rust_entrypoints.resolved_argv(["--help"]) == ["--help"]


def test_facade_console_script_flips_through_the_process_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from harness.cli import application

    monkeypatch.setenv(rust_entrypoints.RUST_ENTRYPOINTS_FLAG, "1")
    monkeypatch.setattr(sys, "argv", ["harness", "api", "--config", "api.yaml"])
    calls: list[list[str]] = []

    def fake(argv: Sequence[str]) -> int:
        calls.append(list(argv))
        return 13

    monkeypatch.setattr(rust_entrypoints, "exec_rust_facade", fake)

    assert application.main() == 13
    assert calls == [["api", "--config", "api.yaml"]]


def test_facade_dispatches_only_ported_groups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(rust_entrypoints.RUST_ENTRYPOINTS_FLAG, "1")
    calls: list[list[str]] = []

    def fake(name: str, argv: Sequence[str]) -> int:
        calls.append([name, *argv])
        return 5

    monkeypatch.setattr(rust_entrypoints, "_exec", fake)

    assert rust_entrypoints.exec_rust_facade(["api", "--config", "api.yaml"]) == 5
    assert rust_entrypoints.exec_rust_facade(["mcp"]) == 5
    assert calls == [
        ["harness", "api", "--config", "api.yaml"],
        ["harness", "mcp"],
    ]


def test_facade_keeps_unported_groups_on_python(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(rust_entrypoints.RUST_ENTRYPOINTS_FLAG, "1")

    def fail(name: str, argv: Sequence[str]) -> int:
        raise AssertionError("an unported facade group must not exec")

    monkeypatch.setattr(rust_entrypoints, "_exec", fail)

    assert rust_entrypoints.exec_rust_facade([]) is None
    assert rust_entrypoints.exec_rust_facade(["--help"]) is None
    assert rust_entrypoints.exec_rust_facade(["backtest-job"]) is None
    assert rust_entrypoints.exec_rust_facade(["trader", "--config", "t.yaml"]) is None


def test_facade_falls_back_when_the_flip_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(rust_entrypoints.RUST_ENTRYPOINTS_FLAG, raising=False)
    assert rust_entrypoints.exec_rust_facade(["api"]) is None


def test_facade_main_dispatches_through_the_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from harness.cli import application

    calls: list[list[str]] = []

    def fake(argv: Sequence[str]) -> int:
        calls.append(list(argv))
        return 11

    monkeypatch.setattr(rust_entrypoints, "exec_rust_facade", fake)

    assert application.main(["api", "--config", "api.yaml"]) == 11
    assert calls == [["api", "--config", "api.yaml"]]


@pytest.mark.parametrize(
    ("entrypoint", "prog_name"),
    [(backtest_job.main, "backtest-job"), (trader.main, "trader")],
)
def test_engine_entrypoints_stay_on_python_by_default(
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: Callable[[Sequence[str] | None], int],
    prog_name: str,
) -> None:
    """The engine console scripts consult the switch and keep Python."""
    calls: list[str] = []

    def no_port(name: str, argv: Sequence[str]) -> None:
        calls.append(name)
        return None

    monkeypatch.setattr(rust_entrypoints, "exec_rust_entrypoint", no_port)

    assert entrypoint(["--help"]) == 0
    assert calls == [prog_name]
