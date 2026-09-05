"""Immutable submission contracts for the protected-test authority boundary.

Thesis is the *client* of the protected-test authority.  The authority itself
is a non-LLM service outside the Thesis reasoning path and is never
implemented here: this package implements only the client-side protocol
records needed to request eligibility for a protected test, to carry the
signed facts the authority returns, and to mirror those facts read-only.

A submission is the immutable request for eligibility.  It carries the exact
experiment-contract digest, the exact strategy-bundle and configuration
fingerprints, the strategy lineage with its signed ancestry, the declared
prior test-exposure identifiers, and the model-registry identities of the
generator, selector, and proposed RCA roles.  The authority never trusts
lineage IDs as proof of novelty; it derives reuse from the exact bundle
fingerprints and verifies the signed ancestry itself.

The model-registry identity surface here is deliberately minimal:
``src/thesis/registry`` (model attestation) owns the full attestation record.
Until that capability lands, this module stores registry identities as plain
frozen values and treats them as opaque to the protocol.
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
from typing import Any

_SCHEMA = "fca-thesis-protected-submission-v1"
_HASH_DOMAIN = b"fca:thesis-protected-submission:v1\0"

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
_WORKSPACE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class ProtectedTestError(RuntimeError):
    """Base class for protected-test client failures."""


class SubmissionError(ProtectedTestError):
    """A protected-test submission would be incomplete or inconsistent."""


class AuthorityProtocolError(ProtectedTestError):
    """The authority response or transport payload violates the protocol."""


class SignatureVerificationError(ProtectedTestError):
    """A signed fact failed signature verification."""


class FactConflictError(ProtectedTestError):
    """A fact identifier was reused for different content."""


class RegistryRole(StrEnum):
    """The protected-test role a model registry identity is declared for."""

    GENERATOR = "generator"
    SELECTOR = "selector"
    RCA = "rca"


class SealedStatusKind(StrEnum):
    """The canonical kinds of signed sealed-status facts the authority emits."""

    ELIGIBILITY_GRANTED = "eligibility-granted"
    ELIGIBILITY_DENIED = "eligibility-denied"
    ELIGIBILITY_EXHAUSTED = "eligibility-exhausted"
    WINDOW_EXPOSED = "window-exposed"
    WINDOW_REUSED = "window-reused"
    INFLUENCE_RECORDED = "influence-recorded"
    UNKNOWN_PRIOR_EXPOSURE = "unknown-prior-exposure"
    SEALED_STATUS = "sealed-status"


def _identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise SubmissionError(f"{label} must be a safe identifier")
    return value


def _workspace_id(value: str) -> str:
    if not isinstance(value, str) or _WORKSPACE_ID.fullmatch(value) is None:
        raise SubmissionError("workspace_id must be a safe workspace identifier")
    return value


def _digest(value: str, label: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise SubmissionError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _timestamp(value: str, label: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise SubmissionError(
            f"{label} must be a timezone-aware ISO-8601 timestamp"
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SubmissionError(f"{label} must be a timezone-aware ISO-8601 timestamp")
    return (
        parsed.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )


def _jsonable(value: object) -> Any:
    if value is None or isinstance(value, str | bool | int | float):
        return value
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise SubmissionError("submission mappings must have string keys")
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_jsonable(item) for item in value]
    raise SubmissionError("submission values must be JSON-shaped")


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
        raise SubmissionError("submission values must be canonical JSON") from error


def _freeze_mapping(value: Mapping[str, object], label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise SubmissionError(f"{label} must be a JSON-shaped mapping")
    frozen = MappingProxyType(dict(value))
    _canonical_json(_jsonable(dict(frozen)))
    return frozen


@dataclass(frozen=True, slots=True)
class ModelRegistryIdentity:
    """A minimal model-registry identity as carried in a submission.

    ``src/thesis/registry`` owns the full signed attestation record; this
    module stores only the identity surface the authority protocol needs.
    """

    role: RegistryRole
    registry_id: str
    provider: str
    model_id: str
    snapshot_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.role, RegistryRole):
            raise SubmissionError("role must be a RegistryRole")
        object.__setattr__(
            self, "registry_id", _identifier(self.registry_id, "registry_id")
        )
        object.__setattr__(self, "provider", _identifier(self.provider, "provider"))
        object.__setattr__(self, "model_id", _identifier(self.model_id, "model_id"))
        object.__setattr__(
            self, "snapshot_sha256", _digest(self.snapshot_sha256, "snapshot_sha256")
        )


@dataclass(frozen=True, slots=True)
class LineageAncestry:
    """One signed ancestry link retained across workspace export/import."""

    parent_lineage_id: str
    bundle_sha256: str
    signature: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "parent_lineage_id",
            _identifier(self.parent_lineage_id, "parent_lineage_id"),
        )
        object.__setattr__(
            self, "bundle_sha256", _digest(self.bundle_sha256, "bundle_sha256")
        )
        if not isinstance(self.signature, str) or not self.signature.strip():
            raise SubmissionError("ancestry signature must be a non-empty string")
        object.__setattr__(self, "signature", self.signature)


@dataclass(frozen=True, slots=True)
class ProtectedTestSubmission:
    """The immutable client submission for protected-test eligibility.

    Every identity field is locked at creation and the whole submission is
    content-addressed by :attr:`content_sha256`.  Changing any field creates
    a new submission instead of mutating the original request.
    """

    submission_id: str
    workspace_id: str
    experiment_id: str
    lineage_id: str
    contract_sha256: str
    strategy_bundle_sha256: str
    configuration_sha256: str
    submitted_at: str
    lineage_ancestry: tuple[LineageAncestry, ...] = ()
    exposure_ids: tuple[str, ...] = ()
    model_registry: tuple[ModelRegistryIdentity, ...] = ()
    requested_diagnostics: tuple[str, ...] = ()
    schema: str = _SCHEMA

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "submission_id", _identifier(self.submission_id, "submission_id")
        )
        object.__setattr__(self, "workspace_id", _workspace_id(self.workspace_id))
        object.__setattr__(
            self, "experiment_id", _identifier(self.experiment_id, "experiment_id")
        )
        object.__setattr__(
            self, "lineage_id", _identifier(self.lineage_id, "lineage_id")
        )
        object.__setattr__(
            self, "contract_sha256", _digest(self.contract_sha256, "contract_sha256")
        )
        object.__setattr__(
            self,
            "strategy_bundle_sha256",
            _digest(self.strategy_bundle_sha256, "strategy_bundle_sha256"),
        )
        object.__setattr__(
            self,
            "configuration_sha256",
            _digest(self.configuration_sha256, "configuration_sha256"),
        )
        object.__setattr__(
            self, "submitted_at", _timestamp(self.submitted_at, "submitted_at")
        )
        ancestry = tuple(self.lineage_ancestry)
        for link in ancestry:
            if not isinstance(link, LineageAncestry):
                raise SubmissionError(
                    "lineage ancestry must contain LineageAncestry values"
                )
        object.__setattr__(self, "lineage_ancestry", ancestry)
        for label, values in (
            ("exposure_ids", self.exposure_ids),
            ("requested_diagnostics", self.requested_diagnostics),
        ):
            if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
                raise SubmissionError(f"{label} must be a sequence of identifiers")
            for value in values:
                _identifier(value, label)
        registry = tuple(self.model_registry)
        for identity in registry:
            if not isinstance(identity, ModelRegistryIdentity):
                raise SubmissionError(
                    "model registry must contain ModelRegistryIdentity values"
                )
        object.__setattr__(self, "model_registry", registry)

    @property
    def canonical_document(self) -> dict[str, object]:
        """Return the canonical JSON document that the submission hash covers."""
        return {
            "configurationSha256": self.configuration_sha256,
            "contractSha256": self.contract_sha256,
            "exposureIds": list(self.exposure_ids),
            "experimentId": self.experiment_id,
            "lineageAncestry": [
                {
                    "bundleSha256": link.bundle_sha256,
                    "parentLineageId": link.parent_lineage_id,
                    "signature": link.signature,
                }
                for link in self.lineage_ancestry
            ],
            "lineageId": self.lineage_id,
            "modelRegistry": [
                {
                    "modelId": identity.model_id,
                    "provider": identity.provider,
                    "registryId": identity.registry_id,
                    "role": identity.role.value,
                    "snapshotSha256": identity.snapshot_sha256,
                }
                for identity in self.model_registry
            ],
            "requestedDiagnostics": list(self.requested_diagnostics),
            "schema": self.schema,
            "strategyBundleSha256": self.strategy_bundle_sha256,
            "submissionId": self.submission_id,
            "submittedAt": self.submitted_at,
            "workspaceId": self.workspace_id,
        }

    @property
    def canonical_bytes(self) -> bytes:
        """Return the deterministic UTF-8 submission representation."""
        return _canonical_json(self.canonical_document).encode("utf-8")

    @property
    def content_sha256(self) -> str:
        """Return the domain-separated content address of this submission."""
        return hashlib.sha256(_HASH_DOMAIN + self.canonical_bytes).hexdigest()


__all__ = [
    "AuthorityProtocolError",
    "FactConflictError",
    "LineageAncestry",
    "ModelRegistryIdentity",
    "ProtectedTestError",
    "ProtectedTestSubmission",
    "RegistryRole",
    "SealedStatusKind",
    "SignatureVerificationError",
    "SubmissionError",
]
