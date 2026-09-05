"""Model registry CRUD, role enforcement, and per-session verification.

The registry owns the versioned deployment records and cutoff attestations
that RCA sessions must pass through.  Every verified session records which
registry identity served it, the attestation it relied on, the conservative
cutoff check, and the effective capability and role policy — as a
deterministic, domain-separated, hash-chained record in the repository
journal style.  Verification happens at call time: opening a session runs
the full gate, and every capability use is re-checked against the session's
recorded policy and the registry's current revocation state.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import get_args

from thesis.agent.router import ModelProfile, Role
from thesis.registry.attestation import (
    AttestationRegistry,
    CutoffAttestation,
)
from thesis.registry.records import (
    DeploymentStatus,
    ModelCapability,
    ModelRegistryRecord,
    RegistryError,
    RoleRestrictionError,
)
from thesis.registry.tiers import (
    RCA_BASE_CAPABILITIES,
    RCA_DENIED_CAPABILITIES,
    RcaEvidenceTier,
    TierPolicyError,
    effective_capabilities,
    requires_attested_cutoff,
)

_SESSION_DOMAIN = b"fca:thesis-model-session:v1\0"
_ZERO_HASH = "0" * 64
_SESSION_SCHEMA = "fca-thesis-model-session-record-v1"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$")
_AGENT_ROLES: frozenset[str] = frozenset(get_args(Role))


class RegistryConflictError(RegistryError):
    """A registry record ID was reused for incompatible content."""


class SessionVerificationError(RegistryError):
    """A model session failed the registry verification gate."""

    def __init__(self, reasons: tuple[str, ...]) -> None:
        self.reasons = reasons
        super().__init__("model session rejected: " + "; ".join(reasons))


def _identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise RegistryError(f"{label} must be a portable identifier")
    return value


def _timestamp(value: datetime, label: str) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise RegistryError(f"{label} must be timezone-aware")
    return (
        value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )


def _iso_date_text(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise RegistryError(f"{label} must be an ISO-8601 calendar date")
    try:
        date.fromisoformat(value)
    except ValueError as error:
        raise RegistryError(f"{label} must be an ISO-8601 calendar date") from error
    return value


def _jsonable(value: object) -> object:
    if value is None or isinstance(value, str | bool | int | float):
        return value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_jsonable(item) for item in value]
    raise RegistryError("registry values must be JSON-shaped")


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
        raise RegistryError("registry values must be canonical JSON") from error


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_id() -> str:
    return uuid.uuid4().hex


@dataclass(frozen=True, slots=True)
class ModelSessionRecord:
    """The immutable verification record of one model session."""

    session_id: str
    workspace_scope: str
    record_id: str
    record_version: int
    record_sha256: str
    provider: str
    model_id: str
    model_version: str
    deployment: str | None
    attestation_id: str | None
    attestation_sha256: str | None
    verified_cutoff: str | None
    earliest_observation: str
    tier: RcaEvidenceTier
    role: str
    effective_capabilities: tuple[ModelCapability, ...]
    actor: str
    opened_at: str
    schema: str = _SESSION_SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.session_id, "session_id")
        _identifier(self.workspace_scope, "workspace_scope")
        _identifier(self.record_id, "record_id")
        if isinstance(self.record_version, bool) or not isinstance(
            self.record_version, int
        ):
            raise RegistryError("record_version must be an integer")
        if self.record_version < 1:
            raise RegistryError("record_version must be at least one")
        if (
            not isinstance(self.record_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", self.record_sha256) is None
        ):
            raise RegistryError("record_sha256 must be a SHA-256 digest")
        _identifier(self.provider, "provider")
        _identifier(self.model_id, "model_id")
        _identifier(self.model_version, "model_version")
        if self.deployment is not None:
            _identifier(self.deployment, "deployment")
        if (self.attestation_id is None) != (self.attestation_sha256 is None):
            raise RegistryError(
                "attestation identity and hash must be recorded together"
            )
        if self.attestation_id is not None:
            _identifier(self.attestation_id, "attestation_id")
        if (self.verified_cutoff is None) != (self.attestation_id is None):
            raise RegistryError("verified cutoff must be recorded with the attestation")
        if self.verified_cutoff is not None:
            _iso_date_text(self.verified_cutoff, "verified_cutoff")
        _iso_date_text(self.earliest_observation, "earliest_observation")
        if not isinstance(self.tier, RcaEvidenceTier):
            raise RegistryError("tier must be an RcaEvidenceTier")
        if not isinstance(self.role, str) or self.role not in _AGENT_ROLES:
            raise RegistryError("role must be a Thesis agent model role")
        capabilities = tuple(self.effective_capabilities)
        if not capabilities:
            raise RegistryError("effective capabilities cannot be empty")
        for capability in capabilities:
            if not isinstance(capability, ModelCapability):
                raise RegistryError(
                    "effective capabilities must be ModelCapability values"
                )
        if capabilities != tuple(
            sorted(set(capabilities), key=lambda item: item.value)
        ):
            raise RegistryError("effective capabilities must be sorted and unique")
        object.__setattr__(self, "effective_capabilities", capabilities)
        _identifier(self.actor, "actor")
        if not isinstance(self.opened_at, str):
            raise RegistryError("opened_at must be an ISO-8601 timestamp")
        try:
            parsed = datetime.fromisoformat(self.opened_at)
        except ValueError as error:
            raise RegistryError(
                "opened_at must be a timezone-aware ISO-8601 timestamp"
            ) from error
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise RegistryError("opened_at must be a timezone-aware ISO-8601 timestamp")

    @property
    def canonical_document(self) -> dict[str, object]:
        """Return the canonical JSON document that the session hash covers."""
        return {
            "actor": self.actor,
            "attestationId": self.attestation_id,
            "attestationSha256": self.attestation_sha256,
            "deployment": self.deployment,
            "earliestObservation": self.earliest_observation,
            "effectiveCapabilities": [
                capability.value for capability in self.effective_capabilities
            ],
            "modelId": self.model_id,
            "modelVersion": self.model_version,
            "openedAt": self.opened_at,
            "provider": self.provider,
            "recordId": self.record_id,
            "recordSha256": self.record_sha256,
            "recordVersion": self.record_version,
            "role": self.role,
            "schema": self.schema,
            "sessionId": self.session_id,
            "tier": self.tier.value,
            "verifiedCutoff": self.verified_cutoff,
            "workspaceScope": self.workspace_scope,
        }

    @property
    def content_sha256(self) -> str:
        """Return the content digest of this session record."""
        return hashlib.sha256(
            _canonical_json(_jsonable(self.canonical_document)).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class SessionLedgerEntry:
    """One immutable hash-chained session ledger entry."""

    sequence: int
    previous_hash: str
    entry_hash: str
    opened_at: str
    record: ModelSessionRecord


def _session_entry_hash(
    record: ModelSessionRecord,
    opened_at: str,
    sequence: int,
    previous_hash: str,
) -> str:
    document = {
        "openedAt": opened_at,
        "previousHash": previous_hash,
        "record": record.canonical_document,
        "sequence": sequence,
    }
    payload = _canonical_json(_jsonable(document)).encode("utf-8")
    return hashlib.sha256(_SESSION_DOMAIN + payload).hexdigest()


def _chain_entry(
    sequence: int,
    previous_hash: str,
    record: ModelSessionRecord,
) -> SessionLedgerEntry:
    return SessionLedgerEntry(
        sequence=sequence,
        previous_hash=previous_hash,
        entry_hash=_session_entry_hash(
            record, record.opened_at, sequence, previous_hash
        ),
        opened_at=record.opened_at,
        record=record,
    )


@dataclass(frozen=True, slots=True)
class ModelSession:
    """The verified session snapshot handed to the caller."""

    record: ModelSessionRecord
    registry_record: ModelRegistryRecord
    attestation: CutoffAttestation | None
    verified_cutoff: date | None


def verify_session_chain(entries: Sequence[SessionLedgerEntry]) -> None:
    """Validate sequence continuity and the session hash chain."""
    previous_hash = _ZERO_HASH
    for expected_sequence, entry in enumerate(entries, start=1):
        if entry.sequence != expected_sequence:
            raise RegistryError("session ledger sequence contains a gap")
        if entry.previous_hash != previous_hash:
            raise RegistryError(
                f"session ledger entry {entry.sequence} breaks the hash chain"
            )
        expected_hash = _session_entry_hash(
            entry.record, entry.opened_at, entry.sequence, entry.previous_hash
        )
        if entry.entry_hash != expected_hash:
            raise RegistryError(
                f"session ledger entry {entry.sequence} hash is invalid"
            )
        previous_hash = entry.entry_hash


class ModelRegistry:
    """CRUD and versioning for model registry records."""

    def __init__(self) -> None:
        self._current: dict[str, ModelRegistryRecord] = {}
        self._history: dict[str, tuple[ModelRegistryRecord, ...]] = {}

    def register(self, record: ModelRegistryRecord) -> ModelRegistryRecord:
        """Register a record, or return the identical registered record.

        Registering new content under an existing ID requires a strictly
        higher record version; revoked identities can never be re-registered.
        """
        if not isinstance(record, ModelRegistryRecord):
            raise RegistryError("record must be a ModelRegistryRecord")
        existing = self._current.get(record.record_id)
        if existing is not None:
            if existing.content_sha256 == record.content_sha256:
                return existing
            if existing.status is DeploymentStatus.REVOKED:
                raise RegistryConflictError(
                    f"record {record.record_id!r} is revoked and cannot be "
                    "re-registered"
                )
            if record.version <= existing.version:
                raise RegistryConflictError(
                    f"record {record.record_id!r} must register a version "
                    f"greater than {existing.version}"
                )
        self._history[record.record_id] = (
            *self._history.get(record.record_id, ()),
            record,
        )
        self._current[record.record_id] = record
        return record

    def get(self, record_id: str) -> ModelRegistryRecord | None:
        """Return the current record version for an ID, or ``None``."""
        return self._current.get(record_id)

    def history(self, record_id: str) -> tuple[ModelRegistryRecord, ...]:
        """Return every registered version of a record ID in order."""
        return self._history.get(record_id, ())

    def records(self) -> tuple[ModelRegistryRecord, ...]:
        """Return the current version of every record ordered by ID."""
        return tuple(self._current[record_id] for record_id in sorted(self._current))

    def revoke(
        self,
        record_id: str,
        *,
        actor: str,
        reason: str,
        revoked_at: datetime,
    ) -> ModelRegistryRecord:
        """Revoke a record by registering a new ``REVOKED`` version."""
        _identifier(actor, "revocation actor")
        if not isinstance(reason, str) or not reason.strip():
            raise RegistryError("revocation reason must be non-empty")
        existing = self._current.get(record_id)
        if existing is None:
            raise RegistryConflictError(f"unknown record: {record_id!r}")
        if existing.status is DeploymentStatus.REVOKED:
            raise RegistryConflictError(f"record {record_id!r} is already revoked")
        revoked = ModelRegistryRecord(
            record_id=existing.record_id,
            version=existing.version + 1,
            provider=existing.provider,
            model_id=existing.model_id,
            model_version=existing.model_version,
            deployment=existing.deployment,
            context_window_tokens=existing.context_window_tokens,
            capabilities=existing.capabilities,
            allowed_roles=existing.allowed_roles,
            immutable_weights=existing.immutable_weights,
            attestation_id=existing.attestation_id,
            registered_at=existing.registered_at,
            registered_by=existing.registered_by,
            max_output_tokens=existing.max_output_tokens,
            status=DeploymentStatus.REVOKED,
            revoked_at=_timestamp(revoked_at, "revoked_at"),
            revocation_reason=reason,
            schema=existing.schema,
        )
        return self.register(revoked)

    def check_role(self, record_id: str, role: str) -> ModelRegistryRecord:
        """Enforce the role restriction of a current record version."""
        record = self._current.get(record_id)
        if record is None:
            raise RoleRestrictionError(f"unknown record: {record_id!r}")
        if record.status is DeploymentStatus.REVOKED:
            raise RoleRestrictionError(
                f"record {record_id!r} is revoked and cannot serve roles"
            )
        if not isinstance(role, str) or role not in _AGENT_ROLES:
            raise RoleRestrictionError(f"unsupported Thesis role: {role!r}")
        if role not in record.allowed_roles:
            raise RoleRestrictionError(
                f"record {record_id!r} may not serve role {role!r}"
            )
        return record


class ModelSessionVerifier:
    """Per-session verification gate over the registry and attestations."""

    def __init__(
        self,
        *,
        registry: ModelRegistry,
        attestations: AttestationRegistry,
        clock: Callable[[], datetime] = _utc_now,
        id_factory: Callable[[], str] = _new_id,
    ) -> None:
        self._registry = registry
        self._attestations = attestations
        self._clock = clock
        self._id_factory = id_factory
        self._sessions: list[SessionLedgerEntry] = []
        self._index: dict[str, SessionLedgerEntry] = {}

    def sessions(self) -> tuple[SessionLedgerEntry, ...]:
        """Return all session ledger entries in hash-chain order."""
        return tuple(self._sessions)

    def get_session(self, session_id: str) -> ModelSessionRecord | None:
        """Return the recorded session for an ID, or ``None``."""
        entry = self._index.get(session_id)
        return None if entry is None else entry.record

    def verify(self) -> None:
        """Validate the append-only hash chain of recorded sessions."""
        verify_session_chain(self._sessions)

    def open_session(
        self,
        *,
        record_id: str,
        role: str,
        tier: RcaEvidenceTier,
        workspace_scope: str,
        earliest_observation: date,
        actor: str,
        requested_capabilities: Iterable[ModelCapability] = RCA_BASE_CAPABILITIES,
    ) -> ModelSession:
        """Verify a deployment for one session and record it.

        The gate runs in a fixed order and collects every failure reason:
        unknown, revoked, and (for attested tiers) mutable deployments; role
        restrictions; missing, revoked, or mismatched attestations; a
        cutoff that is not strictly before the earliest model-visible
        observation; and denied or undeclared capabilities.  Verified
        sessions are hash-chained with the registry identity, attestation,
        and verification results they relied on.
        """
        if not isinstance(tier, RcaEvidenceTier):
            raise TierPolicyError("tier must be an RcaEvidenceTier")
        reasons: list[str] = []
        record = self._registry.get(record_id)
        if record is None:
            reasons.append("unknown-deployment")
        elif record.status is DeploymentStatus.REVOKED:
            reasons.append("revoked-deployment")
        else:
            if requires_attested_cutoff(tier) and not record.immutable_weights:
                reasons.append("mutable-deployment")
            try:
                self._registry.check_role(record_id, role)
            except RoleRestrictionError:
                reasons.append("role-not-permitted")
        attestation = self._attestation_for(record, tier, earliest_observation, reasons)
        requested = frozenset(requested_capabilities)
        reasons.extend(self._capability_reasons(record, requested))
        if reasons:
            raise SessionVerificationError(tuple(reasons))
        assert record is not None
        effective = effective_capabilities(tier, requested)
        opened_at = _timestamp(self._clock(), "session clock")
        session_record = ModelSessionRecord(
            session_id=self._id_factory(),
            workspace_scope=_identifier(workspace_scope, "workspace_scope"),
            record_id=record.record_id,
            record_version=record.version,
            record_sha256=record.content_sha256,
            provider=record.provider,
            model_id=record.model_id,
            model_version=record.model_version,
            deployment=record.deployment,
            attestation_id=(
                None if attestation is None else attestation.attestation_id
            ),
            attestation_sha256=(
                None if attestation is None else attestation.content_sha256
            ),
            verified_cutoff=(
                None if attestation is None else attestation.cutoff_date.isoformat()
            ),
            earliest_observation=earliest_observation.isoformat(),
            tier=tier,
            role=role,
            effective_capabilities=tuple(sorted(effective)),
            actor=_identifier(actor, "actor"),
            opened_at=opened_at,
        )
        self._append(session_record)
        return ModelSession(
            record=session_record,
            registry_record=record,
            attestation=attestation,
            verified_cutoff=None if attestation is None else attestation.cutoff_date,
        )

    def assert_capability(self, session_id: str, capability: ModelCapability) -> None:
        """Re-check a capability against the session policy at call time.

        Revoking the deployment after the session opened invalidates the
        session: subsequent capability assertions fail.
        """
        entry = self._index.get(session_id)
        if entry is None:
            raise SessionVerificationError(("unknown-session",))
        record = entry.record
        if capability not in record.effective_capabilities:
            raise SessionVerificationError(
                (f"capability-not-granted:{capability.value}",)
            )
        current = self._registry.get(record.record_id)
        if current is None or current.status is DeploymentStatus.REVOKED:
            raise SessionVerificationError(("deployment-revoked-after-open",))

    def _attestation_for(
        self,
        record: ModelRegistryRecord | None,
        tier: RcaEvidenceTier,
        earliest_observation: date,
        reasons: list[str],
    ) -> CutoffAttestation | None:
        if record is None or not requires_attested_cutoff(tier):
            return None
        attestation = self._attestations.get(record.attestation_id)
        if attestation is None:
            reasons.append("unattested-deployment")
            return None
        if self._attestations.is_revoked(attestation.attestation_id):
            reasons.append("revoked-attestation")
            return None
        if (
            attestation.provider != record.provider
            or attestation.model_id != record.model_id
        ):
            reasons.append("attestation-mismatch")
            return None
        if attestation.cutoff_date >= earliest_observation:
            reasons.append("cutoff-not-before-observation")
        return attestation

    def _capability_reasons(
        self,
        record: ModelRegistryRecord | None,
        requested: frozenset[ModelCapability],
    ) -> list[str]:
        for capability in requested:
            if not isinstance(capability, ModelCapability):
                raise TierPolicyError("requested capabilities must be ModelCapability")
        reasons = [
            f"denied-capability-requested:{capability.value}"
            for capability in sorted(
                requested & RCA_DENIED_CAPABILITIES, key=lambda item: item.value
            )
        ]
        if record is None:
            return reasons
        reasons.extend(
            f"denied-capability-declared:{capability.value}"
            for capability in sorted(
                record.capabilities & RCA_DENIED_CAPABILITIES,
                key=lambda item: item.value,
            )
        )
        reasons.extend(
            f"capability-not-declared:{capability.value}"
            for capability in sorted(
                requested - record.capabilities, key=lambda item: item.value
            )
        )
        return reasons

    def _append(self, record: ModelSessionRecord) -> None:
        previous_hash = self._sessions[-1].entry_hash if self._sessions else _ZERO_HASH
        entry = _chain_entry(len(self._sessions) + 1, previous_hash, record)
        self._sessions.append(entry)
        self._index[record.session_id] = entry


def model_profile_for(
    record: ModelRegistryRecord, attestation: CutoffAttestation
) -> ModelProfile:
    """Build the agent router profile for a registry-verified deployment.

    This is the composition seam with ``thesis.agent``: the registry record
    and its attestation are the only source of the routing identity, cutoff,
    and cutoff evidence.  The agent ``ModelProfile`` requires a semantic
    version, so records whose exact model version is not semver-compatible
    cannot be routed and raise here instead of silently degrading.
    """
    if record.attestation_id != attestation.attestation_id:
        raise RegistryError("attestation does not belong to the registry record")
    if (
        not isinstance(record.model_version, str)
        or _VERSION.fullmatch(record.model_version) is None
    ):
        raise RegistryError(
            "model_version must be a semantic version to compose with the "
            "agent model-role policy"
        )
    return ModelProfile(
        profile_id=record.record_id,
        version=record.model_version,
        provider=record.provider,
        model=record.model_id if record.deployment is None else record.deployment,
        knowledge_cutoff=attestation.cutoff_date.isoformat(),
        cutoff_evidence=attestation.content_sha256,
    )


__all__ = [
    "ModelRegistry",
    "ModelSession",
    "ModelSessionRecord",
    "ModelSessionVerifier",
    "RegistryConflictError",
    "SessionLedgerEntry",
    "SessionVerificationError",
    "model_profile_for",
    "verify_session_chain",
]
