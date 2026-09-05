"""Immutable model registry records and workflow-role restrictions.

A registry record pins the exact provider/model deployment identity that a
Thesis model role may serve: provider, model identifier, deployment name,
model version, capability policy, context window, and the identity of the
cutoff attestation the deployment must carry.  Records are versioned:
registering new content under an existing record ID creates a new version and
revocation is a new version with ``REVOKED`` status, never an in-place edit.

Role restrictions record which Thesis agent model roles a deployment may
serve.  The permitted roles are exactly the roles of the agent model-role
policy (:data:`thesis.agent.router.Role`), so a registry record and a
:class:`thesis.agent.router.RolePolicy` share one role vocabulary.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import cast, get_args

from thesis.agent.router import Role

_SCHEMA = "fca-thesis-model-registry-record-v1"
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
_AGENT_ROLES: frozenset[str] = frozenset(get_args(Role))


class RegistryError(RuntimeError):
    """Base class for model registry failures."""


class RoleRestrictionError(RegistryError):
    """A deployment may not serve the requested Thesis model role."""


class ModelCapability(StrEnum):
    """One declared deployment capability the registry can grant or deny."""

    TEXT_GENERATION = "text_generation"
    WEB_BROWSING = "web_browsing"
    WORKSPACE_HISTORY = "workspace_history"
    REAL_ENTITY_MAPPINGS = "real_entity_mappings"
    PRIOR_PROTECTED_OUTPUTS = "prior_protected_outputs"
    STRATEGY_GENERATION = "strategy_generation"
    CANDIDATE_SELECTION = "candidate_selection"
    CANDIDATE_RANKING = "candidate_ranking"
    STRATEGY_EDITING = "strategy_editing"
    ACTIVATION_REQUEST = "activation_request"


class DeploymentStatus(StrEnum):
    """The registry lifecycle state of one deployment identity."""

    ACTIVE = "active"
    REVOKED = "revoked"


def _identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise RegistryError(f"{label} must be a portable identifier")
    return value


def _timestamp(value: str, label: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise RegistryError(
            f"{label} must be a timezone-aware ISO-8601 timestamp"
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RegistryError(f"{label} must be a timezone-aware ISO-8601 timestamp")
    return (
        parsed.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )


def _role_set(value: object, label: str) -> frozenset[Role]:
    if isinstance(value, str | bytes) or not isinstance(
        value, set | frozenset | tuple | list
    ):
        raise RegistryError(f"{label} must be a collection of Thesis roles")
    items = tuple(value)
    if not items:
        raise RegistryError(f"{label} must name at least one Thesis role")
    for role in items:
        if not isinstance(role, str) or role not in _AGENT_ROLES:
            raise RegistryError(f"{label} contains an unsupported Thesis role")
    return frozenset(cast("tuple[Role, ...]", items))


def _capability_set(value: object, label: str) -> frozenset[ModelCapability]:
    if isinstance(value, str | bytes) or not isinstance(
        value, set | frozenset | tuple | list
    ):
        raise RegistryError(f"{label} must be a collection of model capabilities")
    items = tuple(value)
    if not items:
        raise RegistryError(f"{label} must declare at least one capability")
    for capability in items:
        if not isinstance(capability, ModelCapability):
            raise RegistryError(f"{label} contains an unsupported capability")
    return frozenset(cast("tuple[ModelCapability, ...]", items))


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


@dataclass(frozen=True, slots=True)
class ModelRegistryRecord:
    """The exact, versioned identity of one provider model deployment."""

    record_id: str
    version: int
    provider: str
    model_id: str
    model_version: str
    deployment: str | None
    context_window_tokens: int
    capabilities: frozenset[ModelCapability]
    allowed_roles: frozenset[Role]
    immutable_weights: bool
    attestation_id: str
    registered_at: str
    registered_by: str
    max_output_tokens: int | None = None
    status: DeploymentStatus = DeploymentStatus.ACTIVE
    revoked_at: str | None = None
    revocation_reason: str | None = None
    schema: str = _SCHEMA

    def __post_init__(self) -> None:
        _identifier(self.record_id, "record_id")
        if isinstance(self.version, bool) or not isinstance(self.version, int):
            raise RegistryError("record version must be an integer")
        if self.version < 1:
            raise RegistryError("record version must be at least one")
        _identifier(self.provider, "provider")
        _identifier(self.model_id, "model_id")
        _identifier(self.model_version, "model_version")
        if self.deployment is not None:
            _identifier(self.deployment, "deployment")
        if isinstance(self.context_window_tokens, bool) or not isinstance(
            self.context_window_tokens, int
        ):
            raise RegistryError("context_window_tokens must be an integer")
        if self.context_window_tokens < 1:
            raise RegistryError("context_window_tokens must be positive")
        if self.max_output_tokens is not None:
            if isinstance(self.max_output_tokens, bool) or not isinstance(
                self.max_output_tokens, int
            ):
                raise RegistryError("max_output_tokens must be an integer")
            if self.max_output_tokens < 1:
                raise RegistryError("max_output_tokens must be positive")
        object.__setattr__(
            self, "capabilities", _capability_set(self.capabilities, "capabilities")
        )
        object.__setattr__(
            self, "allowed_roles", _role_set(self.allowed_roles, "allowed_roles")
        )
        if not isinstance(self.immutable_weights, bool):
            raise RegistryError("immutable_weights must be a boolean")
        _identifier(self.attestation_id, "attestation_id")
        object.__setattr__(
            self, "registered_at", _timestamp(self.registered_at, "registered_at")
        )
        _identifier(self.registered_by, "registered_by")
        if not isinstance(self.status, DeploymentStatus):
            raise RegistryError("status must be a DeploymentStatus")
        if self.revoked_at is not None:
            object.__setattr__(
                self, "revoked_at", _timestamp(self.revoked_at, "revoked_at")
            )
        if self.status is DeploymentStatus.REVOKED and self.revoked_at is None:
            raise RegistryError("revoked records must record revoked_at")
        if self.status is not DeploymentStatus.REVOKED and self.revoked_at is not None:
            raise RegistryError("only revoked records may record revoked_at")

    @property
    def canonical_document(self) -> dict[str, object]:
        """Return the canonical JSON document that the record hash covers."""
        return {
            "allowedRoles": sorted(role for role in self.allowed_roles),
            "attestationId": self.attestation_id,
            "capabilities": sorted(
                capability.value for capability in self.capabilities
            ),
            "contextWindowTokens": self.context_window_tokens,
            "deployment": self.deployment,
            "immutableWeights": self.immutable_weights,
            "maxOutputTokens": self.max_output_tokens,
            "modelId": self.model_id,
            "modelVersion": self.model_version,
            "provider": self.provider,
            "recordId": self.record_id,
            "registeredAt": self.registered_at,
            "registeredBy": self.registered_by,
            "revocationReason": self.revocation_reason,
            "revokedAt": self.revoked_at,
            "schema": self.schema,
            "status": self.status.value,
            "version": self.version,
        }

    @property
    def content_sha256(self) -> str:
        """Return the content digest of this record version."""
        return hashlib.sha256(
            _canonical_json(_jsonable(self.canonical_document)).encode("utf-8")
        ).hexdigest()
