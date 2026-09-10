"""Typed AST lint for custom screener filter sources.

The validator mirrors the strictness of ``harness.strategy.lint``: it is
static, side-effect free, and reports every construct that escapes the
screening sandbox — imports outside the ``math``/``statistics`` allowlist,
dunder attribute or name access, dangerous builtin calls, wall-clock and
networking attribute chains, subprocess and concurrency patterns, async
syntax, and disallowed module structure.

The checking logic lives in the stdlib-only worker
(``harness.screening._worker``) so the subprocess enforces exactly what
this host-side lint reports; this module adds typed diagnostics and the
raise-ready guard used by the public API.
"""

from __future__ import annotations

from dataclasses import dataclass

from harness.screening._worker import script_hash, validate_source
from harness.screening.errors import (
    FilterError,
    FilterErrorKind,
    FilterEvaluationError,
    filter_error_from_record,
)

__all__ = [
    "FilterLintReport",
    "FilterViolation",
    "assert_filter_source",
    "lint_filter_source",
]


@dataclass(frozen=True, slots=True)
class FilterViolation:
    """One actionable screener-source diagnostic."""

    code: str
    kind: FilterErrorKind
    lineno: int
    col_offset: int
    symbol: str
    message: str
    remediation: str

    def format(self) -> str:
        """Render one diagnostic line naming the rule and its remedy."""
        return (
            f"{self.code} [line {self.lineno}:{self.col_offset}]"
            f" [{self.kind.value}] {self.message} (fix: {self.remediation})"
        )


@dataclass(frozen=True, slots=True)
class FilterLintReport:
    """All boundary violations found in one screener source."""

    source_hash: str
    violations: tuple[FilterViolation, ...]

    @property
    def ok(self) -> bool:
        """Return whether the source stays inside the sandbox."""
        return not self.violations

    @property
    def codes(self) -> tuple[str, ...]:
        """Return the rule code of every violation in report order."""
        return tuple(violation.code for violation in self.violations)

    @property
    def kinds(self) -> tuple[FilterErrorKind, ...]:
        """Return the error kind of every violation in report order."""
        return tuple(violation.kind for violation in self.violations)

    def render(self) -> str:
        """Render a human-readable report for console output."""
        if not self.violations:
            return f"script {self.source_hash}: screener sandbox clean"
        header = f"script {self.source_hash}: {len(self.violations)} violation(s)"
        return "\n".join(
            [header, *(f"  {violation.format()}" for violation in self.violations)]
        )

    def raise_for_status(self) -> None:
        """Raise :class:`FilterEvaluationError` when the source is unclean."""
        if self.violations:
            raise FilterEvaluationError(self._error(self.violations[0]))

    def _error(self, violation: FilterViolation) -> FilterError:
        rendered = (
            f"{violation.code} [line {violation.lineno}] {violation.message}"
            f" (fix: {violation.remediation})"
        )
        return FilterError(violation.kind, rendered, self.source_hash)


def _as_int(value: object, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return value


def lint_filter_source(source: object) -> FilterLintReport:
    """Lint screener source text and return every boundary violation."""
    source_hash = script_hash(source) if isinstance(source, str) else ""
    records = validate_source(source)
    violations = tuple(
        FilterViolation(
            code=str(record.get("code", "")),
            kind=filter_error_from_record(record, source_hash).kind,
            lineno=_as_int(record.get("lineno"), 0),
            col_offset=_as_int(record.get("col_offset"), 0),
            symbol=str(record.get("symbol", "")),
            message=str(record.get("message", "")),
            remediation=str(record.get("remediation", "")),
        )
        for record in records
    )
    return FilterLintReport(source_hash, violations)


def assert_filter_source(source: str) -> FilterLintReport:
    """Lint ``source`` and raise :class:`FilterEvaluationError` when unclean."""
    report = lint_filter_source(source)
    report.raise_for_status()
    return report
