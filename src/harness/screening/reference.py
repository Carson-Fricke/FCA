"""In-process reference executor for sandbox parity checks.

This module executes the exact same script contract as the subprocess
worker but without process isolation: validate, compile, and exec the
``screen(ctx)`` function against restricted builtins and the same
serialized payload the worker receives.  It exists so parity tests can
prove the sandbox protocol changes nothing — same script plus same data
must produce identical decisions in both execution models.

It is a comparison and migration-verification tool, not a production
screening path: production evaluation must go through
:func:`harness.screening.evaluate_filter` / ``evaluate_universe`` so the
process boundary and the wall-clock kill stay in the loop.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

from harness.screening._worker import _ScreenFailure, execute_program, script_hash
from harness.screening.context import FilterContext
from harness.screening.errors import (
    FilterErrorKind,
    FilterEvaluationError,
    filter_error,
)
from harness.screening.runner import (
    FilterBatchResult,
    _payloads,
    _validated_contexts,
)

__all__ = [
    "evaluate_filter_inprocess",
    "evaluate_universe_inprocess",
]


def _round_trip_payloads(payloads: list[dict[str, object]]) -> list[dict[str, object]]:
    """Round-trip every payload through JSON exactly like the wire protocol.

    The worker only ever sees data that survived a JSON round trip, so the
    reference executor observes the same floats, strings, and ``None``
    values the sandbox observes.
    """
    return [
        json.loads(json.dumps(payload, sort_keys=True, allow_nan=False))
        for payload in payloads
    ]


def _typed_failure(
    script_source: str, kind: str, message: str
) -> FilterEvaluationError:
    """Map a program failure kind onto the typed error, failing closed."""
    try:
        resolved = FilterErrorKind(kind)
    except ValueError:
        resolved = FilterErrorKind.WORKER_FAILURE
    return filter_error(resolved, message, script_hash(script_source))


def evaluate_universe_inprocess(
    script_source: str,
    contexts: Sequence[FilterContext],
) -> FilterBatchResult:
    """Evaluate a screener script in-process against restricted builtins.

    Mirrors the worker's validation and output contract without a
    subprocess: any violation raises :class:`FilterEvaluationError` and
    results are never partial.  There is no timeout — a runaway script can
    only be stopped by the caller, which is precisely why the sandboxed
    runner exists.
    """
    if not isinstance(script_source, str):
        raise filter_error(
            FilterErrorKind.INPUT_INVALID,
            "script_source must be a string",
            "",
        )
    validated = _validated_contexts(contexts)
    payloads = _round_trip_payloads(_payloads(validated))
    symbols = [context.symbol for context in validated]
    try:
        decisions = execute_program(script_source, payloads)
    except _ScreenFailure as failure:
        raise _typed_failure(script_source, failure.kind, failure.message) from failure
    return FilterBatchResult(
        script_hash(script_source),
        {symbol: decisions[symbol] for symbol in symbols},
    )


def evaluate_filter_inprocess(
    script_source: str,
    context: FilterContext,
) -> FilterBatchResult:
    """Evaluate a screener script in-process for exactly one symbol."""
    return evaluate_universe_inprocess(script_source, (context,))
