"""Dependency-interval declarations and the deterministic interval audit.

Experiments declare the dependency windows of the results they consume:
warm-up, feature lookback, label horizon, position life, liquidation,
revised-data, and linked-instrument boundaries.  The audit in this module is
a pure, deterministic interval computation over those declarations.  Its
rules are:

1. ``undeclared_dependency``: every observed consumption window must be
   covered by a declaration between the same consumer and producer whose
   window of the same kind contains it (start before, end after).  An
   observed look at another experiment's results without a covering
   declaration is a finding.
2. ``overlapping_dependency``: for one consumer, label or position windows
   declared against two different producers must not intersect.  An
   intersection is tolerated only when the consumer declares an embargo
   window of kind ``liquidation`` or ``linked_instrument`` that contains the
   whole intersection span.
3. ``undeclared_boundary``: an evaluated window of one experiment that
   intersects a produced window of any other experiment requires a
   declaration between the two whose windows cover the intersection.  This
   is the look-between check: evaluation inside another experiment's
   dependency window is never silent.
4. ``multiple_testing_exposure``: an experiment with a declared look count
   and stopping rule whose observed looks exceed the declared budget, or a
   look count without any declared stopping rule, is a finding.

Findings are returned sorted by kind, consumer, producer, window span, and
detail, so identical inputs always produce identical reports.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from thesis.experiments.contracts import StoppingRule

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")


class IntervalAuditError(ValueError):
    """Raised when dependency-interval declarations are invalid."""


class IntervalKind(StrEnum):
    """The dependency-interval boundary kinds an experiment can declare."""

    WARM_UP = "warm_up"
    LOOKBACK = "lookback"
    LABEL = "label"
    POSITION = "position"
    LIQUIDATION = "liquidation"
    REVISED_DATA = "revised_data"
    LINKED_INSTRUMENT = "linked_instrument"


class IntervalFindingKind(StrEnum):
    """The deterministic findings emitted by the dependency-interval audit."""

    UNDECLARED_DEPENDENCY = "undeclared_dependency"
    OVERLAPPING_DEPENDENCY = "overlapping_dependency"
    UNDECLARED_BOUNDARY = "undeclared_boundary"
    MULTIPLE_TESTING_EXPOSURE = "multiple_testing_exposure"


_PURGE_KINDS = (IntervalKind.LABEL, IntervalKind.POSITION, IntervalKind.REVISED_DATA)
_EMBARGO_KINDS = (IntervalKind.LIQUIDATION, IntervalKind.LINKED_INSTRUMENT)
_CONFLICT_KINDS = (IntervalKind.LABEL, IntervalKind.POSITION)


def _identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise IntervalAuditError(f"{label} must be a safe identifier")
    return value


def _timestamp(value: str, label: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise IntervalAuditError(
            f"{label} must be a timezone-aware ISO-8601 timestamp"
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise IntervalAuditError(f"{label} must be a timezone-aware ISO-8601 timestamp")
    return (
        parsed.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )


def _bounds(start_ns: int, end_ns: int, label: str) -> tuple[int, int]:
    if isinstance(start_ns, bool) or not isinstance(start_ns, int):
        raise IntervalAuditError(f"{label} boundaries must be integers")
    if isinstance(end_ns, bool) or not isinstance(end_ns, int):
        raise IntervalAuditError(f"{label} boundaries must be integers")
    if start_ns >= end_ns:
        raise IntervalAuditError(f"{label} start must precede its end")
    return start_ns, end_ns


@dataclass(frozen=True, slots=True)
class IntervalWindow:
    """One declared dependency window in epoch nanoseconds."""

    kind: IntervalKind
    start_ns: int
    end_ns: int

    def __post_init__(self) -> None:
        if not isinstance(self.kind, IntervalKind):
            raise IntervalAuditError("kind must be an IntervalKind")
        _bounds(self.start_ns, self.end_ns, "interval window")


@dataclass(frozen=True, slots=True)
class IntervalSpan:
    """A computed time span, used for intersections and embargo coverage."""

    start_ns: int
    end_ns: int


@dataclass(frozen=True, slots=True)
class DependencyDeclaration:
    """A pre-evaluation declaration of the results an experiment consumes."""

    declaration_id: str
    consumer_experiment_id: str
    producer_experiment_id: str
    windows: tuple[IntervalWindow, ...]
    declared_by: str
    declared_at: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "declaration_id", _identifier(self.declaration_id, "declaration_id")
        )
        object.__setattr__(
            self,
            "consumer_experiment_id",
            _identifier(self.consumer_experiment_id, "consumer_experiment_id"),
        )
        object.__setattr__(
            self,
            "producer_experiment_id",
            _identifier(self.producer_experiment_id, "producer_experiment_id"),
        )
        if self.consumer_experiment_id == self.producer_experiment_id:
            raise IntervalAuditError("an experiment cannot depend on itself")
        windows = tuple(self.windows)
        if not windows:
            raise IntervalAuditError("declarations must contain at least one window")
        for window in windows:
            if not isinstance(window, IntervalWindow):
                raise IntervalAuditError("windows must contain IntervalWindow values")
        object.__setattr__(
            self,
            "windows",
            tuple(
                sorted(
                    windows,
                    key=lambda item: (item.kind.value, item.start_ns, item.end_ns),
                )
            ),
        )
        object.__setattr__(
            self, "declared_by", _identifier(self.declared_by, "declared_by")
        )
        object.__setattr__(
            self, "declared_at", _timestamp(self.declared_at, "declared_at")
        )


@dataclass(frozen=True, slots=True)
class ObservedConsumption:
    """One observed read of another experiment's results."""

    consumer_experiment_id: str
    producer_experiment_id: str
    window: IntervalWindow

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "consumer_experiment_id",
            _identifier(self.consumer_experiment_id, "consumer_experiment_id"),
        )
        object.__setattr__(
            self,
            "producer_experiment_id",
            _identifier(self.producer_experiment_id, "producer_experiment_id"),
        )
        if not isinstance(self.window, IntervalWindow):
            raise IntervalAuditError("window must be an IntervalWindow")


@dataclass(frozen=True, slots=True)
class ProducedWindow:
    """The interval of data or results one experiment produced."""

    experiment_id: str
    window: IntervalWindow

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "experiment_id", _identifier(self.experiment_id, "experiment_id")
        )
        if not isinstance(self.window, IntervalWindow):
            raise IntervalAuditError("window must be an IntervalWindow")


@dataclass(frozen=True, slots=True)
class EvaluatedWindow:
    """The interval of data whose results feed one experiment's claim."""

    consumer_experiment_id: str
    window: IntervalWindow

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "consumer_experiment_id",
            _identifier(self.consumer_experiment_id, "consumer_experiment_id"),
        )
        if not isinstance(self.window, IntervalWindow):
            raise IntervalAuditError("window must be an IntervalWindow")


@dataclass(frozen=True, slots=True)
class PurgeEmbargo:
    """Purge and embargo allowances derived from declared windows.

    ``purge_ns`` is the maximum span among label, position, and revised-data
    windows: information that persists past a partition boundary.
    ``embargo_ns`` is the maximum span among liquidation and
    linked-instrument windows: economically linked adjacent records.
    """

    purge_ns: int
    embargo_ns: int
    purge_basis: tuple[IntervalKind, ...]
    embargo_basis: tuple[IntervalKind, ...]


@dataclass(frozen=True, slots=True)
class IntervalFinding:
    """One deterministic audit finding."""

    kind: IntervalFindingKind
    detail: str
    consumer_experiment_id: str | None = None
    producer_experiment_id: str | None = None
    start_ns: int | None = None
    end_ns: int | None = None


@dataclass(frozen=True, slots=True)
class IntervalAuditReport:
    """The complete, deterministically ordered result of one audit."""

    findings: tuple[IntervalFinding, ...] = ()

    @property
    def clean(self) -> bool:
        """Return whether the audit produced no findings."""
        return not self.findings


def derive_purge_and_embargo(
    windows: Sequence[IntervalWindow],
) -> PurgeEmbargo:
    """Derive purge and embargo allowances from declared windows.

    The purge is the maximum span among label, position, and revised-data
    windows; the embargo is the maximum span among liquidation and
    linked-instrument windows.  Windows of other kinds contribute nothing.
    Both are zero when no relevant window is declared.
    """
    prepared: list[IntervalWindow] = []
    for window in windows:
        if not isinstance(window, IntervalWindow):
            raise IntervalAuditError("windows must contain IntervalWindow values")
        prepared.append(window)
    purge_basis = tuple(
        kind
        for kind in sorted(_PURGE_KINDS, key=lambda item: item.value)
        if any(window.kind is kind for window in prepared)
    )
    embargo_basis = tuple(
        kind
        for kind in sorted(_EMBARGO_KINDS, key=lambda item: item.value)
        if any(window.kind is kind for window in prepared)
    )
    purge_ns = max(
        (
            window.end_ns - window.start_ns
            for window in prepared
            if window.kind in _PURGE_KINDS
        ),
        default=0,
    )
    embargo_ns = max(
        (
            window.end_ns - window.start_ns
            for window in prepared
            if window.kind in _EMBARGO_KINDS
        ),
        default=0,
    )
    return PurgeEmbargo(
        purge_ns=purge_ns,
        embargo_ns=embargo_ns,
        purge_basis=purge_basis,
        embargo_basis=embargo_basis,
    )


def _intersection(first: IntervalWindow, second: IntervalWindow) -> IntervalSpan | None:
    start = max(first.start_ns, second.start_ns)
    end = min(first.end_ns, second.end_ns)
    if start >= end:
        return None
    return IntervalSpan(start_ns=start, end_ns=end)


def _covers(declared: IntervalWindow, observed: IntervalWindow) -> bool:
    return (
        declared.kind is observed.kind
        and declared.start_ns <= observed.start_ns
        and observed.end_ns <= declared.end_ns
    )


def _contains_span(declared: IntervalWindow, span: IntervalSpan) -> bool:
    return declared.start_ns <= span.start_ns and span.end_ns <= declared.end_ns


def _finding_sort_key(finding: IntervalFinding) -> tuple[str, str, str, int, int, str]:
    return (
        finding.kind.value,
        finding.consumer_experiment_id or "",
        finding.producer_experiment_id or "",
        -1 if finding.start_ns is None else finding.start_ns,
        -1 if finding.end_ns is None else finding.end_ns,
        finding.detail,
    )


def audit_intervals(
    *,
    declarations: Sequence[DependencyDeclaration] = (),
    observed_consumptions: Sequence[ObservedConsumption] = (),
    produced_windows: Sequence[ProducedWindow] = (),
    evaluated_windows: Sequence[EvaluatedWindow] = (),
    stopping_rules: Mapping[str, StoppingRule] | None = None,
    look_counts: Mapping[str, int] | None = None,
) -> IntervalAuditReport:
    """Run the deterministic dependency-interval audit over the inputs."""
    for declaration in declarations:
        if not isinstance(declaration, DependencyDeclaration):
            raise IntervalAuditError(
                "declarations must be DependencyDeclaration values"
            )
    for consumption in observed_consumptions:
        if not isinstance(consumption, ObservedConsumption):
            raise IntervalAuditError("consumptions must be ObservedConsumption values")
    for produced in produced_windows:
        if not isinstance(produced, ProducedWindow):
            raise IntervalAuditError("produced windows must be ProducedWindow values")
    for evaluated in evaluated_windows:
        if not isinstance(evaluated, EvaluatedWindow):
            raise IntervalAuditError("evaluated windows must be EvaluatedWindow values")

    by_pair: dict[tuple[str, str], tuple[DependencyDeclaration, ...]] = {}
    for declaration in declarations:
        key = (declaration.consumer_experiment_id, declaration.producer_experiment_id)
        by_pair[key] = by_pair.get(key, ()) + (declaration,)

    findings: list[IntervalFinding] = []

    for consumption in observed_consumptions:
        matching = by_pair.get(
            (consumption.consumer_experiment_id, consumption.producer_experiment_id), ()
        )
        covered = any(
            _covers(window, consumption.window)
            for declaration in matching
            for window in declaration.windows
        )
        if not covered:
            findings.append(
                IntervalFinding(
                    kind=IntervalFindingKind.UNDECLARED_DEPENDENCY,
                    detail=(
                        f"observed {consumption.window.kind.value} window "
                        f"[{consumption.window.start_ns}, {consumption.window.end_ns}) "
                        f"from {consumption.producer_experiment_id} is not covered "
                        "by a declaration"
                    ),
                    consumer_experiment_id=consumption.consumer_experiment_id,
                    producer_experiment_id=consumption.producer_experiment_id,
                    start_ns=consumption.window.start_ns,
                    end_ns=consumption.window.end_ns,
                )
            )

    consumers = sorted(
        {declaration.consumer_experiment_id for declaration in declarations}
    )
    for consumer in consumers:
        producer_windows: dict[str, tuple[IntervalWindow, ...]] = {}
        embargo_windows: tuple[IntervalWindow, ...] = ()
        for declaration in declarations:
            if declaration.consumer_experiment_id != consumer:
                continue
            producer_windows.setdefault(declaration.producer_experiment_id, ())
            producer_windows[declaration.producer_experiment_id] = (
                producer_windows[declaration.producer_experiment_id]
                + declaration.windows
            )
            embargo_windows = embargo_windows + tuple(
                window
                for window in declaration.windows
                if window.kind in _EMBARGO_KINDS
            )
        producers = sorted(producer_windows)
        for first_index, first_producer in enumerate(producers):
            for second_producer in producers[first_index + 1 :]:
                for first_window in producer_windows[first_producer]:
                    if first_window.kind not in _CONFLICT_KINDS:
                        continue
                    for second_window in producer_windows[second_producer]:
                        if second_window.kind not in _CONFLICT_KINDS:
                            continue
                        overlap = _intersection(first_window, second_window)
                        if overlap is None:
                            continue
                        embargoed = any(
                            _contains_span(window, overlap)
                            for window in embargo_windows
                        )
                        if embargoed:
                            continue
                        findings.append(
                            IntervalFinding(
                                kind=IntervalFindingKind.OVERLAPPING_DEPENDENCY,
                                detail=(
                                    f"label/position windows from {first_producer} and "
                                    f"{second_producer} overlap on "
                                    f"[{overlap.start_ns}, {overlap.end_ns}) without "
                                    "a declared embargo"
                                ),
                                consumer_experiment_id=consumer,
                                producer_experiment_id=second_producer,
                                start_ns=overlap.start_ns,
                                end_ns=overlap.end_ns,
                            )
                        )

    for evaluated in evaluated_windows:
        for produced in produced_windows:
            if produced.experiment_id == evaluated.consumer_experiment_id:
                continue
            overlap = _intersection(evaluated.window, produced.window)
            if overlap is None:
                continue
            matching = by_pair.get(
                (evaluated.consumer_experiment_id, produced.experiment_id), ()
            )
            covered = any(
                _contains_span(window, overlap)
                for declaration in matching
                for window in declaration.windows
            )
            if not covered:
                findings.append(
                    IntervalFinding(
                        kind=IntervalFindingKind.UNDECLARED_BOUNDARY,
                        detail=(
                            f"evaluated window [{overlap.start_ns}, {overlap.end_ns}) "
                            f"intersects produced window of {produced.experiment_id} "
                            "without a declaration"
                        ),
                        consumer_experiment_id=evaluated.consumer_experiment_id,
                        producer_experiment_id=produced.experiment_id,
                        start_ns=overlap.start_ns,
                        end_ns=overlap.end_ns,
                    )
                )

    rules = stopping_rules if stopping_rules is not None else {}
    counts = look_counts if look_counts is not None else {}
    for experiment_id in sorted(set(rules) | set(counts)):
        rule = rules.get(experiment_id)
        count = counts.get(experiment_id)
        if rule is None:
            findings.append(
                IntervalFinding(
                    kind=IntervalFindingKind.MULTIPLE_TESTING_EXPOSURE,
                    detail=(
                        f"experiment {experiment_id} has observed looks but no "
                        "declared stopping rule"
                    ),
                    consumer_experiment_id=experiment_id,
                )
            )
            continue
        if count is None:
            continue
        exposure = rule.exposure(count)
        if exposure.exceeds_budget:
            findings.append(
                IntervalFinding(
                    kind=IntervalFindingKind.MULTIPLE_TESTING_EXPOSURE,
                    detail=(
                        f"experiment {experiment_id} observed {count} looks against "
                        f"declared budget {exposure.allowed_looks}"
                    ),
                    consumer_experiment_id=experiment_id,
                )
            )

    return IntervalAuditReport(findings=tuple(sorted(findings, key=_finding_sort_key)))


__all__ = [
    "DependencyDeclaration",
    "EvaluatedWindow",
    "IntervalAuditError",
    "IntervalAuditReport",
    "IntervalFinding",
    "IntervalFindingKind",
    "IntervalKind",
    "IntervalSpan",
    "IntervalWindow",
    "ObservedConsumption",
    "ProducedWindow",
    "PurgeEmbargo",
    "audit_intervals",
    "derive_purge_and_embargo",
]
