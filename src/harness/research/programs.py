"""Restricted-namespace execution for workspace research source.

The source contract is deliberately smaller than the strategy contract. A
model defines ``fit(context) -> dict[str, number]`` and
``observe(fit, context) -> dict[str, number]``; a reusable workspace indicator
defines ``calculate(context, parameters) -> JSON-shaped numbers``.
Only mathematical imports are available and the namespace exposes no file,
network, subprocess, clock, reflection, or dynamic-code builtins.
"""

from __future__ import annotations

import ast
import builtins
import math
import numbers
import statistics
from collections.abc import Callable, Mapping, Sequence
from typing import Final, cast

import numpy

_ALLOWED_MODULES: Final[Mapping[str, object]] = {
    "math": math,
    "numpy": numpy,
    "statistics": statistics,
}
_SAFE_BUILTINS: Final[dict[str, object]] = {
    name: getattr(builtins, name)
    for name in (
        "abs",
        "all",
        "any",
        "bool",
        "dict",
        "enumerate",
        "filter",
        "float",
        "frozenset",
        "int",
        "isinstance",
        "len",
        "list",
        "map",
        "max",
        "min",
        "range",
        "round",
        "set",
        "sorted",
        "str",
        "sum",
        "tuple",
        "zip",
    )
}
_DENIED_CALLS: Final = frozenset(
    {
        "breakpoint",
        "compile",
        "eval",
        "exec",
        "getattr",
        "globals",
        "input",
        "locals",
        "open",
        "setattr",
        "vars",
    }
)
_DENIED_ATTRIBUTES: Final = frozenset(
    {
        "dump",
        "dumps",
        "fromfile",
        "genfromtxt",
        "load",
        "loadtxt",
        "memmap",
        "save",
        "savetxt",
        "tofile",
    }
)


class ResearchProgramError(ValueError):
    """Workspace research source or its result violates the contract."""


def _restricted_import(
    name: str,
    globals: object = None,
    locals: object = None,
    fromlist: object = (),
    level: int = 0,
) -> object:
    del globals, locals, fromlist
    if level != 0 or name not in _ALLOWED_MODULES:
        raise ImportError(f"research source cannot import {name!r}")
    return _ALLOWED_MODULES[name]


def _namespace(source: str, entrypoint: str) -> Mapping[str, object]:
    if not isinstance(source, str) or not source.strip():
        raise ResearchProgramError("research source must be non-empty")
    if not isinstance(entrypoint, str) or not entrypoint.isidentifier():
        raise ResearchProgramError("research entrypoint must be a Python identifier")
    try:
        tree = ast.parse(source, filename="<workspace-research>", mode="exec")
    except SyntaxError as error:
        raise ResearchProgramError(f"research source does not parse: {error}") from None
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.Await, ast.Lambda)):
            raise ResearchProgramError("research source must be synchronous and named")
        if isinstance(node, ast.Import):
            if any(alias.name not in _ALLOWED_MODULES for alias in node.names):
                raise ResearchProgramError(
                    "research imports are limited to math/statistics/numpy"
                )
        if isinstance(node, ast.ImportFrom):
            raise ResearchProgramError(
                "research source must import whole allowlisted modules"
            )
        if isinstance(node, ast.Name) and node.id.startswith("__"):
            raise ResearchProgramError(
                "dunder access is not permitted in research source"
            )
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("__"):
                raise ResearchProgramError(
                    "dunder access is not permitted in research source"
                )
            if node.attr in _DENIED_ATTRIBUTES:
                raise ResearchProgramError(
                    f"research source cannot call data-I/O attribute {node.attr!r}"
                )
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in _DENIED_CALLS:
                raise ResearchProgramError(
                    f"research source cannot call {node.func.id!r}"
                )
    safe_builtins = dict(_SAFE_BUILTINS)
    safe_builtins["__import__"] = _restricted_import
    namespace: dict[str, object] = {
        "__builtins__": safe_builtins,
        **_ALLOWED_MODULES,
    }
    try:
        exec(  # noqa: S102 - constrained namespace defined above
            compile(tree, filename="<workspace-research>", mode="exec"),
            namespace,
        )
    except Exception as error:  # noqa: BLE001 - workspace errors fail closed
        raise ResearchProgramError(
            f"research source failed to load: {error}"
        ) from error
    function = namespace.get(entrypoint)
    if not callable(function):
        raise ResearchProgramError(
            f"research source must define callable {entrypoint!r}"
        )
    return namespace


def _finite_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, numbers.Real):
        raise ResearchProgramError(f"{label} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise ResearchProgramError(f"{label} must be finite")
    return number


def fit_model(
    source: str, entrypoint: str, context: Mapping[str, object]
) -> dict[str, float]:
    """Fit a model and return its finite numeric parameters/calibration."""
    namespace = _namespace(source, entrypoint)
    function = cast("Callable[[dict[str, object]], object]", namespace[entrypoint])
    try:
        result = function(dict(context))
    except Exception as error:  # noqa: BLE001 - workspace errors fail closed
        raise ResearchProgramError(f"research model fit failed: {error}") from error
    return _metric_mapping(result, "research model fit")


def observe_model(
    source: str,
    entrypoint: str,
    fit: Mapping[str, float],
    context: Mapping[str, object],
) -> dict[str, float]:
    """Apply frozen fit parameters and return current finite observations."""
    namespace = _namespace(source, entrypoint)
    function = cast(
        "Callable[[dict[str, float], dict[str, object]], object]",
        namespace[entrypoint],
    )
    try:
        result = function(dict(fit), dict(context))
    except Exception as error:  # noqa: BLE001 - workspace errors fail closed
        raise ResearchProgramError(f"research model observe failed: {error}") from error
    return _metric_mapping(result, "research model observations")


def _metric_mapping(result: object, label: str) -> dict[str, float]:
    if not isinstance(result, Mapping) or not result:
        raise ResearchProgramError(f"{label} must return a non-empty metric mapping")
    metrics: dict[str, float] = {}
    for name, value in result.items():
        if not isinstance(name, str) or not name.isidentifier():
            raise ResearchProgramError(f"{label} names must be identifiers")
        metrics[name] = _finite_number(value, f"{label} value {name!r}")
    return metrics


def evaluate_indicator(
    source: str,
    entrypoint: str,
    context: Mapping[str, object],
    parameters: Mapping[str, object],
) -> object:
    """Execute one workspace indicator and validate its numeric result tree."""
    namespace = _namespace(source, entrypoint)
    function = cast(
        "Callable[[dict[str, object], dict[str, object]], object]",
        namespace[entrypoint],
    )
    try:
        result = function(dict(context), dict(parameters))
    except Exception as error:  # noqa: BLE001 - workspace errors fail closed
        raise ResearchProgramError(f"workspace indicator failed: {error}") from error
    return _numeric_tree(result, "workspace indicator output")


def _numeric_tree(value: object, label: str) -> object:
    if isinstance(value, numbers.Real) and not isinstance(value, bool):
        return _finite_number(value, label)
    if isinstance(value, Mapping):
        return {
            str(key): _numeric_tree(item, f"{label}.{key}")
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            _numeric_tree(item, f"{label}[{index}]") for index, item in enumerate(value)
        ]
    if value is None:
        return None
    raise ResearchProgramError(f"{label} must contain only finite numbers or nulls")


__all__ = [
    "ResearchProgramError",
    "evaluate_indicator",
    "fit_model",
    "observe_model",
]
