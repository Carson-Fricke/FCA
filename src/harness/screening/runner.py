"""Subprocess runner: the public sandboxed evaluation API.

``evaluate_filter`` and ``evaluate_universe`` are the only entry points the
thesis screening layer should call.  Each evaluation validates the script
source statically, serializes one :class:`FilterContext` payload per
symbol, spawns a fresh stdlib-only worker process (one subprocess per
evaluation batch, never per symbol), speaks a strict JSON request/response
protocol over stdin/stdout, and kills the worker when the wall clock
expires.  Every failure mode — sandbox violation, script error, timeout,
protocol mismatch, invalid input — raises :class:`FilterEvaluationError`
with a structured :class:`FilterError`; partial result sets are never
returned.

Worker cold start is dominated by CPython startup and measures roughly
0.1-0.3 s per batch on a typical Windows dev box (measured continuously by
``tests/harness/screening/test_cold_start.py``), so batches — not single
symbols — are the isolation unit.

Windows honesty: the wall clock is the only hard resource limit.  The
standard library cannot cap a child's memory on Windows, so a script that
allocates in a loop can exhaust host RAM before the kill fires; see the
``harness.screening`` module documentation for the full residual-risk
statement.
"""

from __future__ import annotations

import json
import math
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from harness.screening._worker import MAX_PAYLOAD_BYTES, PROTOCOL_ID, script_hash
from harness.screening.context import FilterContext
from harness.screening.errors import (
    FilterErrorKind,
    FilterEvaluationError,
    filter_error,
    filter_error_from_record,
)
from harness.screening.validator import lint_filter_source

DEFAULT_TIMEOUT_SECONDS: Final = 60.0
STDERR_TAIL_CHARS: Final = 2000

_WORKER_PATH: Final = Path(__file__).with_name("_worker.py")


@dataclass(frozen=True, slots=True)
class FilterResult:
    """The outcome of one sandboxed filter evaluation for one symbol."""

    symbol: str
    passed: bool
    script_hash: str


@dataclass(frozen=True, slots=True)
class FilterBatchResult:
    """The complete decisions of one sandboxed evaluation batch.

    ``decisions`` maps every submitted symbol to its filter verdict in
    submission order; a violation anywhere raises instead of returning a
    partial mapping.
    """

    script_hash: str
    decisions: dict[str, bool]

    @property
    def passed_symbols(self) -> tuple[str, ...]:
        """Return the symbols whose filter evaluation passed."""
        return tuple(symbol for symbol, passed in self.decisions.items() if passed)

    @property
    def rejected_symbols(self) -> tuple[str, ...]:
        """Return the symbols whose filter evaluation failed."""
        return tuple(symbol for symbol, passed in self.decisions.items() if not passed)


def _worker_command() -> tuple[str, ...]:
    """Return the isolated spawn command for the screening worker."""
    return (sys.executable, "-I", "-S", "-B", str(_WORKER_PATH))


def _validated_timeout(timeout_seconds: float) -> float:
    if isinstance(timeout_seconds, bool) or not isinstance(
        timeout_seconds, int | float
    ):
        raise filter_error(
            FilterErrorKind.INPUT_INVALID,
            "timeout_seconds must be a positive number of seconds",
            "",
        )
    value = float(timeout_seconds)
    if not math.isfinite(value) or value <= 0:
        raise filter_error(
            FilterErrorKind.INPUT_INVALID,
            "timeout_seconds must be a positive finite number of seconds",
            "",
        )
    return value


def _validated_contexts(contexts: object) -> tuple[FilterContext, ...]:
    if isinstance(contexts, FilterContext):
        raise filter_error(
            FilterErrorKind.INPUT_INVALID,
            "contexts must be a sequence of FilterContext values; use"
            " evaluate_filter for a single symbol",
            "",
        )
    if isinstance(contexts, str) or not isinstance(contexts, Sequence):
        raise filter_error(
            FilterErrorKind.INPUT_INVALID,
            "contexts must be a sequence of FilterContext values",
            "",
        )
    submitted = tuple(contexts)
    for context in submitted:
        if not isinstance(context, FilterContext):
            raise filter_error(
                FilterErrorKind.INPUT_INVALID,
                "contexts must contain FilterContext values",
                "",
            )
    symbols = [context.symbol for context in submitted]
    if len(set(symbols)) != len(symbols):
        raise filter_error(
            FilterErrorKind.INPUT_INVALID,
            "contexts must contain each symbol at most once",
            "",
        )
    return submitted


def _payloads(contexts: tuple[FilterContext, ...]) -> list[dict[str, object]]:
    try:
        return [context.to_payload() for context in contexts]
    except (TypeError, ValueError) as error:
        raise filter_error(
            FilterErrorKind.INPUT_INVALID,
            f"context payload is not serializable: {error}",
            "",
        ) from error


def _stderr_tail(stderr: bytes | None) -> str:
    if not stderr:
        return ""
    return stderr.decode("utf-8", errors="replace")[-STDERR_TAIL_CHARS:]


def _require(condition: object, message: str, script_hash: str) -> None:
    if not condition:
        raise filter_error(FilterErrorKind.WORKER_FAILURE, message, script_hash)


def _run_worker(
    source: str,
    payloads: list[dict[str, object]],
    symbols: list[str],
    timeout_seconds: float,
) -> dict[str, bool]:
    """Spawn one worker, evaluate the batch, and enforce the wall clock."""
    source_hash = script_hash(source)
    request = {
        "protocol": PROTOCOL_ID,
        "script": source,
        "contexts": payloads,
    }
    try:
        request_bytes = json.dumps(request, sort_keys=True, allow_nan=False).encode(
            "utf-8"
        )
    except (TypeError, ValueError) as error:
        raise filter_error(
            FilterErrorKind.INPUT_INVALID,
            f"request payload is not serializable: {error}",
            source_hash,
        ) from error
    if len(request_bytes) > MAX_PAYLOAD_BYTES:
        raise filter_error(
            FilterErrorKind.INPUT_INVALID,
            f"request exceeds the {MAX_PAYLOAD_BYTES} byte payload cap",
            source_hash,
        )
    if not _WORKER_PATH.is_file():
        raise filter_error(
            FilterErrorKind.WORKER_FAILURE,
            f"screening worker entry point is missing: {_WORKER_PATH}",
            source_hash,
        )
    try:
        process = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            _worker_command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as error:
        raise filter_error(
            FilterErrorKind.WORKER_FAILURE,
            f"screening worker failed to start: {error}",
            source_hash,
        ) from error
    try:
        stdout, stderr = process.communicate(request_bytes, timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()
        raise filter_error(
            FilterErrorKind.TIMEOUT,
            f"screener worker exceeded the {timeout_seconds:g}s wall clock and"
            " was killed",
            source_hash,
        ) from None
    except BaseException:
        process.kill()
        process.communicate()
        raise
    _require(
        process.returncode == 0,
        f"screening worker exited with {process.returncode}: {_stderr_tail(stderr)}",
        source_hash,
    )
    _require(
        stdout is not None and bool(stdout.strip()),
        "screening worker produced no response",
        source_hash,
    )
    assert stdout is not None
    try:
        response = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise filter_error(
            FilterErrorKind.WORKER_FAILURE,
            f"screening worker response is not valid JSON: {error}",
            source_hash,
        ) from error
    _require(
        isinstance(response, dict), "screening response must be an object", source_hash
    )
    assert isinstance(response, dict)
    _require(
        response.get("protocol") == PROTOCOL_ID,
        "screening response protocol does not match",
        source_hash,
    )
    _require(
        response.get("script_hash") == source_hash,
        "screening response does not match the submitted script",
        source_hash,
    )
    if response.get("ok") is not True:
        record = response.get("error")
        if not isinstance(record, Mapping):
            raise filter_error(
                FilterErrorKind.WORKER_FAILURE,
                "screening worker failed without an error record",
                source_hash,
            )
        raise FilterEvaluationError(filter_error_from_record(record, source_hash))
    results = response.get("results")
    _require(
        isinstance(results, dict),
        "screening response results must be an object",
        source_hash,
    )
    assert isinstance(results, dict)
    for symbol, passed in results.items():
        _require(
            isinstance(symbol, str) and isinstance(passed, bool),
            "screening results must map symbol strings to booleans",
            source_hash,
        )
    _require(
        set(results) == set(symbols),
        "screening response does not decide exactly the submitted symbols",
        source_hash,
    )
    return {symbol: bool(results[symbol]) for symbol in symbols}


def evaluate_universe(
    script_source: str,
    contexts: Sequence[FilterContext],
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> FilterBatchResult:
    """Evaluate a screener script over a universe of contexts.

    One fresh worker process evaluates the whole batch; the script source
    is validated before the spawn, the context payloads are serialized,
    and the worker is killed at the wall clock.  Any violation raises
    :class:`FilterEvaluationError`; results are never partial.
    """
    timeout = _validated_timeout(timeout_seconds)
    if not isinstance(script_source, str):
        raise filter_error(
            FilterErrorKind.INPUT_INVALID,
            "script_source must be a string",
            "",
        )
    validated = _validated_contexts(contexts)
    report = lint_filter_source(script_source)
    report.raise_for_status()
    payloads = _payloads(validated)
    symbols = [context.symbol for context in validated]
    decisions = _run_worker(script_source, payloads, symbols, timeout)
    return FilterBatchResult(report.source_hash, decisions)


def evaluate_filter(
    script_source: str,
    context: FilterContext,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
) -> FilterResult:
    """Evaluate a screener script for exactly one symbol."""
    if not isinstance(context, FilterContext):
        raise filter_error(
            FilterErrorKind.INPUT_INVALID,
            "context must be a FilterContext",
            "",
        )
    batch = evaluate_universe(
        script_source, (context,), timeout_seconds=timeout_seconds
    )
    return FilterResult(
        context.symbol,
        batch.decisions[context.symbol],
        batch.script_hash,
    )
