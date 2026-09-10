"""Programmatic custom screeners under a fail-closed boundary.

A custom screener is user-authored Python source defining exactly one
``def screen(ctx) -> list[dict]`` function. The compilation boundary rejects
everything else:

- module-level statements other than allowlisted imports and the ``screen``
  function definition;
- imports outside the allowlist (``math``, ``statistics``);
- import aliases and dunder attribute access of any kind;
- more than one function or any other top-level statement.

The compiled program executes with restricted globals (a minimal
``__builtins__`` set plus the allowlisted modules), so there is no network,
no file I/O, and no wall clock in scope — the only data surface is the
``ctx`` ports the engine injects. Runtime failures inside the script fail
closed as :class:`ScreenerError` carrying the script error, never partially.

Execution seam
--------------

The in-process ``exec`` boundary is the T219 execution model and remains the
default, but it is isolated behind :class:`InProcessProgramExecutor` and the
:class:`ProgramExecutor` protocol (``SANDBOX_MODULE`` below).  The
process-isolated sandbox (T229, ``harness.screening``) replaces exactly that
seam: callers pass an alternative executor to :func:`run_program`, and the
compilation boundary, fact-sheet surface, and fail-closed error contract
stay unchanged.
"""

from __future__ import annotations

import ast
import math
import statistics
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Protocol, cast

from thesis.screening.engine import ScreenerError

ALLOWED_MODULES: Final[tuple[str, ...]] = ("math", "statistics")
SCREEN_FUNCTION: Final[str] = "screen"
SANDBOX_MODULE: Final = "harness.screening"

_SAFE_BUILTINS: Final[dict[str, object]] = {
    "abs": abs,
    "bool": bool,
    "dict": dict,
    "enumerate": enumerate,
    "float": float,
    "frozenset": frozenset,
    "int": int,
    "len": len,
    "list": list,
    "map": map,
    "max": max,
    "min": min,
    "range": range,
    "round": round,
    "set": set,
    "sorted": sorted,
    "str": str,
    "sum": sum,
    "tuple": tuple,
    "zip": zip,
}


class ProgramViolationError(ScreenerError):
    """A custom screener script violates the compilation boundary."""


@dataclass(frozen=True, slots=True)
class ScreenContext:
    """The only data surface a custom screener can reach.

    ``market_data`` returns daily bars as mappings with the enriched OHLCV
    vocabulary (``bar_date``, ``open``, ``high``, ``low``, ``close``,
    ``volume``, ``dollar_volume``), ``facts`` exposes the symbol's engine
    fact sheet (market metrics plus classification and fundamentals facts),
    and ``indicators`` returns the named indicator values the engine
    precomputed through the harness registry under their canonical
    ``ind:<name>(<params>)`` keys — scripts never hand-roll indicator math.
    """

    symbols: tuple[str, ...]
    universe: Callable[[str], Sequence[Mapping[str, object]]]
    market_data: Callable[[str, str], Sequence[Mapping[str, object]]]
    facts: Callable[[str], Mapping[str, object]] | None = None
    indicators: Callable[[str], Mapping[str, object]] | None = None

    def available_symbols(self) -> tuple[str, ...]:
        """Return the as-of universe symbols."""
        return tuple(self.symbols)

    def fact_sheet(self, symbol: str) -> Mapping[str, object]:
        """Return the symbol's fact sheet (empty when unavailable)."""
        if self.facts is None:
            return {}
        return self.facts(symbol)

    def indicators_for(self, symbol: str) -> Mapping[str, object]:
        """Return the symbol's precomputed indicator values by metric key."""
        if self.indicators is None:
            return {}
        return self.indicators(symbol)


class ProgramExecutor(Protocol):
    """The single execution seam a process-isolated sandbox replaces."""

    def execute(
        self, program: Program, ctx: ScreenContext
    ) -> tuple[Mapping[str, object], ...]:
        """Run ``screen(ctx)`` and return its rows, failing closed."""
        ...


class InProcessProgramExecutor:
    """The T219 in-process executor over restricted globals."""

    def execute(
        self, program: Program, ctx: ScreenContext
    ) -> tuple[Mapping[str, object], ...]:
        return program.run(ctx)


def _restricted_import(name, *args, **kwargs):
    """Import boundary: only the allowlisted modules resolve."""
    if name not in ALLOWED_MODULES:
        raise ImportError(f"import of {name!r} is blocked in custom screeners")
    return {"math": math, "statistics": statistics}[name]


@dataclass(frozen=True, slots=True)
class Program:
    """A compiled, boundary-checked custom screener."""

    source: str

    def run(self, ctx: ScreenContext) -> tuple[Mapping[str, object], ...]:
        """Execute ``screen(ctx)`` and return its result rows."""
        tree = ast.parse(self.source, mode="exec")
        _extract_screen(tree)
        code = compile(tree, filename="<custom-screener>", mode="exec")
        builtins = dict(_SAFE_BUILTINS)
        builtins["__import__"] = _restricted_import
        namespace: dict[str, object] = {
            "__builtins__": builtins,
            "math": math,
            "statistics": statistics,
        }
        try:
            exec(code, namespace)  # noqa: S102 - restricted-globals boundary
            screen = namespace[SCREEN_FUNCTION]
            if not callable(screen):
                raise ScreenerError(f"{SCREEN_FUNCTION} must be a callable function")
            screen_callable = cast("Callable[[ScreenContext], object]", screen)
            result = screen_callable(ctx)
        except ScreenerError:
            raise
        except Exception as error:  # noqa: BLE001 - script errors fail closed
            raise ScreenerError(f"custom screener failed: {error}") from error
        if not isinstance(result, list) or not all(
            isinstance(row, dict) for row in result
        ):
            raise ScreenerError("custom screener must return a list of dicts")
        return tuple(result)


def compile_program(source: str) -> Program:
    """Validate and compile a custom screener script."""
    if not isinstance(source, str) or not source.strip():
        raise ProgramViolationError("screener source must be a non-empty string")
    try:
        tree = ast.parse(source, mode="exec")
    except SyntaxError as error:
        raise ProgramViolationError(
            f"screener source does not parse: {error}"
        ) from None
    _extract_screen(tree)
    return Program(source=source)


def run_program(
    program: Program,
    ctx: ScreenContext,
    *,
    executor: ProgramExecutor | None = None,
) -> tuple[Mapping[str, object], ...]:
    """Execute a compiled program against ``ctx``.

    Without an explicit executor the T219 in-process boundary runs the
    program; T229's process-isolated sandbox (``harness.screening``) plugs
    in as another :class:`ProgramExecutor` without touching this module's
    boundary checks.
    """
    if not isinstance(program, Program):
        raise ScreenerError("program must be a compiled Program")
    executor = executor or InProcessProgramExecutor()
    try:
        return executor.execute(program, ctx)
    except ScreenerError:
        raise
    except Exception as error:  # noqa: BLE001 - execution failures fail closed
        raise ScreenerError(f"custom screener failed: {error}") from error


def _extract_screen(tree: ast.Module) -> ast.FunctionDef:
    """Validate the module structure and return the screen function."""
    function: ast.FunctionDef | None = None
    for statement in tree.body:
        if isinstance(statement, ast.Import):
            for alias in statement.names:
                if alias.name not in ALLOWED_MODULES or alias.asname is not None:
                    raise ProgramViolationError(
                        f"import of {alias.name!r} is outside the allowlist"
                    )
            continue
        if isinstance(statement, ast.ImportFrom):
            if statement.module not in ALLOWED_MODULES or statement.level:
                raise ProgramViolationError(
                    f"import from {statement.module!r} is outside the allowlist"
                )
            for alias in statement.names:
                if alias.asname is not None:
                    raise ProgramViolationError(
                        "import aliases are not permitted in custom screeners"
                    )
            continue
        if isinstance(statement, ast.FunctionDef):
            if function is not None or statement.name != SCREEN_FUNCTION:
                raise ProgramViolationError(
                    "the module must define exactly one screen(ctx) function"
                )
            function = statement
            continue
        raise ProgramViolationError(
            f"top-level {type(statement).__name__} is not permitted in a "
            "custom screener"
        )
    if function is None:
        raise ProgramViolationError(
            f"the module must define a {SCREEN_FUNCTION}(ctx) function"
        )
    for node in ast.walk(function):
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise ProgramViolationError(
                f"dunder attribute access is not permitted (line {node.lineno})"
            )
    return function
