"""Strict, portable workspace manifest and strategy-family charter."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)


class WorkspaceManifestError(ValueError):
    """Raised when a serialized workspace manifest cannot be loaded safely."""


class WorkspaceSecretError(WorkspaceManifestError):
    """Raised when a manifest contains secret material instead of a reference."""


def _yaml_tuple(value: object) -> object:
    return tuple(value) if isinstance(value, list) else value


def _utc_datetime(value: object) -> object:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("must be an ISO 8601 datetime") from error
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("must include a UTC offset")
        return value.astimezone(UTC)
    return value


WorkspaceId = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=3,
        max_length=63,
        pattern=r"^[a-z0-9][a-z0-9-]*[a-z0-9]$",
    ),
]
Identifier = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@-]*$",
    ),
]
BranchName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._/-]*$",
    ),
]
NonEmptyText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=2_048),
]
Version = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$"),
]
Sha256Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
PositiveInteger = Annotated[int, Field(gt=0)]
NonNegativeInteger = Annotated[int, Field(ge=0)]
SecretReference = Annotated[
    str,
    StringConstraints(
        pattern=(
            r"^(?:\$\{[A-Z_][A-Z0-9_]*\}|"
            r"https://[A-Za-z0-9-]+\.vault\.azure\.net/secrets/"
            r"[A-Za-z0-9-]+(?:/[A-Fa-f0-9]+)?)$"
        )
    ),
]


class StrictWorkspaceModel(BaseModel):
    """Base model that rejects undeclared fields and implicit coercion."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
    )


class WorkspaceOwner(StrictWorkspaceModel):
    """Stable owner identity without authentication material."""

    owner_id: Identifier
    identity_provider: Literal["local", "entra"]
    display_name: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=256),
    ]


class StrategyFamilyCharter(StrictWorkspaceModel):
    """Scope that determines which related strategies belong in a workspace."""

    family_id: Identifier
    title: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=256),
    ]
    objective: NonEmptyText
    markets: Annotated[
        tuple[Identifier, ...], BeforeValidator(_yaml_tuple), Field(min_length=1)
    ]
    horizons: Annotated[
        tuple[Identifier, ...], BeforeValidator(_yaml_tuple), Field(min_length=1)
    ]
    relationship: NonEmptyText
    out_of_scope: Annotated[tuple[NonEmptyText, ...], BeforeValidator(_yaml_tuple)]

    @model_validator(mode="after")
    def _sets_are_unique(self) -> StrategyFamilyCharter:
        for name, values in (
            ("markets", self.markets),
            ("horizons", self.horizons),
            ("out_of_scope", self.out_of_scope),
        ):
            canonical = [value.casefold() for value in values]
            if len(canonical) != len(set(canonical)):
                raise ValueError(f"charter {name} values must be unique")
        return self


class WorkspaceLimits(StrictWorkspaceModel):
    """Hard workspace budgets independent of a particular runtime host."""

    max_repository_bytes: PositiveInteger
    max_artifact_bytes: PositiveInteger
    max_concurrent_executions: PositiveInteger
    max_backtests_per_experiment: PositiveInteger
    max_model_tokens_per_execution: PositiveInteger
    max_web_requests_per_execution: NonNegativeInteger
    max_lake_rows_per_execution: PositiveInteger
    max_wall_time_seconds: PositiveInteger
    max_cpu_cores: PositiveInteger
    max_memory_mib: PositiveInteger
    max_strategy_lineages: PositiveInteger
    allowed_strategy_classes: Annotated[
        tuple[Identifier, ...], BeforeValidator(_yaml_tuple), Field(min_length=1)
    ]
    allowed_activation_modes: Annotated[
        tuple[Literal["paper", "shadow", "live"], ...], BeforeValidator(_yaml_tuple)
    ]

    @model_validator(mode="after")
    def _limits_are_coherent(self) -> WorkspaceLimits:
        if self.max_artifact_bytes > self.max_repository_bytes:
            raise ValueError("max_artifact_bytes must not exceed max_repository_bytes")
        if len(self.allowed_strategy_classes) != len(
            set(self.allowed_strategy_classes)
        ):
            raise ValueError("allowed_strategy_classes values must be unique")
        if len(self.allowed_activation_modes) != len(
            set(self.allowed_activation_modes)
        ):
            raise ValueError("allowed_activation_modes values must be unique")
        return self


class RuntimeProfileReference(StrictWorkspaceModel):
    """Pinned identity of a separately defined runtime profile."""

    profile_id: Identifier
    version: Version
    mode: Literal["local", "azure"]


class PlatformBundleReference(StrictWorkspaceModel):
    """Immutable effective platform and policy bundle identity."""

    bundle_id: Identifier
    version: Version
    bundle_sha256: Sha256Digest
    policy_sha256: Sha256Digest


class RepositoryIdentity(StrictWorkspaceModel):
    """Provider-independent identity of one workspace's private repository."""

    provider: Literal["local", "azure"]
    repository_id: Identifier
    default_branch: BranchName

    @field_validator("default_branch")
    @classmethod
    def _branch_is_safe(cls, value: str) -> str:
        if ".." in value or value.endswith((".", "/")) or "//" in value:
            raise ValueError("default_branch is not a valid Git branch name")
        return value


class WorkspaceManifest(StrictWorkspaceModel):
    """Versioned identity and safety envelope for one isolated workspace."""

    schema_version: Literal[1]
    workspace_id: WorkspaceId
    created_at: Annotated[datetime, BeforeValidator(_utc_datetime)]
    created_by: Identifier
    owner: WorkspaceOwner
    charter: StrategyFamilyCharter
    limits: WorkspaceLimits
    runtime_profile: RuntimeProfileReference
    platform_bundle: PlatformBundleReference
    repository_identity: RepositoryIdentity
    secret_references: dict[Identifier, SecretReference]

    @model_validator(mode="after")
    def _provider_matches_runtime(self) -> WorkspaceManifest:
        if self.repository_identity.provider != self.runtime_profile.mode:
            raise ValueError(
                "repository provider must match the selected runtime profile mode"
            )
        return self

    @property
    def content_hash(self) -> str:
        """Return the canonical hash used by durable provider records."""
        return manifest_sha256(self)


_SECRET_KEY = re.compile(
    r"(?:^|_)(?:access[_-]?key|api[_-]?key|api[_-]?secret|bearer|"
    r"client[_-]?secret|connection[_-]?string|credential|password|"
    r"private[_-]?key|secret|token)(?:$|_)",
    re.IGNORECASE,
)
_SECRET_REFERENCE = re.compile(
    r"(?:\$\{[A-Z_][A-Z0-9_]*\}|"
    r"https://[A-Za-z0-9-]+\.vault\.azure\.net/secrets/"
    r"[A-Za-z0-9-]+(?:/[A-Fa-f0-9]+)?)"
)
_PRIVATE_KEY_MARKER = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")


def _is_reference_tree(value: object) -> bool:
    if isinstance(value, str):
        return _SECRET_REFERENCE.fullmatch(value) is not None
    if isinstance(value, Mapping):
        return all(_is_reference_tree(item) for item in value.values())
    if isinstance(value, list | tuple):
        return bool(value) and all(_is_reference_tree(item) for item in value)
    return False


def _reject_secret_values(value: object, path: str = "") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            item_path = f"{path}.{key}" if path else str(key)
            if isinstance(key, str) and _SECRET_KEY.search(key):
                if not _is_reference_tree(item):
                    raise WorkspaceSecretError(
                        f"{item_path} must contain only secret-store references"
                    )
            _reject_secret_values(item, item_path)
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            _reject_secret_values(item, f"{path}.{index}")
    elif isinstance(value, str) and _PRIVATE_KEY_MARKER.search(value):
        raise WorkspaceSecretError(
            f"{path or 'manifest'} contains private key material"
        )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise WorkspaceManifestError(f"duplicate manifest key: {key}")
        result[key] = value
    return result


def _invalid_json_constant(value: str) -> None:
    raise WorkspaceManifestError(f"unsupported manifest number: {value}")


def load_workspace_manifest(path: str | Path) -> WorkspaceManifest:
    """Load the deterministic JSON subset of YAML 1.2 and validate it strictly."""
    manifest_path = Path(path)
    try:
        text = manifest_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise WorkspaceManifestError(
            f"unable to read workspace manifest: {manifest_path}"
        ) from error
    try:
        values = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_invalid_json_constant,
        )
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise WorkspaceManifestError(
            "workspace manifest must use the deterministic JSON subset of YAML 1.2"
        ) from error
    if not isinstance(values, dict):
        raise WorkspaceManifestError("workspace manifest root must be a mapping")
    _reject_secret_values(values)
    return WorkspaceManifest.model_validate(values)


def _manifest_values(manifest: WorkspaceManifest) -> dict[str, Any]:
    values = manifest.model_dump(mode="json")
    _reject_secret_values(values)
    validated = WorkspaceManifest.model_validate(values)
    return validated.model_dump(mode="json")


def dump_workspace_manifest(manifest: WorkspaceManifest) -> str:
    """Serialize a readable, deterministic YAML 1.2-compatible document."""
    return (
        json.dumps(
            _manifest_values(manifest),
            indent=2,
            sort_keys=True,
            ensure_ascii=True,
        )
        + "\n"
    )


def write_workspace_manifest(path: str | Path, manifest: WorkspaceManifest) -> None:
    """Write a validated manifest without resolving any secret references."""
    Path(path).write_text(dump_workspace_manifest(manifest), encoding="utf-8")


def manifest_sha256(manifest: WorkspaceManifest) -> str:
    """Hash the canonical manifest independently of its filesystem location."""
    canonical = json.dumps(
        _manifest_values(manifest),
        separators=(",", ":"),
        sort_keys=True,
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


__all__ = [
    "BranchName",
    "Identifier",
    "NonEmptyText",
    "PlatformBundleReference",
    "RepositoryIdentity",
    "RuntimeProfileReference",
    "SecretReference",
    "Sha256Digest",
    "StrategyFamilyCharter",
    "StrictWorkspaceModel",
    "Version",
    "WorkspaceId",
    "WorkspaceLimits",
    "WorkspaceManifest",
    "WorkspaceManifestError",
    "WorkspaceOwner",
    "WorkspaceSecretError",
    "dump_workspace_manifest",
    "load_workspace_manifest",
    "manifest_sha256",
    "write_workspace_manifest",
]
