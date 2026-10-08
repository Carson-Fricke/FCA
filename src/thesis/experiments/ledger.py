"""Append-only observed attempt and declared influence ledgers.

The attempt ledger records every system-observed research attempt —
hypotheses, generated candidates, model samples, aborted jobs, variants,
parameters, metric evaluations, market/horizon choices, holdout looks, and
contract deviations.  The influence ledger records declared influences: which
attempt influenced which decision, from model, human, retrieval, upload,
tool, or source provenance, with ``unknown`` or ``exposed`` modifiers when
provenance is incomplete or post-window.

Both ledgers are append-only hash chains in the repository journal style:
entries carry a sequence, the previous entry hash (genesis uses 64 zeros),
and a domain-separated SHA-256 over the canonical entry document.  Re-appending
identical content is idempotent; re-using an identifier with different
content is rejected.  There is no update or delete operation.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from types import MappingProxyType

_CHAIN_DOMAIN = b"fca:thesis-experiment-ledger:v1\0"
_ZERO_HASH = "0" * 64
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")


class LedgerError(RuntimeError):
    """Base class for experiment ledger failures."""


class LedgerIntegrityError(LedgerError):
    """A ledger sequence does not match its append-only hash chain."""


class AttemptConflictError(LedgerError):
    """An attempt identifier was reused for different content."""


class AttemptKind(StrEnum):
    """The system-observed attempt kinds the attempt ledger records."""

    HYPOTHESIS = "hypothesis"
    CANDIDATE = "candidate"
    MODEL_SAMPLE = "model_sample"
    ABORTED_JOB = "aborted_job"
    VARIANT = "variant"
    PARAMETER = "parameter"
    NULL_TEST = "null_test"
    VARIATION = "variation"
    METRIC = "metric"
    MARKET_HORIZON_CHOICE = "market_horizon_choice"
    HOLDOUT_LOOK = "holdout_look"
    CONTRACT_DEVIATION = "contract_deviation"


class InfluenceSource(StrEnum):
    """The provenance kind of a declared influence."""

    MODEL = "model"
    HUMAN = "human"
    RETRIEVAL = "retrieval"
    UPLOAD = "upload"
    TOOL = "tool"
    SOURCE = "source"


class InfluenceModifier(StrEnum):
    """Whether an influence is characterized, unknown, or exposure-bearing."""

    NONE = "none"
    UNKNOWN = "unknown"
    EXPOSED = "exposed"


def _identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise LedgerError(f"{label} must be a safe identifier")
    return value


def _digest(value: str, label: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise LedgerError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LedgerError(f"{label} must be a non-empty string")
    if any(ord(character) < 32 for character in value):
        raise LedgerError(f"{label} contains unsupported characters")
    return value


def _timestamp(value: str, label: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise LedgerError(
            f"{label} must be a timezone-aware ISO-8601 timestamp"
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise LedgerError(f"{label} must be a timezone-aware ISO-8601 timestamp")
    return (
        parsed.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )


def _document_text(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise LedgerError(f"attempt record {key} must be a string")
    return value


def _freeze_mapping(value: Mapping[str, object], label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise LedgerError(f"{label} must be a JSON-shaped mapping")
    frozen = MappingProxyType(dict(value))
    _canonical_json(_jsonable(dict(frozen)))
    return frozen


def _jsonable(value: object) -> object:
    if value is None or isinstance(value, str | bool | int | float):
        return value
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise LedgerError("ledger mappings must have string keys")
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_jsonable(item) for item in value]
    raise LedgerError("ledger values must be JSON-shaped")


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
        raise LedgerError("ledger values must be canonical JSON") from error


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    """One system-observed research attempt tied to one contract hash."""

    attempt_id: str
    contract_sha256: str
    kind: AttemptKind
    description: str
    actor: str
    observed_at: str
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "attempt_id", _identifier(self.attempt_id, "attempt_id")
        )
        object.__setattr__(
            self, "contract_sha256", _digest(self.contract_sha256, "contract_sha256")
        )
        if not isinstance(self.kind, AttemptKind):
            raise LedgerError("kind must be an AttemptKind")
        object.__setattr__(self, "description", _text(self.description, "description"))
        object.__setattr__(self, "actor", _identifier(self.actor, "actor"))
        object.__setattr__(
            self, "observed_at", _timestamp(self.observed_at, "observed_at")
        )
        object.__setattr__(self, "payload", _freeze_mapping(self.payload, "payload"))

    @property
    def canonical_document(self) -> dict[str, object]:
        """Return the canonical JSON document for this attempt."""
        return {
            "actor": self.actor,
            "attemptId": self.attempt_id,
            "contractSha256": self.contract_sha256,
            "description": self.description,
            "kind": self.kind.value,
            "observedAt": self.observed_at,
            "payload": _jsonable(dict(self.payload)),
            "schema": "fca-thesis-attempt-record-v1",
        }

    @property
    def content_sha256(self) -> str:
        """Return the content digest of this attempt record."""
        return hashlib.sha256(
            _canonical_json(self.canonical_document).encode("utf-8")
        ).hexdigest()

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> AttemptRecord:
        """Rebuild one attempt from its canonical JSON document.

        The kind is coerced through :class:`AttemptKind`, the payload must be
        a JSON-shaped mapping, and the rebuilt record must reproduce the
        canonical document exactly: a malformed, non-canonical, or extended
        document is rejected rather than silently repaired.
        """
        if not isinstance(payload, Mapping):
            raise LedgerError("attempt record must be a mapping")
        if payload.get("schema") != "fca-thesis-attempt-record-v1":
            raise LedgerError("attempt record schema is not supported")
        raw_kind = payload.get("kind")
        if not isinstance(raw_kind, str):
            raise LedgerError("attempt record kind must be a string")
        try:
            kind = AttemptKind(raw_kind)
        except ValueError as error:
            raise LedgerError(
                f"attempt record kind is not supported: {raw_kind!r}"
            ) from error
        raw_payload = payload.get("payload")
        if not isinstance(raw_payload, Mapping):
            raise LedgerError("attempt record payload must be a mapping")
        record = cls(
            attempt_id=_document_text(payload, "attemptId"),
            contract_sha256=_document_text(payload, "contractSha256"),
            kind=kind,
            description=_document_text(payload, "description"),
            actor=_document_text(payload, "actor"),
            observed_at=_document_text(payload, "observedAt"),
            payload=raw_payload,
        )
        if _canonical_json(_jsonable(record.canonical_document)) != _canonical_json(
            _jsonable(dict(payload))
        ):
            raise LedgerError("attempt record document is not canonical")
        return record


@dataclass(frozen=True, slots=True)
class InfluenceRecord:
    """One declared influence of an attempt on a decision."""

    influence_id: str
    attempt_id: str
    source: InfluenceSource
    modifier: InfluenceModifier
    decision: str
    declared_by: str
    declared_at: str
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "influence_id", _identifier(self.influence_id, "influence_id")
        )
        object.__setattr__(
            self, "attempt_id", _identifier(self.attempt_id, "attempt_id")
        )
        if not isinstance(self.source, InfluenceSource):
            raise LedgerError("source must be an InfluenceSource")
        if not isinstance(self.modifier, InfluenceModifier):
            raise LedgerError("modifier must be an InfluenceModifier")
        object.__setattr__(self, "decision", _text(self.decision, "decision"))
        object.__setattr__(
            self, "declared_by", _identifier(self.declared_by, "declared_by")
        )
        object.__setattr__(
            self, "declared_at", _timestamp(self.declared_at, "declared_at")
        )
        object.__setattr__(self, "payload", _freeze_mapping(self.payload, "payload"))
        if self.modifier is not InfluenceModifier.NONE:
            rationale = self.payload.get("rationale")
            if not isinstance(rationale, str):
                raise LedgerError(
                    f"influence rationale is required for "
                    f"{self.modifier.value} influences"
                )
            _text(
                rationale,
                f"influence rationale (required for {self.modifier.value} influences)",
            )

    @property
    def canonical_document(self) -> dict[str, object]:
        """Return the canonical JSON document for this influence."""
        return {
            "attemptId": self.attempt_id,
            "decision": self.decision,
            "declaredAt": self.declared_at,
            "declaredBy": self.declared_by,
            "influenceId": self.influence_id,
            "modifier": self.modifier.value,
            "payload": _jsonable(dict(self.payload)),
            "schema": "fca-thesis-influence-record-v1",
            "source": self.source.value,
        }

    @property
    def content_sha256(self) -> str:
        """Return the content digest of this influence record."""
        return hashlib.sha256(
            _canonical_json(self.canonical_document).encode("utf-8")
        ).hexdigest()


def classify_influence(
    source: InfluenceSource,
    *,
    provenance_known: bool,
    post_window: bool,
) -> InfluenceModifier:
    """Return the deterministic modifier for one influence.

    Unknown provenance produces ``unknown``; known provenance that is current
    with or after the protected window produces ``exposed``; only fully
    characterized pre-window provenance produces ``none``.
    """
    if not isinstance(source, InfluenceSource):
        raise LedgerError("source must be an InfluenceSource")
    if not provenance_known:
        return InfluenceModifier.UNKNOWN
    if post_window:
        return InfluenceModifier.EXPOSED
    return InfluenceModifier.NONE


@dataclass(frozen=True, slots=True)
class LedgerEntry:
    """One immutable hash-chained ledger entry."""

    sequence: int
    previous_hash: str
    entry_hash: str
    recorded_at: str
    record: AttemptRecord | InfluenceRecord


def _entry_hash(
    record: AttemptRecord | InfluenceRecord,
    recorded_at: str,
    sequence: int,
    previous_hash: str,
) -> str:
    document = {
        "previousHash": previous_hash,
        "record": record.canonical_document,
        "recordedAt": recorded_at,
        "sequence": sequence,
    }
    payload = _canonical_json(_jsonable(document)).encode("utf-8")
    return hashlib.sha256(_CHAIN_DOMAIN + payload).hexdigest()


def _chain_entry(
    sequence: int,
    previous_hash: str,
    record: AttemptRecord | InfluenceRecord,
    recorded_at: str,
) -> LedgerEntry:
    return LedgerEntry(
        sequence=sequence,
        previous_hash=previous_hash,
        entry_hash=_entry_hash(record, recorded_at, sequence, previous_hash),
        recorded_at=recorded_at,
        record=record,
    )


def verify_chain(entries: Sequence[LedgerEntry]) -> None:
    """Validate sequence continuity and the append-only hash chain."""
    previous_hash = _ZERO_HASH
    for expected_sequence, entry in enumerate(entries, start=1):
        if entry.sequence != expected_sequence:
            raise LedgerIntegrityError("ledger sequence contains a gap")
        if entry.previous_hash != previous_hash:
            raise LedgerIntegrityError(
                f"ledger entry {entry.sequence} breaks the hash chain"
            )
        expected_hash = _entry_hash(
            entry.record, entry.recorded_at, entry.sequence, entry.previous_hash
        )
        if entry.entry_hash != expected_hash:
            raise LedgerIntegrityError(f"ledger entry {entry.sequence} hash is invalid")
        previous_hash = entry.entry_hash


class AttemptLedger:
    """Append-only ledger of system-observed research attempts."""

    def __init__(self) -> None:
        self._entries: list[LedgerEntry] = []
        self._index: dict[str, LedgerEntry] = {}

    def append(self, record: AttemptRecord) -> LedgerEntry:
        """Append one attempt, or return the existing identical entry.

        Re-appending identical content is idempotent; re-using an attempt ID
        for different content raises :class:`AttemptConflictError`.
        """
        if not isinstance(record, AttemptRecord):
            raise LedgerError("record must be an AttemptRecord")
        existing = self._index.get(record.attempt_id)
        if existing is not None:
            if (
                isinstance(existing.record, AttemptRecord)
                and existing.record.content_sha256 == record.content_sha256
            ):
                return existing
            raise AttemptConflictError(
                f"attempt ID {record.attempt_id!r} is already recorded"
            )
        previous_hash = self._entries[-1].entry_hash if self._entries else _ZERO_HASH
        entry = _chain_entry(
            len(self._entries) + 1, previous_hash, record, record.observed_at
        )
        self._entries.append(entry)
        self._index[record.attempt_id] = entry
        return entry

    @classmethod
    def load(
        cls, records: Iterable[AttemptRecord | Mapping[str, object]]
    ) -> AttemptLedger:
        """Rebuild one attempt ledger by replaying ordered records through append.

        Records may be typed attempts or their canonical documents.  Replay
        derives the identical hash chain, and re-appended identical content is
        idempotent exactly as in :meth:`append`.
        """
        ledger = cls()
        for record in records:
            if isinstance(record, AttemptRecord):
                ledger.append(record)
            else:
                ledger.append(AttemptRecord.from_dict(record))
        return ledger

    def entries(self) -> tuple[LedgerEntry, ...]:
        """Return all entries in hash-chain order."""
        return tuple(self._entries)

    def get(self, attempt_id: str) -> LedgerEntry | None:
        """Return the entry for an attempt ID, or ``None``."""
        return self._index.get(attempt_id)

    def verify(self) -> None:
        """Validate the append-only hash chain of this ledger."""
        verify_chain(self._entries)


class InfluenceLedger:
    """Append-only ledger of declared influences on decisions."""

    def __init__(self) -> None:
        self._entries: list[LedgerEntry] = []
        self._index: dict[str, LedgerEntry] = {}

    def append(self, record: InfluenceRecord) -> LedgerEntry:
        """Append one influence, or return the existing identical entry.

        Re-appending identical content is idempotent; re-using an influence
        ID for different content raises :class:`AttemptConflictError`.
        """
        if not isinstance(record, InfluenceRecord):
            raise LedgerError("record must be an InfluenceRecord")
        existing = self._index.get(record.influence_id)
        if existing is not None:
            if (
                isinstance(existing.record, InfluenceRecord)
                and existing.record.content_sha256 == record.content_sha256
            ):
                return existing
            raise AttemptConflictError(
                f"influence ID {record.influence_id!r} is already recorded"
            )
        previous_hash = self._entries[-1].entry_hash if self._entries else _ZERO_HASH
        entry = _chain_entry(
            len(self._entries) + 1, previous_hash, record, record.declared_at
        )
        self._entries.append(entry)
        self._index[record.influence_id] = entry
        return entry

    def entries(self) -> tuple[LedgerEntry, ...]:
        """Return all entries in hash-chain order."""
        return tuple(self._entries)

    def get(self, influence_id: str) -> LedgerEntry | None:
        """Return the entry for an influence ID, or ``None``."""
        return self._index.get(influence_id)

    def verify(self) -> None:
        """Validate the append-only hash chain of this ledger."""
        verify_chain(self._entries)


__all__ = [
    "AttemptConflictError",
    "AttemptKind",
    "AttemptLedger",
    "AttemptRecord",
    "InfluenceLedger",
    "InfluenceModifier",
    "InfluenceRecord",
    "InfluenceSource",
    "LedgerEntry",
    "LedgerError",
    "LedgerIntegrityError",
    "classify_influence",
    "verify_chain",
]
