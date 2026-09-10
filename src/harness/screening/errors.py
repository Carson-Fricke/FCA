"""Structured fail-closed error records for the screening sandbox.

Every sandbox violation — an import, dunder access, a dangerous call, a
wall-clock or network pattern, async syntax, an output-shape breach, a
script runtime error, a worker timeout or protocol failure, or an invalid
host input — is reported as a :class:`FilterError` carrying a stable
:class:`FilterErrorKind`, a human message, and the content hash of the
offending script.  Evaluations never return partial results: a violation
anywhere aborts the whole batch.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from harness.screening._worker import (
    KIND_ASYNC_SYNTAX,
    KIND_CONCURRENCY,
    KIND_DANGEROUS_CALL,
    KIND_DUNDER_ACCESS,
    KIND_IMPORT_BLOCKED,
    KIND_INPUT_INVALID,
    KIND_NETWORKING,
    KIND_OUTPUT_SHAPE,
    KIND_SCRIPT_ERROR,
    KIND_SLEEPING,
    KIND_STRUCTURE_VIOLATION,
    KIND_SUBPROCESS,
    KIND_SYNTAX_ERROR,
    KIND_TIMEOUT,
    KIND_WALL_CLOCK,
    KIND_WORKER_FAILURE,
    MAX_MESSAGE_CHARS,
)


class FilterErrorKind(StrEnum):
    """One stable class of fail-closed screening failure."""

    SYNTAX_ERROR = KIND_SYNTAX_ERROR
    STRUCTURE_VIOLATION = KIND_STRUCTURE_VIOLATION
    IMPORT_BLOCKED = KIND_IMPORT_BLOCKED
    DUNDER_ACCESS = KIND_DUNDER_ACCESS
    DANGEROUS_CALL = KIND_DANGEROUS_CALL
    WALL_CLOCK = KIND_WALL_CLOCK
    SLEEPING = KIND_SLEEPING
    NETWORKING = KIND_NETWORKING
    SUBPROCESS = KIND_SUBPROCESS
    CONCURRENCY = KIND_CONCURRENCY
    ASYNC_SYNTAX = KIND_ASYNC_SYNTAX
    OUTPUT_SHAPE = KIND_OUTPUT_SHAPE
    SCRIPT_ERROR = KIND_SCRIPT_ERROR
    TIMEOUT = KIND_TIMEOUT
    WORKER_FAILURE = KIND_WORKER_FAILURE
    INPUT_INVALID = KIND_INPUT_INVALID


@dataclass(frozen=True, slots=True)
class FilterError:
    """One structured sandbox failure record.

    ``script_hash`` is the domain-separated SHA-256 of the evaluated
    source, so a record can always be traced back to the exact script.
    """

    kind: FilterErrorKind
    message: str
    script_hash: str

    def render(self) -> str:
        """Render the record as a single log-friendly line."""
        return f"{self.kind.value}: {self.message} (script {self.script_hash})"


class FilterEvaluationError(RuntimeError):
    """Raised when a screening evaluation fails closed.

    The :attr:`error` attribute carries the structured record; the
    exception message renders it.
    """

    def __init__(self, error: FilterError) -> None:
        super().__init__(error.render())
        self.error = error


def filter_error(
    kind: FilterErrorKind, message: str, script_hash: str
) -> FilterEvaluationError:
    """Build the raise-ready error for ``kind`` with a truncated message."""
    return FilterEvaluationError(
        FilterError(kind, message[:MAX_MESSAGE_CHARS], script_hash)
    )


def filter_error_from_record(
    record: Mapping[str, object], script_hash: str
) -> FilterError:
    """Map a worker error record onto a typed :class:`FilterError`.

    Unknown or missing kinds fail closed as
    :attr:`FilterErrorKind.WORKER_FAILURE`.
    """
    raw_kind = record.get("kind")
    try:
        kind = FilterErrorKind(str(raw_kind))
    except ValueError:
        kind = FilterErrorKind.WORKER_FAILURE
    message = record.get("message")
    if not isinstance(message, str) or not message:
        message = "worker returned an unparseable error record"
    return FilterError(kind, message, script_hash)


__all__ = [
    "FilterError",
    "FilterErrorKind",
    "FilterEvaluationError",
    "filter_error_from_record",
]
