"""Research mandate: the launch-scoped data window and its enforcement.

A research mandate fixes the data a workflow execution may develop against. It
is derived on demand from the execution's persisted launch request, never from
workflow state or folds: ``createdAt`` is the cut-off and the request's
``partition`` holds the day counts. The same execution therefore always derives
the same mandate. A launch request without a usable ``createdAt`` or ``partition``
raises :class:`MandateError`; nothing falls back to an unrestricted window.

The launch request persists an explicit partition at launch. Dependency
horizons derive its purge and embargo allowances; derivation after launch reads
the persisted values alone.

For cut-off C (UTC), with the partition's day counts:

- forward (``PartitionRole.HOLDOUT``) is ``[C - forwardDays, C - embargoDays)``;
- development (``PartitionRole.DEVELOPMENT``) is
  ``[C - historyDays, C - forwardDays - purgeDays)``.

The purge gap ``[C - forwardDays - purgeDays, C - forwardDays)`` and the embargo
tail ``[C - embargoDays, C)`` belong to neither partition. The purge and
embargo allowances come from the declared label-horizon and liquidation windows
through ``derive_purge_and_embargo``, and the derived partitions must pass
``audit_intervals``.

Both bounds of a window are enforced by its policy. Committed program
configurations may only use data inside the policy window of their phase.
Violations are rejected with a deterministic repair message that names the
boundaries in RFC3339 UTC.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from thesis.experiments.contracts import (
    ExperimentContractError,
    Partition,
    PartitionRole,
)
from thesis.experiments.intervals import (
    EvaluatedWindow,
    IntervalKind,
    IntervalWindow,
    ProducedWindow,
    audit_intervals,
    derive_purge_and_embargo,
)

LAUNCH_PARTITION_PROVENANCE = "launch-v2"
MAX_HISTORY_DAYS = 36_500

_SCHEMA = "fca-thesis-research-mandate-v2"
_HASH_DOMAIN = b"fca:thesis-research-mandate:v2\0"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
_WORKSPACE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
_PARTITION_KEYS = frozenset(
    {"historyDays", "forwardDays", "purgeDays", "embargoDays", "provenance"}
)
_DEVELOPMENT_EXPERIMENT = "development"
_FORWARD_EXPERIMENT = "forward"
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_NANOS_PER_MICROSECOND = 1_000
_NANOS_PER_SECOND = 1_000_000_000
_DAY_NS = 86_400 * _NANOS_PER_SECOND


class MandateError(ValueError):
    """Raised when a mandate, its launch partition, or a window is malformed."""


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MandateError(f"{label} must be a non-empty string")
    if any(ord(character) < 32 for character in value):
        raise MandateError(f"{label} contains unsupported characters")
    return value


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MandateError(f"{label} must be an integer")
    return value


def _jsonable(value: object) -> Any:
    if value is None or isinstance(value, str | bool | int | float):
        return value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_jsonable(item) for item in value]
    raise MandateError("mandate values must be JSON-shaped")


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            _jsonable(value),
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise MandateError("mandate values must be canonical JSON") from error


def rfc3339(value_ns: int) -> str:
    """Render one epoch-nanosecond instant as an RFC3339 UTC timestamp."""
    seconds, nanoseconds = divmod(_integer(value_ns, "window bound"), _NANOS_PER_SECOND)
    try:
        moment = _EPOCH + timedelta(
            seconds=seconds, microseconds=nanoseconds // _NANOS_PER_MICROSECOND
        )
    except OverflowError as error:
        raise MandateError("window bound is outside the supported calendar") from error
    return moment.isoformat().replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class DataWindowPolicy:
    """The one data window a phase may use; both of its bounds are enforced."""

    start_ns: int
    end_ns: int
    label: str
    override: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "start_ns", _integer(self.start_ns, "window start"))
        object.__setattr__(self, "end_ns", _integer(self.end_ns, "window end"))
        if self.start_ns >= self.end_ns:
            raise MandateError("data window start must precede its end")
        object.__setattr__(self, "label", _text(self.label, "window label"))
        if not isinstance(self.override, bool):
            raise MandateError("window override must be a boolean")

    def permits(self, start_ns: int, end_ns: int) -> bool:
        """Return whether an exclusive-end window lies inside both policy bounds."""
        return self.start_ns <= start_ns < end_ns <= self.end_ns

    def window_violation(self, start_ns: int, end_ns: int) -> str | None:
        """Return the repair message when a window falls outside the policy.

        The permitted window is exclusive-ended: it must be non-empty, start no
        earlier than the policy start, and end no later than the policy end.
        The message names the violated bounds and the allowed window in RFC3339
        UTC so a committed configuration can be repaired precisely.
        """
        if self.permits(start_ns, end_ns):
            return None
        window = f"[{rfc3339(start_ns)}, {rfc3339(end_ns)})"
        allowed = f"[{rfc3339(self.start_ns)}, {rfc3339(self.end_ns)})"
        if start_ns >= end_ns:
            return (
                f"configuration window {window} is empty or inverted; the allowed "
                f"{self.label} window is {allowed}. Set configuration.start before "
                "configuration.end, inside that window."
            )
        repairs: list[str] = []
        if start_ns < self.start_ns:
            repairs.append(
                f"set configuration.start to at or after {rfc3339(self.start_ns)}"
            )
        if end_ns > self.end_ns:
            repairs.append(
                f"set configuration.end to at or before {rfc3339(self.end_ns)} "
                "(exclusive)"
            )
        return (
            f"configuration window {window} is outside the allowed {self.label} "
            f"window {allowed}; " + " and ".join(repairs) + "."
        )


def _validate_days(
    history_days: int, forward_days: int, purge_days: int, embargo_days: int
) -> None:
    if forward_days < 1:
        raise MandateError("partition forwardDays must be at least one")
    if purge_days < 0:
        raise MandateError("partition purgeDays must not be negative")
    if embargo_days < 0:
        raise MandateError("partition embargoDays must not be negative")
    if embargo_days >= forward_days:
        raise MandateError("partition embargoDays must be less than forwardDays")
    if history_days <= forward_days + purge_days:
        raise MandateError(
            "partition historyDays must exceed forwardDays plus purgeDays"
        )
    if history_days > MAX_HISTORY_DAYS:
        raise MandateError(f"partition historyDays must not exceed {MAX_HISTORY_DAYS}")


@dataclass(frozen=True, slots=True)
class LaunchPartition:
    """The data partition one launch request persists for its execution."""

    history_days: int
    forward_days: int
    purge_days: int
    embargo_days: int
    provenance: str = LAUNCH_PARTITION_PROVENANCE

    def __post_init__(self) -> None:
        _validate_days(
            _integer(self.history_days, "partition historyDays"),
            _integer(self.forward_days, "partition forwardDays"),
            _integer(self.purge_days, "partition purgeDays"),
            _integer(self.embargo_days, "partition embargoDays"),
        )
        if self.provenance != LAUNCH_PARTITION_PROVENANCE:
            raise MandateError("partition provenance is not a supported launch version")

    def as_dict(self) -> dict[str, object]:
        """Return the JSON document a launch request persists."""
        return {
            "historyDays": self.history_days,
            "forwardDays": self.forward_days,
            "purgeDays": self.purge_days,
            "embargoDays": self.embargo_days,
            "provenance": self.provenance,
        }

    @classmethod
    def from_dict(cls, payload: object) -> LaunchPartition:
        """Read a persisted partition; a missing, extra, or mistyped key fails."""
        if not isinstance(payload, Mapping):
            raise MandateError("launch request partition is missing or not an object")
        if set(payload) != _PARTITION_KEYS:
            raise MandateError(
                "launch request partition must declare historyDays, forwardDays, "
                "purgeDays, embargoDays, and provenance"
            )
        return cls(
            history_days=_integer(payload["historyDays"], "partition historyDays"),
            forward_days=_integer(payload["forwardDays"], "partition forwardDays"),
            purge_days=_integer(payload["purgeDays"], "partition purgeDays"),
            embargo_days=_integer(payload["embargoDays"], "partition embargoDays"),
            provenance=_text(payload["provenance"], "partition provenance"),
        )


def declared_launch_partition(payload: object) -> LaunchPartition:
    """Derive partition gaps from explicit information and position dependencies.

    Horizons are conservative maximum calendar-day spans across all planned
    parameter choices. A later design needing longer spans requires a new launch.
    No launch path selects a default holdout length or dependence allowance.
    """
    keys = {
        "historyDays",
        "forwardDays",
        "informationHorizonDays",
        "positionHorizonDays",
        "liquidationDays",
    }
    if not isinstance(payload, Mapping) or set(payload) != keys:
        raise MandateError(
            "partition must explicitly declare historyDays, forwardDays, informationHorizonDays, positionHorizonDays, and liquidationDays"
        )
    values = {name: _integer(payload[name], f"partition {name}") for name in keys}
    for name in ("informationHorizonDays", "positionHorizonDays", "liquidationDays"):
        if values[name] < 0:
            raise MandateError(f"partition {name} must not be negative")
    return LaunchPartition(
        history_days=values["historyDays"],
        forward_days=values["forwardDays"],
        purge_days=max(values["informationHorizonDays"], values["positionHorizonDays"]),
        embargo_days=values["liquidationDays"],
    )


def _partitions_for(
    cutoff_ns: int,
    *,
    history_days: int,
    forward_days: int,
    purge_days: int,
    embargo_days: int,
) -> tuple[Partition, Partition]:
    """Return the development and forward partitions for one launch cut-off.

    The purge is the label horizon declared across the gap before the forward
    window, and the embargo is the liquidation tail declared before the cut-off.
    ``derive_purge_and_embargo`` turns those declarations into allowances. The
    development window ends at the forward start less the purge allowance. Its
    label horizon must then stop at the forward start, which
    ``audit_intervals`` checks.
    """
    forward_start_ns = cutoff_ns - forward_days * _DAY_NS
    development_start_ns = cutoff_ns - history_days * _DAY_NS
    declared: list[IntervalWindow] = []
    if purge_days:
        declared.append(
            IntervalWindow(
                IntervalKind.LABEL,
                forward_start_ns - purge_days * _DAY_NS,
                forward_start_ns,
            )
        )
    if embargo_days:
        declared.append(
            IntervalWindow(
                IntervalKind.LIQUIDATION,
                cutoff_ns - embargo_days * _DAY_NS,
                cutoff_ns,
            )
        )
    allowances = derive_purge_and_embargo(declared)
    development_end_ns = forward_start_ns - allowances.purge_ns
    forward_end_ns = cutoff_ns - allowances.embargo_ns
    report = audit_intervals(
        produced_windows=(
            ProducedWindow(
                _DEVELOPMENT_EXPERIMENT,
                IntervalWindow(
                    IntervalKind.LABEL,
                    development_start_ns,
                    development_end_ns + allowances.purge_ns,
                ),
            ),
        ),
        evaluated_windows=(
            EvaluatedWindow(
                _FORWARD_EXPERIMENT,
                IntervalWindow(IntervalKind.LABEL, forward_start_ns, forward_end_ns),
            ),
        ),
    )
    if not report.clean:
        raise MandateError(
            "research mandate partitions fail the interval audit: "
            f"{report.findings[0].detail}"
        )
    return (
        Partition(
            role=PartitionRole.DEVELOPMENT,
            start_ns=development_start_ns,
            end_ns=development_end_ns,
            purged_ns=allowances.purge_ns,
        ),
        Partition(
            role=PartitionRole.HOLDOUT,
            start_ns=forward_start_ns,
            end_ns=forward_end_ns,
            embargoed_ns=allowances.embargo_ns,
        ),
    )


@dataclass(frozen=True, slots=True)
class ResearchMandate:
    """The launch-scoped data mandate derived from one durable execution."""

    workspace_id: str
    execution_id: str
    cutoff_ns: int
    history_days: int
    forward_days: int
    purge_days: int
    embargo_days: int
    partitions: tuple[Partition, ...]
    provenance: str = LAUNCH_PARTITION_PROVENANCE

    def __post_init__(self) -> None:
        if (
            not isinstance(self.workspace_id, str)
            or _WORKSPACE_ID.fullmatch(self.workspace_id) is None
        ):
            raise MandateError("workspace_id must be a safe workspace identifier")
        if (
            not isinstance(self.execution_id, str)
            or _IDENTIFIER.fullmatch(self.execution_id) is None
        ):
            raise MandateError("execution_id must be a safe identifier")
        cutoff_ns = _integer(self.cutoff_ns, "cutoff_ns")
        history_days = _integer(self.history_days, "history_days")
        forward_days = _integer(self.forward_days, "forward_days")
        purge_days = _integer(self.purge_days, "purge_days")
        embargo_days = _integer(self.embargo_days, "embargo_days")
        _validate_days(history_days, forward_days, purge_days, embargo_days)
        partitions = tuple(self.partitions)
        if len(partitions) != 2 or any(
            not isinstance(partition, Partition) for partition in partitions
        ):
            raise MandateError(
                "mandate requires exactly a development and a forward partition"
            )
        development, forward = partitions
        if (
            development.role is not PartitionRole.DEVELOPMENT
            or forward.role is not PartitionRole.HOLDOUT
        ):
            raise MandateError("mandate partitions must be development then forward")
        expected = _partitions_for(
            cutoff_ns,
            history_days=history_days,
            forward_days=forward_days,
            purge_days=purge_days,
            embargo_days=embargo_days,
        )
        if partitions != expected:
            raise MandateError(
                "mandate partitions must match the declared cut-off, day counts, "
                "purge, and embargo"
            )
        if self.provenance != LAUNCH_PARTITION_PROVENANCE:
            raise MandateError("mandate provenance is not a supported launch version")
        object.__setattr__(self, "partitions", partitions)

    @property
    def development(self) -> Partition:
        """Return the development partition."""
        return next(
            partition
            for partition in self.partitions
            if partition.role is PartitionRole.DEVELOPMENT
        )

    @property
    def forward(self) -> Partition:
        """Return the reserved forward partition."""
        return next(
            partition
            for partition in self.partitions
            if partition.role is PartitionRole.HOLDOUT
        )

    @property
    def default_window(self) -> DataWindowPolicy:
        """Return the development window as the enforceable policy."""
        development = self.development
        return DataWindowPolicy(
            start_ns=development.start_ns,
            end_ns=development.end_ns,
            label="development",
        )

    @property
    def forward_window(self) -> DataWindowPolicy:
        """Return the reserved forward window as the override policy."""
        forward = self.forward
        return DataWindowPolicy(
            start_ns=forward.start_ns,
            end_ns=forward.end_ns,
            label="forward",
            override=True,
        )

    def window_violation(self, start_ns: int, end_ns: int) -> str | None:
        """Return the repair message when a development window falls outside.

        The permitted window is exclusive-ended and inside both development
        bounds, and the message also names the forward boundary.
        """
        violation = self.default_window.window_violation(start_ns, end_ns)
        if violation is None:
            return None
        return (
            f"{violation} The research mandate forward boundary is "
            f"{rfc3339(self.forward.start_ns)}."
        )

    @property
    def canonical_document(self) -> dict[str, object]:
        """Return the canonical JSON document the content digest covers."""
        return {
            "cutoffNs": self.cutoff_ns,
            "embargoDays": self.embargo_days,
            "executionId": self.execution_id,
            "forwardDays": self.forward_days,
            "historyDays": self.history_days,
            "partitions": [
                {
                    "embargoedNs": partition.embargoed_ns,
                    "endNs": partition.end_ns,
                    "purgedNs": partition.purged_ns,
                    "role": partition.role.value,
                    "startNs": partition.start_ns,
                }
                for partition in self.partitions
            ],
            "provenance": self.provenance,
            "purgeDays": self.purge_days,
            "schema": _SCHEMA,
            "workspaceId": self.workspace_id,
        }

    @property
    def canonical_bytes(self) -> bytes:
        """Return the deterministic UTF-8 mandate representation."""
        return _canonical_json(self.canonical_document).encode("utf-8")

    @property
    def content_sha256(self) -> str:
        """Return the domain-separated content address of this mandate."""
        return hashlib.sha256(_HASH_DOMAIN + self.canonical_bytes).hexdigest()

    def as_dict(self) -> dict[str, object]:
        """Return the detached, JSON-ready mandate representation."""
        return self.canonical_document

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> ResearchMandate:
        """Rebuild one mandate from its canonical document.

        The rebuilt mandate must reproduce the document exactly, so a document
        whose purge, embargo, or partitions disagree with its day counts fails.
        """
        if not isinstance(payload, Mapping):
            raise MandateError("mandate document must be an object")
        if payload.get("schema") != _SCHEMA:
            raise MandateError("mandate document schema is not supported")
        raw_partitions = payload.get("partitions")
        if isinstance(raw_partitions, str | bytes) or not isinstance(
            raw_partitions, Sequence
        ):
            raise MandateError("mandate partitions must be a sequence")
        partitions: list[Partition] = []
        for item in raw_partitions:
            if not isinstance(item, Mapping):
                raise MandateError("mandate partitions must be objects")
            try:
                role = PartitionRole(item.get("role"))
            except (TypeError, ValueError) as error:
                raise MandateError("mandate partition role is not supported") from error
            try:
                partitions.append(
                    Partition(
                        role=role,
                        start_ns=_integer(item.get("startNs"), "partition startNs"),
                        end_ns=_integer(item.get("endNs"), "partition endNs"),
                        purged_ns=_integer(item.get("purgedNs"), "partition purgedNs"),
                        embargoed_ns=_integer(
                            item.get("embargoedNs"), "partition embargoedNs"
                        ),
                    )
                )
            except ExperimentContractError as error:
                raise MandateError(str(error)) from error
        mandate = cls(
            workspace_id=_text(payload.get("workspaceId"), "workspaceId"),
            execution_id=_text(payload.get("executionId"), "executionId"),
            cutoff_ns=_integer(payload.get("cutoffNs"), "cutoffNs"),
            history_days=_integer(payload.get("historyDays"), "historyDays"),
            forward_days=_integer(payload.get("forwardDays"), "forwardDays"),
            purge_days=_integer(payload.get("purgeDays"), "purgeDays"),
            embargo_days=_integer(payload.get("embargoDays"), "embargoDays"),
            partitions=tuple(partitions),
            provenance=_text(payload.get("provenance"), "provenance"),
        )
        if mandate.canonical_document != dict(payload):
            raise MandateError("mandate document is not canonical")
        return mandate


def datetime_ns(moment: datetime) -> int:
    """Return epoch nanoseconds for one timezone-aware instant."""
    if (
        not isinstance(moment, datetime)
        or moment.tzinfo is None
        or moment.utcoffset() is None
    ):
        raise MandateError("window bound must be a timezone-aware datetime")
    delta = moment.astimezone(UTC) - _EPOCH
    return (
        delta.days * 86_400 + delta.seconds
    ) * _NANOS_PER_SECOND + delta.microseconds * _NANOS_PER_MICROSECOND


def rfc3339_ns(value: str) -> int:
    """Parse one RFC3339 timestamp into epoch nanoseconds."""
    if not isinstance(value, str) or not value.strip():
        raise MandateError("window bound must be an RFC3339 timestamp")
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as error:
        raise MandateError("window bound must be an RFC3339 timestamp") from error
    return datetime_ns(moment)


def _launch_cutoff_ns(launch_request: Mapping[str, object]) -> int:
    value = launch_request.get("createdAt")
    if not isinstance(value, str) or not value.strip():
        raise MandateError("launch request createdAt must be an RFC3339 timestamp")
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as error:
        raise MandateError(
            "launch request createdAt must be an RFC3339 timestamp"
        ) from error
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise MandateError("launch request createdAt must be timezone-aware")
    return datetime_ns(moment)


def derive_mandate(
    workspace_id: str,
    execution_id: str,
    launch_request: Mapping[str, object],
) -> ResearchMandate:
    """Derive the launch-scoped mandate from an execution's launch request.

    The cut-off is the request's ``createdAt`` and the day counts are its
    persisted ``partition``. A missing or malformed value of either raises
    :class:`MandateError`; nothing is defaulted.
    """
    if not isinstance(launch_request, Mapping):
        raise MandateError("launch request must be an object")
    cutoff_ns = _launch_cutoff_ns(launch_request)
    partition = LaunchPartition.from_dict(launch_request.get("partition"))
    development, forward = _partitions_for(
        cutoff_ns,
        history_days=partition.history_days,
        forward_days=partition.forward_days,
        purge_days=partition.purge_days,
        embargo_days=partition.embargo_days,
    )
    return ResearchMandate(
        workspace_id=workspace_id,
        execution_id=execution_id,
        cutoff_ns=cutoff_ns,
        history_days=partition.history_days,
        forward_days=partition.forward_days,
        purge_days=partition.purge_days,
        embargo_days=partition.embargo_days,
        partitions=(development, forward),
        provenance=partition.provenance,
    )


def derive_launch_mandate(
    store: object | None,
    *,
    workspace_id: str,
    execution_id: str,
) -> ResearchMandate:
    """Derive the mandate from the execution's durable document in ``store``.

    A missing store, a missing execution document, or a document without a
    launch request raises :class:`MandateError`; there is no unrestricted
    fallback.
    """
    get = getattr(store, "get", None)
    if not callable(get):
        raise MandateError("research mandate requires the execution store")
    try:
        document = get(execution_id)
    except (KeyError, ValueError) as error:
        raise MandateError(
            f"execution {execution_id} has no durable launch document"
        ) from error
    launch_request = getattr(document, "launch_request", None)
    if not isinstance(launch_request, Mapping):
        raise MandateError(f"execution {execution_id} has no launch request")
    return derive_mandate(workspace_id, execution_id, launch_request)


def mandate_section(mandate: ResearchMandate) -> str:
    """Render the research-mandate prompt section for discovery and model creation."""
    development = mandate.default_window
    forward = mandate.forward_window
    return "\n".join(
        [
            "## Research mandate",
            f"- Launch cutoff (UTC): {rfc3339(mandate.cutoff_ns)}",
            f"- Forward boundary (UTC): {rfc3339(forward.start_ns)}",
            "- Allowed development window (UTC): "
            f"[{rfc3339(development.start_ns)}, {rfc3339(development.end_ns)})",
            "- Reserved forward window (UTC): "
            f"[{rfc3339(forward.start_ns)}, {rfc3339(forward.end_ns)})",
            f"- Purge: {mandate.purge_days} day(s) before the forward boundary; "
            f"embargo: {mandate.embargo_days} day(s) before the cutoff. Neither "
            "gap is usable.",
            "- Never author, update, validate, run, or batch-run a study or "
            "strategy whose configuration window is not inside the allowed "
            "development window; committed configurations must start at or after "
            "its start and end at or before its end (exclusive).",
            f"- Mandate content: {mandate.content_sha256[:16]} "
            f"(provenance {mandate.provenance})",
        ]
    )


__all__ = [
    "LAUNCH_PARTITION_PROVENANCE",
    "DataWindowPolicy",
    "LaunchPartition",
    "MandateError",
    "ResearchMandate",
    "datetime_ns",
    "declared_launch_partition",
    "derive_launch_mandate",
    "derive_mandate",
    "mandate_section",
    "rfc3339",
    "rfc3339_ns",
]
