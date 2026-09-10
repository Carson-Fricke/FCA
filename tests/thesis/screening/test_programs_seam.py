"""Custom-program executor seam tests: the in-process boundary stays the
default, and an alternative executor (the T229 sandbox seam) plugs in without
touching the compilation boundary."""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from thesis.screening import (
    ScreenContext,
    ScreenerError,
    compile_program,
    run_program,
)
from thesis.screening.programs import InProcessProgramExecutor

PROGRAM = (
    "def screen(ctx):\n    return [{'symbol': s} for s in ctx.available_symbols()]\n"
)


def _context() -> ScreenContext:
    return ScreenContext(
        symbols=("AAA", "BBB"),
        universe=lambda symbol: (),
        market_data=lambda symbol, as_of: (),
        facts=lambda symbol: {"security_type": "stock"},
        indicators=lambda symbol: {"ind:rsi(period=14)": "42.0"},
    )


def test_in_process_executor_is_the_default() -> None:
    program = compile_program(PROGRAM)

    rows = run_program(program, _context())

    assert [row["symbol"] for row in rows] == ["AAA", "BBB"]


def test_explicit_in_process_executor_matches_the_default() -> None:
    program = compile_program(PROGRAM)

    rows = run_program(program, _context(), executor=InProcessProgramExecutor())

    assert [row["symbol"] for row in rows] == ["AAA", "BBB"]


def test_custom_executor_replaces_the_in_process_boundary() -> None:
    class RecordingExecutor:
        def __init__(self) -> None:
            self.seen_context: ScreenContext | None = None

        def execute(
            self, program: object, ctx: ScreenContext
        ) -> tuple[Mapping[str, object], ...]:
            self.seen_context = ctx
            return ({"symbol": "SANDBOX", "count": 1},)

    executor = RecordingExecutor()

    rows = run_program(compile_program(PROGRAM), _context(), executor=executor)

    assert [row["symbol"] for row in rows] == ["SANDBOX"]
    assert executor.seen_context is not None
    assert executor.seen_context.fact_sheet("AAA")["security_type"] == "stock"
    assert executor.seen_context.indicators_for("AAA")["ind:rsi(period=14)"] == "42.0"


def test_executor_failures_fail_closed_with_the_script_error() -> None:
    class ExplodingExecutor:
        def execute(
            self, program: object, ctx: ScreenContext
        ) -> tuple[Mapping[str, object], ...]:
            raise ValueError("sandbox is gone")

    program = compile_program(PROGRAM)

    with pytest.raises(ScreenerError, match="custom screener failed"):
        run_program(program, _context(), executor=ExplodingExecutor())


def test_fact_and_indicator_surfaces_default_to_empty() -> None:
    context = ScreenContext(
        symbols=("AAA",),
        universe=lambda symbol: (),
        market_data=lambda symbol, as_of: (),
    )

    assert context.fact_sheet("AAA") == {}
    assert context.indicators_for("AAA") == {}
