"""Protected exposure ledger with reuse, influence, and downgrade mapping.

Every contact with protected data is recorded here: what was exposed, when,
under which execution and experiment, through which submission and window,
and to which actor channel.  The ledger is an append-only hash chain in the
repository journal style and composes with the established
``thesis.experiments`` vocabulary: exposure records map deterministically
onto :class:`~thesis.experiments.ExposureKind`, so reuse and influence flow
into the same evidence downgrades as ordinary experiments.

The authority remains the canonical owner of exposure and reuse.  This
ledger is the client-side record Thesis keeps from the signed facts and from
its own evaluation submissions; it never overrides an authority decision.

Exact mapping from authority sealed-status facts onto the experiments
exposure vocabulary (:class:`~thesis.experiments.ExposureKind`):

============================  ===========================================
Sealed-status fact kind       ExposureKind
============================  ===========================================
eligibility-granted           ``None`` (no protected data contact yet)
eligibility-denied            ``None``
eligibility-exhausted         ``None`` (budget accounting, not exposure)
window-exposed                ``SELECTION_EXPOSURE``
window-reused                 ``ADAPTIVE_REUSE``
influence-recorded            refined by payload ``influenceModifier``:
                              ``none`` -> ``None``,
                              ``unknown`` -> ``INFLUENCE_UNKNOWN``,
                              ``exposed`` -> ``KNOWLEDGE_EXPOSURE``
unknown-prior-exposure        ``UNKNOWN_PRIOR_EXPOSURE``
sealed-status                 ``None``
============================  ===========================================

Each mapped kind carries the established
``thesis.experiments.EXPOSURE_MODIFIERS`` modifiers, e.g. a
``window-reused`` fact maps to ``ADAPTIVE_REUSE`` and therefore to the
``reused`` and ``adaptive-selection`` evidence modifiers, and a
``window-exposed`` fact maps to ``SELECTION_EXPOSURE`` with the
``selection-exposed`` modifier.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType

from thesis.experiments.grades import (
    EXPOSURE_MODIFIERS,
    EvidenceClaim,
    EvidenceModifier,
    ExposureKind,
    downgrade,
)
from thesis.experiments.ledger import (
    InfluenceModifier,
    InfluenceSource,
    classify_influence,
)
from thesis.protected.contracts import (
    ProtectedTestError,
    SealedStatusKind,
)
from thesis.protected.facts import SealedStatusFact

_LEDGER_DOMAIN = b"fca:thesis-protected-exposure-ledger:v1\0"
_LEDGER_SCHEMA = "fca-thesis-protected-exposure-record-v1"

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
_WORKSPACE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")


class ExposureError(ProtectedTestError):
    """Base class for protected exposure ledger failures."""


class ExposureConflictError(ExposureError):
    """An exposure identifier was reused for different content."""


class ExposureChannel(StrEnum):
    """The actor channel through which protected data was exposed."""

    PROTECTED_RUNNER = "protected-runner"
    RCA_SESSION = "rca-session"
    MAIN_AGENT = "main-agent"
    USER = "user"
    OPERATOR = "operator"


def _identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ExposureError(f"{label} must be a safe identifier")
    return value


def _workspace_id(value: str) -> str:
    if not isinstance(value, str) or _WORKSPACE_ID.fullmatch(value) is None:
        raise ExposureError("workspace_id must be a safe workspace identifier")
    return value


def _digest(value: str, label: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ExposureError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ExposureError(f"{label} must be a non-empty string")
    if any(ord(character) < 32 for character in value):
        raise ExposureError(f"{label} contains unsupported characters")
    return value


def _timestamp(value: str, label: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise ExposureError(
            f"{label} must be a timezone-aware ISO-8601 timestamp"
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ExposureError(f"{label} must be a timezone-aware ISO-8601 timestamp")
    return (
        parsed.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )


def _jsonable(value: object) -> object:
    if value is None or isinstance(value, str | bool | int | float):
        return value
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ExposureError("exposure mappings must have string keys")
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_jsonable(item) for item in value]
    raise ExposureError("exposure values must be JSON-shaped")


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise ExposureError("exposure values must be canonical JSON") from error


def _freeze_mapping(value: Mapping[str, object], label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ExposureError(f"{label} must be a JSON-shaped mapping")
    frozen = MappingProxyType(dict(value))
    _canonical_json(_jsonable(dict(frozen)))
    return frozen


FACT_EXPOSURE_KINDS: Mapping[SealedStatusKind, ExposureKind | None] = {
    SealedStatusKind.ELIGIBILITY_GRANTED: None,
    SealedStatusKind.ELIGIBILITY_DENIED: None,
    SealedStatusKind.ELIGIBILITY_EXHAUSTED: None,
    SealedStatusKind.WINDOW_EXPOSED: ExposureKind.SELECTION_EXPOSURE,
    SealedStatusKind.WINDOW_REUSED: ExposureKind.ADAPTIVE_REUSE,
    SealedStatusKind.INFLUENCE_RECORDED: None,
    SealedStatusKind.UNKNOWN_PRIOR_EXPOSURE: ExposureKind.UNKNOWN_PRIOR_EXPOSURE,
    SealedStatusKind.SEALED_STATUS: None,
}


def influence_exposure_kind(modifier: InfluenceModifier) -> ExposureKind | None:
    """Map an influence modifier onto the experiments exposure vocabulary.

    Unknown provenance is ``INFLUENCE_UNKNOWN``; known provenance that is
    current with or after the protected window is ``KNOWLEDGE_EXPOSURE``;
    fully characterized pre-window provenance carries no exposure kind.
    """
    if not isinstance(modifier, InfluenceModifier):
        raise ExposureError("modifier must be an InfluenceModifier")
    if modifier is InfluenceModifier.UNKNOWN:
        return ExposureKind.INFLUENCE_UNKNOWN
    if modifier is InfluenceModifier.EXPOSED:
        return ExposureKind.KNOWLEDGE_EXPOSURE
    return None


def exposure_kind_for_fact(fact: SealedStatusFact) -> ExposureKind | None:
    """Return the deterministic experiments exposure kind for one fact."""
    if not isinstance(fact, SealedStatusFact):
        raise ExposureError("fact must be a SealedStatusFact")
    if fact.kind is SealedStatusKind.INFLUENCE_RECORDED:
        modifier_value = fact.payload.get("influenceModifier")
        if not isinstance(modifier_value, str):
            raise ExposureError(
                "influence-recorded facts must carry an influenceModifier"
            )
        try:
            modifier = InfluenceModifier(modifier_value)
        except ValueError as error:
            raise ExposureError(
                f"unknown influence modifier {modifier_value!r}"
            ) from error
        return influence_exposure_kind(modifier)
    return FACT_EXPOSURE_KINDS[fact.kind]


def modifiers_for(kind: ExposureKind) -> tuple[EvidenceModifier, ...]:
    """Return the established evidence modifiers for an exposure kind."""
    if not isinstance(kind, ExposureKind):
        raise ExposureError("kind must be an ExposureKind")
    return EXPOSURE_MODIFIERS[kind]


def window_influence_modifier(
    source: InfluenceSource,
    *,
    provenance_known: bool,
    post_window: bool,
) -> InfluenceModifier:
    """Classify one influence against a protected window.

    Direct composition with
    :func:`thesis.experiments.ledger.classify_influence`: unknown provenance
    is ``unknown``, known post-window provenance is ``exposed``, and only
    fully characterized pre-window provenance is ``none``.
    """
    return classify_influence(
        source, provenance_known=provenance_known, post_window=post_window
    )


def downgrade_for_fact(
    claim: EvidenceClaim,
    fact: SealedStatusFact,
    *,
    reason: str,
    actor: str,
) -> EvidenceClaim:
    """Apply the authority fact's mapped exposure to an evidence claim.

    Uses the established one-way downgrade table; facts that map to no
    exposure kind return the claim unchanged.  The fact's issue time becomes
    the recorded downgrade time.
    """
    kind = exposure_kind_for_fact(fact)
    if kind is None:
        return claim
    return downgrade(
        claim,
        kind,
        reason=reason,
        actor=actor,
        recorded_at=fact.issued_at,
    )


@dataclass(frozen=True, slots=True)
class ProtectedExposureRecord:
    """One recorded contact with protected data."""

    exposure_id: str
    workspace_id: str
    experiment_id: str
    submission_id: str
    window_id: str
    contract_sha256: str
    strategy_bundle_sha256: str
    lineage_id: str
    channel: ExposureChannel
    execution_id: str
    exposed_at: str
    description: str
    payload: Mapping[str, object] = MappingProxyType({})
    schema: str = _LEDGER_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "exposure_id", _identifier(self.exposure_id, "exposure_id")
        )
        object.__setattr__(self, "workspace_id", _workspace_id(self.workspace_id))
        object.__setattr__(
            self, "experiment_id", _identifier(self.experiment_id, "experiment_id")
        )
        object.__setattr__(
            self, "submission_id", _identifier(self.submission_id, "submission_id")
        )
        object.__setattr__(self, "window_id", _identifier(self.window_id, "window_id"))
        object.__setattr__(
            self, "contract_sha256", _digest(self.contract_sha256, "contract_sha256")
        )
        object.__setattr__(
            self,
            "strategy_bundle_sha256",
            _digest(self.strategy_bundle_sha256, "strategy_bundle_sha256"),
        )
        object.__setattr__(
            self, "lineage_id", _identifier(self.lineage_id, "lineage_id")
        )
        if not isinstance(self.channel, ExposureChannel):
            raise ExposureError("channel must be an ExposureChannel")
        object.__setattr__(
            self, "execution_id", _identifier(self.execution_id, "execution_id")
        )
        object.__setattr__(
            self, "exposed_at", _timestamp(self.exposed_at, "exposed_at")
        )
        object.__setattr__(self, "description", _text(self.description, "description"))
        object.__setattr__(self, "payload", _freeze_mapping(self.payload, "payload"))

    @property
    def canonical_document(self) -> dict[str, object]:
        """Return the canonical JSON document for this exposure record."""
        return {
            "channel": self.channel.value,
            "contractSha256": self.contract_sha256,
            "description": self.description,
            "exposedAt": self.exposed_at,
            "experimentId": self.experiment_id,
            "exposureId": self.exposure_id,
            "lineageId": self.lineage_id,
            "payload": _jsonable(dict(self.payload)),
            "schema": self.schema,
            "strategyBundleSha256": self.strategy_bundle_sha256,
            "submissionId": self.submission_id,
            "windowId": self.window_id,
            "workspaceId": self.workspace_id,
        }

    @property
    def content_sha256(self) -> str:
        """Return the content digest of this exposure record."""
        return hashlib.sha256(
            _canonical_json(self.canonical_document).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class ExposureEntry:
    """One immutable hash-chained exposure ledger entry."""

    sequence: int
    previous_hash: str
    entry_hash: str
    recorded_at: str
    exposure_kind: ExposureKind
    record: ProtectedExposureRecord


def _entry_hash(
    record: ProtectedExposureRecord,
    exposure_kind: ExposureKind,
    recorded_at: str,
    sequence: int,
    previous_hash: str,
) -> str:
    document = {
        "exposureKind": exposure_kind.value,
        "previousHash": previous_hash,
        "record": record.canonical_document,
        "recordedAt": recorded_at,
        "sequence": sequence,
    }
    payload = _canonical_json(_jsonable(document)).encode("utf-8")
    return hashlib.sha256(_LEDGER_DOMAIN + payload).hexdigest()


def verify_exposure_chain(entries: Sequence[ExposureEntry]) -> None:
    """Validate sequence continuity and the append-only hash chain."""
    previous_hash = "0" * 64
    for expected_sequence, entry in enumerate(entries, start=1):
        if entry.sequence != expected_sequence:
            raise ExposureError("exposure ledger sequence contains a gap")
        if entry.previous_hash != previous_hash:
            raise ExposureError(
                f"exposure entry {entry.sequence} breaks the hash chain"
            )
        expected_hash = _entry_hash(
            entry.record,
            entry.exposure_kind,
            entry.recorded_at,
            entry.sequence,
            entry.previous_hash,
        )
        if entry.entry_hash != expected_hash:
            raise ExposureError(f"exposure entry {entry.sequence} hash is invalid")
        previous_hash = entry.entry_hash


class ExposureLedger:
    """Append-only ledger of every contact with protected data.

    A window once exposed is marked reused: the first ledger entry for a
    window derives ``SELECTION_EXPOSURE`` and every later entry for the same
    window derives ``ADAPTIVE_REUSE`` unless an explicit kind is provided
    (for example the kind mapped from an authority fact).
    """

    def __init__(self) -> None:
        self._entries: list[ExposureEntry] = []
        self._index: dict[str, ExposureEntry] = {}
        self._windows: dict[str, list[str]] = {}

    def append(
        self,
        record: ProtectedExposureRecord,
        *,
        kind: ExposureKind | None = None,
    ) -> ExposureEntry:
        """Append one exposure, or return the existing identical entry.

        Re-appending identical content is idempotent; re-using an exposure ID
        for different content raises :class:`ExposureConflictError`.
        """
        if not isinstance(record, ProtectedExposureRecord):
            raise ExposureError("record must be a ProtectedExposureRecord")
        if kind is not None and not isinstance(kind, ExposureKind):
            raise ExposureError("kind must be an ExposureKind")
        existing = self._index.get(record.exposure_id)
        if existing is not None:
            if existing.record.content_sha256 == record.content_sha256:
                return existing
            raise ExposureConflictError(
                f"exposure ID {record.exposure_id!r} is already recorded"
            )
        derived = kind or self._derived_kind(record.window_id)
        previous_hash = self._entries[-1].entry_hash if self._entries else "0" * 64
        entry = ExposureEntry(
            sequence=len(self._entries) + 1,
            previous_hash=previous_hash,
            entry_hash=_entry_hash(
                record,
                derived,
                record.exposed_at,
                len(self._entries) + 1,
                previous_hash,
            ),
            recorded_at=record.exposed_at,
            exposure_kind=derived,
            record=record,
        )
        self._entries.append(entry)
        self._index[record.exposure_id] = entry
        self._windows.setdefault(record.window_id, []).append(record.exposure_id)
        return entry

    def entries(self) -> tuple[ExposureEntry, ...]:
        """Return all entries in hash-chain order."""
        return tuple(self._entries)

    def get(self, exposure_id: str) -> ExposureEntry | None:
        """Return the entry for an exposure ID, or ``None``."""
        return self._index.get(exposure_id)

    def exposure_count(self, window_id: str) -> int:
        """Return how many recorded contacts a window has had."""
        return len(self._windows.get(window_id, ()))

    def is_reused(self, window_id: str, submission_id: str | None = None) -> bool:
        """Return whether a window has been exposed before.

        With ``submission_id``, only exposures under other submissions count;
        re-submitting the same submission is still a reuse of the window but
        is reported separately through :meth:`exposure_count`.
        """
        exposure_ids = self._windows.get(window_id, ())
        if submission_id is None:
            return bool(exposure_ids)
        for exposure_id in exposure_ids:
            entry = self._index[exposure_id]
            if entry.record.submission_id != submission_id:
                return True
        return False

    def reused_windows(self) -> tuple[str, ...]:
        """Return window IDs that have more than one recorded contact."""
        return tuple(
            window_id
            for window_id, exposure_ids in self._windows.items()
            if len(exposure_ids) > 1
        )

    def verify(self) -> None:
        """Validate the append-only hash chain of this ledger."""
        verify_exposure_chain(self._entries)

    def _derived_kind(self, window_id: str) -> ExposureKind:
        if window_id in self._windows:
            return ExposureKind.ADAPTIVE_REUSE
        return ExposureKind.SELECTION_EXPOSURE


__all__ = [
    "ExposureChannel",
    "ExposureConflictError",
    "ExposureEntry",
    "ExposureError",
    "ExposureKind",
    "ExposureLedger",
    "FACT_EXPOSURE_KINDS",
    "InfluenceModifier",
    "ProtectedExposureRecord",
    "downgrade_for_fact",
    "exposure_kind_for_fact",
    "influence_exposure_kind",
    "modifiers_for",
    "verify_exposure_chain",
    "window_influence_modifier",
]
