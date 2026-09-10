"""Process-isolated execution sandbox for custom screener filters.

Custom Python screener filters run in a fresh, stdlib-only worker
subprocess per evaluation batch: the worker receives the script source and
one serialized data payload per symbol over stdin, returns structured
``symbol -> bool`` decisions over stdout, and is killed by the host when
the wall clock expires.  Scripts never import ``harness``, never touch the
host's objects, and can only use the preloaded ``math`` and ``statistics``
modules.

Script contract
---------------

A screener script defines exactly one ``def screen(ctx)`` function.
``ctx`` is a plain dict with:

- ``facts``: the security fact sheet — ``symbol``, ``security_type``,
  ``exchange``, ``sic_industry``, ``as_of``, and ``fundamentals`` (the
  fundamental values as of the as-of instant);
- ``bars``: the symbol's OHLCV window as a list of dicts with ``date``,
  ``open``, ``high``, ``low``, ``close``, ``volume``;
- ``indicators``: precomputed named series as lists of floats (or
  ``None`` before warm-up), aligned with ``bars``.  The host computes
  them through ``harness.indicators.create_indicator``; scripts never
  import harness and never hand-roll indicator math beyond simple
  arithmetic on the provided series.

The return value must be a bool (the current symbol's decision) or a dict
of ``symbol -> bool``; anything else is an output-shape violation.

Fail-closed boundary.  Every violation — a disallowed import, dunder
access, a dangerous builtin call, a wall-clock/network/subprocess/
concurrency pattern, async syntax, disallowed module structure, an
output-shape breach, a script runtime error, a worker timeout or protocol
failure, or invalid host input — raises :class:`FilterEvaluationError`
carrying a :class:`FilterError` (stable kind, message, script hash).
Results are never partial: one violation aborts the whole batch.

Determinism.  The same script and the same data produce identical results
inside and outside the sandbox: the worker validates and executes with the
same restricted globals as :mod:`harness.screening.reference`, the
in-process reference executor included for parity verification.

Public API::

    evaluate_filter(script_source, context) -> FilterResult
    evaluate_universe(script_source, contexts) -> FilterBatchResult
    evaluate_filter_inprocess(script_source, context) -> FilterBatchResult
    evaluate_universe_inprocess(script_source, contexts) -> FilterBatchResult
    lint_filter_source(source) -> FilterLintReport

Windows honesty.  The standard library cannot cap a child process's
memory on Windows, and AST-level blocking of allocation-heavy patterns is
NOT reliable, so memory exhaustion is not prevented.  The enforced limits
are the import lock, the restricted builtin and dunder surface, the 64 MiB
request cap, and the wall-clock kill (default 60 s, configurable).  A
script that allocates in a loop can exhaust host RAM before the kill
fires; screen only trusted authors' scripts, or run on a
resource-isolated host.
"""

from __future__ import annotations

from harness.screening.context import (
    BarRecord,
    FilterContext,
    IndicatorSeriesRequest,
    SecurityFacts,
    compute_indicator_series,
)
from harness.screening.errors import (
    FilterError,
    FilterErrorKind,
    FilterEvaluationError,
    filter_error_from_record,
)
from harness.screening.reference import (
    evaluate_filter_inprocess,
    evaluate_universe_inprocess,
)
from harness.screening.runner import (
    DEFAULT_TIMEOUT_SECONDS,
    FilterBatchResult,
    FilterResult,
    evaluate_filter,
    evaluate_universe,
)
from harness.screening.validator import (
    FilterLintReport,
    FilterViolation,
    assert_filter_source,
    lint_filter_source,
)

__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "BarRecord",
    "FilterBatchResult",
    "FilterContext",
    "FilterError",
    "FilterErrorKind",
    "FilterEvaluationError",
    "FilterLintReport",
    "FilterResult",
    "FilterViolation",
    "IndicatorSeriesRequest",
    "SecurityFacts",
    "assert_filter_source",
    "compute_indicator_series",
    "evaluate_filter",
    "evaluate_filter_inprocess",
    "evaluate_universe",
    "evaluate_universe_inprocess",
    "filter_error_from_record",
    "lint_filter_source",
]
