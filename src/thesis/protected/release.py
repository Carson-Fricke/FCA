"""Protected RCA release composition with session binding and risk record.

A :class:`ProtectedRcaRelease` composes the authority-side steps of one
blinded RCA release:

1. aliasing selection by evidence tier and release gates (never a Thesis
   user mode);
2. fail-closed screening of the original strategy source: identity-revealing
   source blocks the release and is never rewritten or relabeled;
3. a deterministic identity mapping held only in the release authority
   layer and never serialized into released payloads;
4. allowlist-driven artifact transformation that removes or transforms
   path, tag, log, error, timestamp, and timing side channels;
5. binding of the full aliased run to exactly one named RCA session, with a
   deterministic egress filter for advice returning to the main agent; and
6. the structured release risk assessment (cross-release disclosure,
   empirical reidentification risk, diagnostic utility, residual risk).

The release record serializes to canonical sorted-key JSON (schema v1) for
audit; the identity mapping itself is never part of it, and the released
artifacts, manifest, and filtered advice carry no real identity value.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType

from thesis.protected.aliasing import (
    AliasPlan,
    AliasStrategyKind,
    IdentityMapping,
    IdentitySurface,
    RcaEvidenceTier,
    ReleaseBlockedError,
    ReleaseGate,
    build_aliasing,
)
from thesis.protected.contracts import ProtectedTestError
from thesis.protected.flow_checks import (
    OutputSurface,
    assert_no_raw_identifiers,
    sanitize_output,
    screen_source,
)

RELEASE_SCHEMA = "fca-thesis-protected-rca-release-v1"
RISK_SCHEMA = "fca-thesis-release-risk-assessment-v1"
MANIFEST_SCHEMA = "fca-thesis-rca-transformation-manifest-v1"

_RELEASE_DOMAIN = b"fca:thesis-protected-rca-release:v1\0"

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
_WORKSPACE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
_ISO_DATETIME = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
)
_ISO_DATE = re.compile(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)")

_MASKING_ALLOWLIST = frozenset({OutputSurface.TIMING})
_HARDENED_ALLOWLIST: frozenset[OutputSurface] = frozenset()


class ReleaseRequestError(ProtectedTestError):
    """A protected RCA release request would be incomplete or inconsistent."""


def _identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ReleaseRequestError(f"{label} must be a safe identifier")
    return value


def _workspace_id(value: str) -> str:
    if not isinstance(value, str) or _WORKSPACE_ID.fullmatch(value) is None:
        raise ReleaseRequestError("workspace_id must be a safe workspace identifier")
    return value


def _timestamp(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise ReleaseRequestError(f"{label} must be an ISO-8601 timestamp string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ReleaseRequestError(
            f"{label} must be a timezone-aware ISO-8601 timestamp"
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReleaseRequestError(
            f"{label} must be a timezone-aware ISO-8601 timestamp"
        )
    return (
        parsed.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )


def _text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReleaseRequestError(f"{label} must be a non-empty string")
    if any(ord(character) < 32 for character in value):
        raise ReleaseRequestError(f"{label} contains unsupported characters")
    return value


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
        raise ReleaseRequestError("release values must be canonical JSON") from error


@dataclass(frozen=True, slots=True)
class ReleaseRiskAssessment:
    """The structured per-release risk record (architecture-required fields).

    Every note is asserted or computed by the authority per release; the
    residual-risk note can never be empty because de-identification is
    best-effort by design.
    """

    cross_release_disclosure: str
    empirical_reidentification_risk: str
    diagnostic_utility: str
    residual_risk: str
    schema: str = RISK_SCHEMA

    def __post_init__(self) -> None:
        for field in (
            "cross_release_disclosure",
            "empirical_reidentification_risk",
            "diagnostic_utility",
            "residual_risk",
        ):
            object.__setattr__(self, field, _text(getattr(self, field), field))

    @property
    def canonical_document(self) -> dict[str, object]:
        """Return the canonical JSON document for this assessment."""
        return {
            "crossReleaseDisclosure": self.cross_release_disclosure,
            "diagnosticUtility": self.diagnostic_utility,
            "empiricalReidentificationRisk": self.empirical_reidentification_risk,
            "residualRisk": self.residual_risk,
            "schema": self.schema,
        }


@dataclass(frozen=True, slots=True)
class TransformationManifest:
    """The transformation manifest explaining preserved and lost semantics."""

    artifact_sha256: Mapping[str, str]
    removed_counts: Mapping[str, int]
    alias_entries: tuple[Mapping[str, object], ...]
    notes: tuple[str, ...]
    schema: str = MANIFEST_SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(self, "artifact_sha256", dict(self.artifact_sha256))
        object.__setattr__(self, "removed_counts", dict(self.removed_counts))
        for note in self.notes:
            _text(note, "manifest note")
        object.__setattr__(self, "notes", tuple(self.notes))

    @property
    def canonical_document(self) -> dict[str, object]:
        """Return the canonical JSON document for this manifest."""
        return {
            "aliasEntries": [dict(entry) for entry in self.alias_entries],
            "artifactSha256": dict(self.artifact_sha256),
            "notes": list(self.notes),
            "removedCounts": dict(self.removed_counts),
            "schema": self.schema,
        }


@dataclass(frozen=True, slots=True)
class RcaReleaseRequest:
    """The immutable authority-side request for one protected RCA release."""

    workspace_id: str
    submission_id: str
    strategy_id: str
    experiment_id: str
    lineage_id: str
    rca_session_id: str
    evidence_tier: RcaEvidenceTier
    release_gate: ReleaseGate
    release_salt: str
    created_at: str
    risk: ReleaseRiskAssessment
    strategy_source: str
    artifacts: Mapping[str, str]
    symbols: tuple[str, ...] = ()
    venues: tuple[str, ...] = ()
    sectors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "workspace_id", _workspace_id(self.workspace_id))
        object.__setattr__(
            self, "submission_id", _identifier(self.submission_id, "submission_id")
        )
        object.__setattr__(
            self, "strategy_id", _identifier(self.strategy_id, "strategy_id")
        )
        object.__setattr__(
            self, "experiment_id", _identifier(self.experiment_id, "experiment_id")
        )
        object.__setattr__(
            self, "lineage_id", _identifier(self.lineage_id, "lineage_id")
        )
        object.__setattr__(
            self, "rca_session_id", _identifier(self.rca_session_id, "rca_session_id")
        )
        if not isinstance(self.evidence_tier, RcaEvidenceTier):
            raise ReleaseRequestError("evidence_tier must be an RcaEvidenceTier")
        if not isinstance(self.release_gate, ReleaseGate):
            raise ReleaseRequestError("release_gate must be a ReleaseGate")
        object.__setattr__(
            self, "release_salt", _text(self.release_salt, "release_salt")
        )
        object.__setattr__(
            self, "created_at", _timestamp(self.created_at, "created_at")
        )
        if not isinstance(self.risk, ReleaseRiskAssessment):
            raise ReleaseRequestError("risk must be a ReleaseRiskAssessment")
        if not isinstance(self.strategy_source, str):
            raise ReleaseRequestError("strategy_source must be a string")
        if not isinstance(self.artifacts, Mapping):
            raise ReleaseRequestError("artifacts must be a mapping of name to text")
        for name, content in self.artifacts.items():
            if not isinstance(name, str) or not name.strip():
                raise ReleaseRequestError("artifact names must be non-empty strings")
            if not isinstance(content, str):
                raise ReleaseRequestError("artifact contents must be strings")
        object.__setattr__(self, "artifacts", MappingProxyType(dict(self.artifacts)))
        for field in ("symbols", "venues", "sectors"):
            values = getattr(self, field)
            if isinstance(values, str) or not isinstance(values, (tuple, list)):
                raise ReleaseRequestError(f"{field} must be a sequence of strings")
            for value in values:
                _text(value, field)
            object.__setattr__(self, field, tuple(dict.fromkeys(values)))


def _extract_timestamps(artifacts: Mapping[str, str]) -> tuple[str, ...]:
    """Collect distinct ISO timestamps and dates from released artifacts."""
    found: set[str] = set()
    for name in sorted(artifacts):
        text = artifacts[name]
        spans: list[tuple[int, int]] = []
        for match in _ISO_DATETIME.finditer(text):
            found.add(match.group(0))
            spans.append((match.start(), match.end()))
        for match in _ISO_DATE.finditer(text):
            if any(start <= match.start() < end for start, end in spans):
                continue
            found.add(match.group(0))
    return tuple(sorted(found))


def _release_id(request: RcaReleaseRequest) -> str:
    document = {
        "createdAt": request.created_at,
        "experimentId": request.experiment_id,
        "releaseSalt": request.release_salt,
        "rcaSessionId": request.rca_session_id,
        "strategyId": request.strategy_id,
        "submissionId": request.submission_id,
        "workspaceId": request.workspace_id,
    }
    payload = _canonical_json(document).encode("utf-8")
    return "rel-" + hashlib.sha256(_RELEASE_DOMAIN + payload).hexdigest()[:24]


@dataclass(frozen=True, slots=True)
class ProtectedRcaRelease:
    """One composed, bound, and auditable protected RCA release.

    ``identity_mapping`` and the artifact texts are authority-side state:
    the full aliased run leaves this layer only through
    :meth:`release_to_session` for the one named RCA session, and advice
    returns to the main agent only through :meth:`filter_advice`.
    """

    release_id: str
    workspace_id: str
    submission_id: str
    strategy_id: str
    experiment_id: str
    lineage_id: str
    rca_session_id: str
    evidence_tier: RcaEvidenceTier
    plan: AliasPlan
    created_at: str
    artifacts: Mapping[str, str]
    manifest: TransformationManifest
    risk: ReleaseRiskAssessment
    identity_mapping: IdentityMapping
    schema: str = RELEASE_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.evidence_tier, RcaEvidenceTier):
            raise ReleaseRequestError("evidence_tier must be an RcaEvidenceTier")
        if not isinstance(self.plan, AliasPlan):
            raise ReleaseRequestError("plan must be an AliasPlan")
        if not isinstance(self.manifest, TransformationManifest):
            raise ReleaseRequestError("manifest must be a TransformationManifest")
        if not isinstance(self.risk, ReleaseRiskAssessment):
            raise ReleaseRequestError("risk must be a ReleaseRiskAssessment")
        if not isinstance(self.identity_mapping, IdentityMapping):
            raise ReleaseRequestError("identity_mapping must be an IdentityMapping")
        if not isinstance(self.artifacts, Mapping):
            raise ReleaseRequestError("artifacts must be a mapping")
        object.__setattr__(self, "artifacts", MappingProxyType(dict(self.artifacts)))

    @classmethod
    def create(cls, request: RcaReleaseRequest) -> ProtectedRcaRelease:
        """Compose the release; every failure mode is fail-closed."""
        if not isinstance(request, RcaReleaseRequest):
            raise ReleaseRequestError("request must be an RcaReleaseRequest")
        if request.evidence_tier is RcaEvidenceTier.NO_RELEASE:
            raise ReleaseBlockedError(
                (
                    "requested evidence tier 'no-release' does not authorize "
                    "any RCA release",
                )
            )
        plan = build_aliasing(request.evidence_tier, request.release_gate)
        known_identifiers = (
            request.workspace_id,
            request.submission_id,
            request.strategy_id,
            request.experiment_id,
            request.lineage_id,
            *request.symbols,
            *request.venues,
            *request.sectors,
        )
        findings = screen_source(
            request.strategy_source, known_identifiers=known_identifiers
        )
        if findings:
            raise ReleaseBlockedError(
                tuple(
                    f"strategy source reveals identity: {finding.describe()}"
                    for finding in findings
                )
            )
        values: dict[IdentitySurface, tuple[str, ...]] = {
            IdentitySurface.STRATEGY_ID: (request.strategy_id,),
            IdentitySurface.EXPERIMENT_ID: (request.experiment_id,),
            IdentitySurface.LINEAGE_ID: (request.lineage_id,),
        }
        if plan.aliases_surface(IdentitySurface.SYMBOL):
            values[IdentitySurface.SYMBOL] = request.symbols
        if plan.aliases_surface(IdentitySurface.VENUE):
            values[IdentitySurface.VENUE] = request.venues
        if plan.aliases_surface(IdentitySurface.SECTOR):
            values[IdentitySurface.SECTOR] = request.sectors
        if plan.aliases_surface(IdentitySurface.TIMESTAMP):
            values[IdentitySurface.TIMESTAMP] = _extract_timestamps(request.artifacts)
        release_id = _release_id(request)
        mapping = IdentityMapping.build(
            release_id=release_id,
            salt=request.release_salt,
            plan=plan,
            values=values,
        )
        allowlist = (
            _MASKING_ALLOWLIST
            if plan.strategy is AliasStrategyKind.IDENTITY_MASKING
            else _HARDENED_ALLOWLIST
        )
        artifacts: dict[str, str] = {}
        digests: dict[str, str] = {}
        counts: dict[str, int] = {}
        for name in sorted(request.artifacts):
            aliased = mapping.apply_to_text(request.artifacts[name])
            sanitized, removed = sanitize_output(aliased, allowlist)
            artifacts[name] = sanitized
            digests[name] = hashlib.sha256(sanitized.encode("utf-8")).hexdigest()
            for finding in removed:
                counts[finding.kind.value] = counts.get(finding.kind.value, 0) + 1
        manifest = TransformationManifest(
            artifact_sha256=digests,
            removed_counts=counts,
            alias_entries=mapping.alias_manifest(),
            notes=_manifest_notes(plan, allowlist),
        )
        return cls(
            release_id=release_id,
            workspace_id=request.workspace_id,
            submission_id=request.submission_id,
            strategy_id=request.strategy_id,
            experiment_id=request.experiment_id,
            lineage_id=request.lineage_id,
            rca_session_id=request.rca_session_id,
            evidence_tier=request.evidence_tier,
            plan=plan,
            created_at=request.created_at,
            artifacts=artifacts,
            manifest=manifest,
            risk=request.risk,
            identity_mapping=mapping,
        )

    @property
    def canonical_document(self) -> dict[str, object]:
        """Return the audit document; it never contains the identity mapping."""
        return {
            "aliasStrategy": self.plan.strategy.value,
            "aliasedSurfaces": [surface.value for surface in self.plan.surfaces],
            "createdAt": self.created_at,
            "evidenceTier": self.plan.evidence_tier.value,
            "experimentId": self.experiment_id,
            "lineageId": self.lineage_id,
            "manifest": self.manifest.canonical_document,
            "policyVersion": self.plan.policy_version,
            "rcaSessionId": self.rca_session_id,
            "releaseId": self.release_id,
            "risk": self.risk.canonical_document,
            "schema": self.schema,
            "strategyId": self.strategy_id,
            "submissionId": self.submission_id,
            "workspaceId": self.workspace_id,
        }

    @property
    def content_sha256(self) -> str:
        """Return the content digest of the canonical release record."""
        return hashlib.sha256(
            _canonical_json(self.canonical_document).encode("utf-8")
        ).hexdigest()

    def release_to_session(self, session_id: str) -> Mapping[str, object]:
        """Release the full aliased run to the one named RCA session only."""
        if not isinstance(session_id, str) or not session_id:
            raise ReleaseRequestError("session_id must be a non-empty string")
        if session_id != self.rca_session_id:
            raise ReleaseBlockedError(
                (
                    f"RCA release {self.release_id!r} is bound to a different "
                    "RCA session",
                )
            )
        return MappingProxyType(
            {
                "artifacts": MappingProxyType(dict(self.artifacts)),
                "manifest": self.manifest.canonical_document,
                "releaseId": self.release_id,
                "rcaSessionId": self.rca_session_id,
            }
        )

    def filter_advice(self, advice_text: str, *, from_session: str) -> str:
        """Apply the deterministic egress filter to RCA session advice.

        Only the bound session may emit advice through this release.  The
        filter first removes every non-allowlisted side channel with the
        strictest allowlist, then substitutes release aliases with neutral
        per-surface ordinal tokens, and finally fails closed if any raw
        alias or raw real identifier survives.
        """
        if not isinstance(advice_text, str):
            raise ReleaseRequestError("advice_text must be a string")
        if not isinstance(from_session, str) or not from_session:
            raise ReleaseRequestError("from_session must be a non-empty string")
        if from_session != self.rca_session_id:
            raise ReleaseBlockedError(
                ("advice egress is bound to a different RCA session",)
            )
        scrubbed, _ = sanitize_output(advice_text, _HARDENED_ALLOWLIST)
        egress = self.identity_mapping.replace_aliases_for_egress(scrubbed)
        assert_no_raw_identifiers(egress, self.identity_mapping)
        return egress


def _manifest_notes(
    plan: AliasPlan, allowlist: frozenset[OutputSurface]
) -> tuple[str, ...]:
    notes = [
        "identity surfaces replaced with per-release opaque tokens; the "
        "real-to-alias mapping stays in the release authority layer",
    ]
    if plan.aliases_surface(IdentitySurface.TIMESTAMP):
        notes.append(
            "timestamps and dates replaced with interval counters; absolute "
            "calendar values and sub-minute spacing are suppressed"
        )
    if OutputSurface.TIMING in allowlist:
        notes.append("timing side channels transformed to order-of-magnitude buckets")
    else:
        notes.append("timing side channels removed")
    if plan.fingerprint_suppression:
        notes.append(
            "hardened de-identification suppresses calendar, scale, and "
            "correlation fingerprints at the text layer"
        )
    notes.append("de-identification is best-effort; no information-theoretic anonymity")
    return tuple(notes)


__all__ = [
    "MANIFEST_SCHEMA",
    "RELEASE_SCHEMA",
    "RISK_SCHEMA",
    "ProtectedRcaRelease",
    "ReleaseBlockedError",
    "ReleaseRequestError",
    "ReleaseRiskAssessment",
    "RcaReleaseRequest",
    "TransformationManifest",
]
