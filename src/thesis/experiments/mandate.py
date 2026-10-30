"""Explicit workflow-owned development and sealed forward data windows.

Discovery declares the windows before empirical work. They are persisted in the
execution's research_window result and recovered unchanged. Signal lookbacks,
label purges and liquidation rules belong to the research design within these
windows; they never silently move the requested dataset boundaries.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from thesis.experiments.contracts import Partition, PartitionRole

_SCHEMA = "fca-thesis-research-mandate-v3"
_HASH_DOMAIN = b"fca:thesis:research-mandate:v3\0"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
_WORKSPACE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_NANOS_PER_MICROSECOND = 1_000
_NANOS_PER_SECOND = 1_000_000_000


class MandateError(ValueError):
    """A declared window or persisted mandate is invalid."""


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


DataWindowResolver = Callable[[], DataWindowPolicy]


@dataclass(frozen=True, slots=True)
class ResearchMandate:
    """The explicit windows frozen by discovery for one execution."""

    workspace_id: str
    execution_id: str
    cutoff_ns: int
    partitions: tuple[Partition, ...]

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
        _integer(self.cutoff_ns, "cutoff_ns")
        if len(self.partitions) != 2 or any(
            not isinstance(p, Partition) for p in self.partitions
        ):
            raise MandateError("mandate requires development and forward partitions")
        development, forward = self.partitions
        if (
            development.role is not PartitionRole.DEVELOPMENT
            or forward.role is not PartitionRole.HOLDOUT
        ):
            raise MandateError("mandate partitions must be development then forward")
        if (
            not development.start_ns
            < development.end_ns
            <= forward.start_ns
            < forward.end_ns
            <= self.cutoff_ns
        ):
            raise MandateError(
                "windows must be nonempty, ordered, nonoverlapping and end by launch time"
            )
        if any(p.purged_ns or p.embargoed_ns for p in self.partitions):
            raise MandateError(
                "research design exclusions must not move dataset boundaries"
            )

    @property
    def development(self) -> Partition:
        return self.partitions[0]

    @property
    def forward(self) -> Partition:
        return self.partitions[1]

    @property
    def default_window(self) -> DataWindowPolicy:
        p = self.development
        return DataWindowPolicy(p.start_ns, p.end_ns, "development")

    @property
    def forward_window(self) -> DataWindowPolicy:
        p = self.forward
        return DataWindowPolicy(p.start_ns, p.end_ns, "forward", override=True)

    def window_violation(self, start_ns: int, end_ns: int) -> str | None:
        violation = self.default_window.window_violation(start_ns, end_ns)
        if violation is None:
            return None
        return f"{violation} The research mandate forward boundary is {rfc3339(self.forward.start_ns)}."

    @property
    def canonical_document(self) -> dict[str, object]:
        return {
            "schema": _SCHEMA,
            "workspaceId": self.workspace_id,
            "executionId": self.execution_id,
            "cutoffNs": self.cutoff_ns,
            "development": {
                "start": rfc3339(self.development.start_ns),
                "end": rfc3339(self.development.end_ns),
            },
            "forward": {
                "start": rfc3339(self.forward.start_ns),
                "end": rfc3339(self.forward.end_ns),
            },
        }

    @property
    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.canonical_document).encode("utf-8")

    @property
    def content_sha256(self) -> str:
        return hashlib.sha256(_HASH_DOMAIN + self.canonical_bytes).hexdigest()

    def as_dict(self) -> dict[str, object]:
        return self.canonical_document

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> ResearchMandate:
        if payload.get("schema") != _SCHEMA:
            raise MandateError("outdated research mandate schema")
        result = cls(
            workspace_id=_text(payload.get("workspaceId"), "workspaceId"),
            execution_id=_text(payload.get("executionId"), "executionId"),
            cutoff_ns=_integer(payload.get("cutoffNs"), "cutoffNs"),
            partitions=_declared_windows(payload),
        )
        if result.canonical_document != dict(payload):
            raise MandateError("mandate document is not canonical")
        return result


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


def rfc3339_ns(value: object) -> int:
    """Parse one RFC3339 timestamp into epoch nanoseconds."""
    if not isinstance(value, str) or not value.strip():
        raise MandateError("window bound must be an RFC3339 timestamp")
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as error:
        raise MandateError("window bound must be an RFC3339 timestamp") from error
    return datetime_ns(moment)


def _declared_windows(payload: Mapping[str, object]) -> tuple[Partition, Partition]:
    windows = []
    for name, role in (
        ("development", PartitionRole.DEVELOPMENT),
        ("forward", PartitionRole.HOLDOUT),
    ):
        window = payload.get(name)
        if not isinstance(window, Mapping) or set(window) != {"start", "end"}:
            raise MandateError(f"{name} requires explicit start and end timestamps")
        start = rfc3339_ns(window["start"])
        end = rfc3339_ns(window["end"])
        if start >= end:
            raise MandateError(f"{name} start must precede its end")
        windows.append(Partition(role=role, start_ns=start, end_ns=end))
    return windows[0], windows[1]


def derive_mandate(
    workspace_id: str, execution_id: str, declaration: Mapping[str, object]
) -> ResearchMandate:
    """Validate explicit windows against the persisted launch timestamp."""
    return ResearchMandate(
        workspace_id=workspace_id,
        execution_id=execution_id,
        cutoff_ns=rfc3339_ns(declaration.get("createdAt")),
        partitions=_declared_windows(declaration),
    )


def resolve_research_mandate(
    store: object | None, *, workspace_id: str, execution_id: str
) -> ResearchMandate | None:
    """Read discovery's frozen windows; absence means setup is still pending."""
    get = getattr(store, "get", None)
    if not callable(get):
        raise MandateError("research mandate requires the execution store")
    document = get(execution_id)
    artifact = document.phase_results.get("research_window")
    if artifact is None:
        return None
    mandate = ResearchMandate.from_dict(artifact)
    if (
        mandate.workspace_id != workspace_id
        or mandate.execution_id != execution_id
        or mandate.cutoff_ns != rfc3339_ns(document.launch_request.get("createdAt"))
    ):
        raise MandateError("research mandate does not match its execution")
    return mandate


def mandate_section(mandate: ResearchMandate) -> str:
    """Render the exact declared windows for downstream phase agents."""
    d, f = mandate.default_window, mandate.forward_window
    return "\n".join(
        [
            "## Research mandate",
            f"- Launch cutoff (UTC): {rfc3339(mandate.cutoff_ns)}",
            f"- Allowed development window (UTC): [{rfc3339(d.start_ns)}, {rfc3339(d.end_ns)})",
            f"- Sealed forward window (UTC): [{rfc3339(f.start_ns)}, {rfc3339(f.end_ns)})",
            "- Program configurations must stay inside their phase's window. Forward data is unavailable until forward validation.",
            "- Warm-up, label purges, and close-out exclusions must be explicit in the research design and remain within these windows. Never move the dataset boundaries to accommodate a signal.",
            f"- Mandate content: {mandate.content_sha256[:16]}",
        ]
    )
