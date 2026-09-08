"""RCA handoff bundle assembly and fail-closed response validation.

A :class:`RcaHandoffBundle` is the one assembly step that hands a protected
result to a named RCA subagent session exactly as the release policy
approves it.  It composes the authority-side release layer
(:mod:`thesis.protected`) with the policy-approved context the architecture
requires: the full aliased run, the original strategy source as released,
the aliased experiment contract and strategy/harness configuration,
dependency versions, benchmark summary, costs, and the data-quality
manifest, together with the release's transformation manifest.  Nothing is
added and nothing is dropped; every payload entry is content-addressed.

The RCA response schema admits only the architecture's diagnosis outputs:
candidate explanations, supporting and conflicting evidence, uncertainty,
alternative explanations, and proposed discriminating tests.  Validation is
structural and fail-closed: any field outside the schema - including fields
that would carry strategy edits, parameter changes, or promotion or
activation decisions - is rejected with an explicit violation list before
anything is recorded.
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

from thesis.protected.aliasing import AliasPlan, AliasStrategyKind
from thesis.protected.contracts import ProtectedTestError
from thesis.protected.flow_checks import (
    OutputSurface,
    sanitize_output,
    screen_source,
)
from thesis.protected.release import ProtectedRcaRelease

BUNDLE_SCHEMA = "fca-thesis-rca-handoff-bundle-v1"
RESPONSE_SCHEMA = "fca-thesis-rca-response-v1"

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
_VALUE_GUARD = r"[A-Za-z0-9_@/:~-]"

_SECTION_ALIASED_RUN = "aliasedRun"
_SECTION_BENCHMARK = "benchmark"
_SECTION_CONTRACT_SNAPSHOT = "contractSnapshot"
_SECTION_COSTS = "costs"
_SECTION_DATA_QUALITY_MANIFEST = "dataQualityManifest"
_SECTION_DEPENDENCY_VERSIONS = "dependencyVersions"
_SECTION_STRATEGY_SOURCE = "strategySource"
_SECTION_TRANSFORMATION_MANIFEST = "transformationManifest"

#: The exact payload sections of a handoff bundle; the assembly adds no
#: section and drops none of them.
BUNDLE_PAYLOAD_SECTIONS: tuple[str, ...] = (
    _SECTION_ALIASED_RUN,
    _SECTION_BENCHMARK,
    _SECTION_CONTRACT_SNAPSHOT,
    _SECTION_COSTS,
    _SECTION_DATA_QUALITY_MANIFEST,
    _SECTION_DEPENDENCY_VERSIONS,
    _SECTION_STRATEGY_SOURCE,
    _SECTION_TRANSFORMATION_MANIFEST,
)

_SOURCE_ENTRY = "source"
_MANIFEST_ENTRY = "manifest"


class RcaHandoffError(ProtectedTestError):
    """A handoff bundle would be incomplete, inconsistent, or unsafe."""


class RcaResponseError(RcaHandoffError):
    """An RCA response violates the response schema; rejection is fail-closed.

    The violations name rejected fields and structural problems without
    echoing the rejected content itself.
    """

    def __init__(self, violations: Sequence[str]) -> None:
        self.violations = tuple(violations)
        if not self.violations:
            raise RcaHandoffError(
                "a rejected RCA response requires at least one violation"
            )
        super().__init__("RCA response rejected: " + "; ".join(self.violations))


class RcaResponseField(StrEnum):
    """The only fields the RCA response schema admits."""

    CANDIDATE_EXPLANATIONS = "candidateExplanations"
    SUPPORTING_EVIDENCE = "supportingEvidence"
    CONFLICTING_EVIDENCE = "conflictingEvidence"
    UNCERTAINTY = "uncertainty"
    ALTERNATIVE_EXPLANATIONS = "alternativeExplanations"
    DISCRIMINATING_TESTS = "discriminatingTests"


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
        raise RcaHandoffError("handoff values must be canonical JSON") from error


def _jsonable(value: object) -> object:
    if value is None or isinstance(value, str | bool | int | float):
        return value
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise RcaHandoffError("handoff mappings must have string keys")
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_jsonable(item) for item in value]
    raise RcaHandoffError("handoff values must be JSON-shaped")


def _identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise RcaHandoffError(f"{label} must be a safe identifier")
    return value


def _timestamp(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise RcaHandoffError(f"{label} must be an ISO-8601 timestamp string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise RcaHandoffError(
            f"{label} must be a timezone-aware ISO-8601 timestamp"
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RcaHandoffError(f"{label} must be a timezone-aware ISO-8601 timestamp")
    return (
        parsed.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )


def _metadata_allowlist(plan: AliasPlan) -> tuple[OutputSurface, ...]:
    """Mirror the release's own side-channel choice for supplied metadata."""
    if plan.strategy is AliasStrategyKind.IDENTITY_MASKING:
        return (OutputSurface.TIMING,)
    return ()


def _aliased_text(
    text: str,
    label: str,
    release: ProtectedRcaRelease,
    allowlist: Sequence[OutputSurface],
) -> str:
    """Alias, sanitize, and verify one supplied metadata text.

    Session-bound payload text may carry the release's alias tokens, but it
    must never carry a raw real identifier: every real identity value, the
    workspace id, and the submission id must have been replaced or absent.
    """
    if not isinstance(text, str) or not text:
        raise RcaHandoffError(f"{label} must be non-empty text")
    mapping = release.identity_mapping
    sanitized, _ = sanitize_output(mapping.apply_to_text(text), allowlist)
    known: tuple[str, ...] = (
        release.workspace_id,
        release.submission_id,
        *mapping.real_values(),
    )
    for real in dict.fromkeys(known):
        pattern = re.compile(f"(?<!{_VALUE_GUARD}){re.escape(real)}(?!{_VALUE_GUARD})")
        if pattern.search(sanitized) is not None:
            raise RcaHandoffError(
                f"{label} still carries a real identifier after aliasing"
            )
    return sanitized


def _named_text_section(
    values: Mapping[str, str],
    label: str,
    release: ProtectedRcaRelease,
    allowlist: Sequence[OutputSurface],
) -> dict[str, str]:
    if not isinstance(values, Mapping):
        raise RcaHandoffError(f"{label} must be a mapping of names to text")
    if not values:
        raise RcaHandoffError(f"{label} must carry at least one entry")
    return {
        _identifier(name, f"{label} entry name"): _aliased_text(
            text, f"{label} entry {name!r}", release, allowlist
        )
        for name, text in values.items()
    }


def _json_section(
    values: Mapping[str, object],
    label: str,
    release: ProtectedRcaRelease,
    allowlist: Sequence[OutputSurface],
) -> dict[str, str]:
    if not isinstance(values, Mapping):
        raise RcaHandoffError(f"{label} must be a JSON-shaped mapping")
    if not values:
        raise RcaHandoffError(f"{label} must carry at least one entry")
    return {
        _identifier(name, f"{label} entry name"): _aliased_text(
            _canonical_json(_jsonable(value)),
            f"{label} entry {name!r}",
            release,
            allowlist,
        )
        for name, value in values.items()
    }


def _released_artifacts(release: ProtectedRcaRelease) -> dict[str, str]:
    """Take the full aliased run through the session-bound release path."""
    released = release.release_to_session(release.rca_session_id)
    artifacts = released["artifacts"]
    if not isinstance(artifacts, Mapping):
        raise RcaHandoffError("the released payload artifacts must be a mapping")
    texts: dict[str, str] = {}
    for name, content in artifacts.items():
        if not isinstance(name, str) or not isinstance(content, str):
            raise RcaHandoffError("released artifacts must map safe names to text")
        if not content:
            raise RcaHandoffError(
                f"released artifact {name!r} must carry non-empty text"
            )
        texts[name] = content
    if not texts:
        raise RcaHandoffError("the aliased run requires at least one artifact")
    return texts


def _screened_source(release: ProtectedRcaRelease, strategy_source: str) -> str:
    """Screen the original source exactly as the release authority does.

    The RCA context receives the original strategy source, never a rewrite.
    Identity-revealing source fails the handoff closed, mirroring the
    release layer's own source gate.
    """
    if not isinstance(strategy_source, str) or not strategy_source:
        raise RcaHandoffError("strategy_source must be non-empty source text")
    known: tuple[str, ...] = (
        release.workspace_id,
        release.submission_id,
        release.strategy_id,
        release.experiment_id,
        release.lineage_id,
        *release.identity_mapping.real_values(),
    )
    findings = screen_source(strategy_source, known_identifiers=known)
    if findings:
        raise RcaHandoffError(
            "strategy source failed release screening: "
            + "; ".join(finding.describe() for finding in findings)
        )
    return strategy_source


@dataclass(frozen=True, slots=True)
class RcaResponseVerdict:
    """The recorded outcome of validating one RCA response.

    An accepted verdict carries the normalized response together with the
    release linkage and content digests the egress and revision layers
    need; a rejected verdict carries only the structural violations.
    """

    accepted: bool
    violations: tuple[str, ...]
    response: Mapping[str, tuple[str, ...]] | None
    release_id: str = ""
    rca_session_id: str = ""
    evidence_tier: str = ""
    release_content_sha256: str = ""
    response_sha256: str = ""


@dataclass(frozen=True, slots=True)
class RcaHandoffBundle:
    """The policy-approved payload set released to one RCA session.

    ``payload`` maps each of :data:`BUNDLE_PAYLOAD_SECTIONS` to its named
    text entries; ``payload_sha256`` content-addresses every entry (and the
    transformation manifest) by ``section/name``.  The identity mapping
    stays in the release object and is never part of the canonical
    document.
    """

    release: ProtectedRcaRelease
    payload: Mapping[str, Mapping[str, str]]
    payload_sha256: Mapping[str, str]
    recorded: RcaResponseVerdict | None = None
    schema: str = BUNDLE_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.release, ProtectedRcaRelease):
            raise RcaHandoffError("release must be a ProtectedRcaRelease")
        if not isinstance(self.payload, Mapping):
            raise RcaHandoffError("payload must map sections to text entries")
        object.__setattr__(
            self,
            "payload",
            MappingProxyType(
                {
                    section: MappingProxyType(dict(items))
                    for section, items in self.payload.items()
                }
            ),
        )
        if not isinstance(self.payload_sha256, Mapping):
            raise RcaHandoffError("payload_sha256 must map entry keys to digests")
        object.__setattr__(
            self, "payload_sha256", MappingProxyType(dict(self.payload_sha256))
        )
        if self.recorded is not None and not isinstance(
            self.recorded, RcaResponseVerdict
        ):
            raise RcaHandoffError("recorded must be an RcaResponseVerdict")

    @property
    def canonical_document(self) -> dict[str, object]:
        """Return the audit document for the assembled bundle."""
        release = self.release
        return {
            "aliasStrategy": release.plan.strategy.value,
            "evidenceTier": release.plan.evidence_tier.value,
            "payload": {
                section: dict(items) for section, items in sorted(self.payload.items())
            },
            "payloadSha256": dict(self.payload_sha256),
            "policyVersion": release.plan.policy_version,
            "releaseContentSha256": release.content_sha256,
            "releaseId": release.release_id,
            "rcaSessionId": release.rca_session_id,
            "schema": self.schema,
        }

    @property
    def content_sha256(self) -> str:
        """Return the content digest of the canonical bundle document."""
        return hashlib.sha256(
            _canonical_json(self.canonical_document).encode("utf-8")
        ).hexdigest()


def assemble_handoff(
    release: ProtectedRcaRelease,
    *,
    strategy_source: str,
    contract_snapshot: Mapping[str, str],
    dependency_versions: Mapping[str, str],
    benchmark: Mapping[str, object],
    costs: Mapping[str, object],
    data_quality_manifest: Mapping[str, object],
) -> RcaHandoffBundle:
    """Assemble the handoff bundle for one bound protected release.

    The bundle carries exactly the policy-approved payload: the full
    aliased run through the session-bound release path, the original
    strategy source as released, the aliased contract/configuration and
    remaining supplied metadata, and the release's transformation manifest.
    Supplied metadata is aliased and sanitized with the release's own
    strategy choice; every entry is content-addressed and the aliased run
    is cross-checked against the transformation manifest digests.  All
    failure modes fail closed.
    """
    if not isinstance(release, ProtectedRcaRelease):
        raise RcaHandoffError("release must be a ProtectedRcaRelease")
    allowlist = _metadata_allowlist(release.plan)
    payload: dict[str, dict[str, str]] = {
        _SECTION_ALIASED_RUN: _released_artifacts(release),
        _SECTION_BENCHMARK: _json_section(benchmark, "benchmark", release, allowlist),
        _SECTION_CONTRACT_SNAPSHOT: _named_text_section(
            contract_snapshot, "contract_snapshot", release, allowlist
        ),
        _SECTION_COSTS: _json_section(costs, "costs", release, allowlist),
        _SECTION_DATA_QUALITY_MANIFEST: _json_section(
            data_quality_manifest, "data_quality_manifest", release, allowlist
        ),
        _SECTION_DEPENDENCY_VERSIONS: _named_text_section(
            dependency_versions, "dependency_versions", release, allowlist
        ),
        _SECTION_STRATEGY_SOURCE: {
            _SOURCE_ENTRY: _screened_source(release, strategy_source)
        },
        _SECTION_TRANSFORMATION_MANIFEST: {
            _MANIFEST_ENTRY: _canonical_json(release.manifest.canonical_document)
        },
    }
    sections = {
        section: MappingProxyType(dict(sorted(payload[section].items())))
        for section in BUNDLE_PAYLOAD_SECTIONS
    }
    digests: dict[str, str] = {}
    for section in BUNDLE_PAYLOAD_SECTIONS:
        for name in sorted(sections[section]):
            digests[f"{section}/{name}"] = hashlib.sha256(
                sections[section][name].encode("utf-8")
            ).hexdigest()
    for name, expected in sorted(release.manifest.artifact_sha256.items()):
        actual = digests[f"{_SECTION_ALIASED_RUN}/{name}"]
        if actual != expected:
            raise RcaHandoffError(
                f"aliased run artifact {name!r} does not match the "
                "release transformation manifest"
            )
    return RcaHandoffBundle(
        release=release,
        payload=MappingProxyType(sections),
        payload_sha256=MappingProxyType(digests),
    )


def validate_rca_response(response: Mapping[str, object]) -> dict[str, tuple[str, ...]]:
    """Validate one RCA response against the response schema, fail-closed.

    The admitted key set is exactly the six :class:`RcaResponseField`
    categories plus an optional correct ``schema`` marker.  Missing fields,
    unknown fields - including any field that would carry strategy edits,
    parameter changes, or promotion or activation decisions - and
    malformed values are rejected as an explicit violation list.
    """
    if not isinstance(response, Mapping):
        raise RcaResponseError(("the RCA response must be a JSON mapping",))
    violations: list[str] = []
    schema = response.get("schema")
    if schema is not None and schema != RESPONSE_SCHEMA:
        violations.append(
            f"field 'schema' must be the RCA response schema {RESPONSE_SCHEMA!r}"
        )
    admitted = {field.value for field in RcaResponseField} | {"schema"}
    unknown: list[str] = []
    for key in response:
        if not isinstance(key, str):
            unknown.append("response keys must be strings")
        elif key not in admitted:
            unknown.append(f"field {key!r} is outside the RCA response schema")
    violations.extend(sorted(unknown))
    normalized: dict[str, tuple[str, ...]] = {}
    for field in RcaResponseField:
        value = response.get(field.value)
        if value is None:
            violations.append(f"missing field {field.value!r}")
            continue
        if not isinstance(value, list):
            violations.append(f"field {field.value!r} must be a list of strings")
            continue
        items: list[str] = []
        well_formed = True
        for index, item in enumerate(value):
            if (
                not isinstance(item, str)
                or not item.strip()
                or any(ord(character) < 32 for character in item)
            ):
                violations.append(
                    f"field {field.value!r} item {index} must be a non-empty "
                    "string without control characters"
                )
                well_formed = False
                continue
            items.append(item)
        if well_formed:
            normalized[field.value] = tuple(items)
    if violations:
        raise RcaResponseError(violations)
    return normalized


def _response_digest(normalized: Mapping[str, tuple[str, ...]]) -> str:
    document = {field.value: list(items) for field, items in _ordered(normalized)}
    return hashlib.sha256(_canonical_json(document).encode("utf-8")).hexdigest()


def _ordered(
    normalized: Mapping[str, tuple[str, ...]],
) -> list[tuple[RcaResponseField, tuple[str, ...]]]:
    ordered: list[tuple[RcaResponseField, tuple[str, ...]]] = []
    for field in RcaResponseField:
        items = normalized.get(field.value)
        if items is not None:
            ordered.append((field, items))
    return ordered


def record_response(
    bundle: RcaHandoffBundle, response: Mapping[str, object]
) -> RcaResponseVerdict:
    """Record one RCA response against its handoff bundle.

    The response is validated against the response schema before anything
    is recorded.  A schema violation returns a rejected verdict carrying
    the violation list and records nothing; an accepted response is
    recorded exactly once together with the release linkage and content
    digests.
    """
    if not isinstance(bundle, RcaHandoffBundle):
        raise RcaHandoffError("bundle must be an RcaHandoffBundle")
    if bundle.recorded is not None and bundle.recorded.accepted:
        raise RcaHandoffError(
            "an accepted RCA response is already recorded for this bundle"
        )
    try:
        normalized = validate_rca_response(response)
    except RcaResponseError as error:
        return RcaResponseVerdict(
            accepted=False, violations=error.violations, response=None
        )
    release = bundle.release
    verdict = RcaResponseVerdict(
        accepted=True,
        violations=(),
        response=MappingProxyType(dict(normalized)),
        release_id=release.release_id,
        rca_session_id=release.rca_session_id,
        evidence_tier=release.plan.evidence_tier.value,
        release_content_sha256=release.content_sha256,
        response_sha256=_response_digest(normalized),
    )
    object.__setattr__(bundle, "recorded", verdict)
    return verdict


__all__ = [
    "BUNDLE_PAYLOAD_SECTIONS",
    "BUNDLE_SCHEMA",
    "RESPONSE_SCHEMA",
    "RcaHandoffBundle",
    "RcaHandoffError",
    "RcaResponseError",
    "RcaResponseField",
    "RcaResponseVerdict",
    "assemble_handoff",
    "record_response",
    "validate_rca_response",
]
