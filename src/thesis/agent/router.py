"""Policy-driven model-role routing and invocation provenance."""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Literal, Protocol

from thesis.agent.context import PromptContext
from thesis.agent.skills import SkillBundle, SkillRef, SkillRegistry, content_hash

Role = Literal["primary", "coding", "critique", "research"]
_ROLES = frozenset(("primary", "coding", "critique", "research"))
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,255}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _new_id() -> str:
    return uuid.uuid4().hex


class RoutingError(ValueError):
    """Raised when a role cannot be resolved under the active policy."""


class ModelInvocationError(RuntimeError):
    """Raised when an injected model provider fails an invocation."""

    def __init__(self, message: str, provenance: InvocationProvenance) -> None:
        super().__init__(message)
        self.provenance = provenance


def _validate_identifier(value: str, field_name: str) -> None:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise RoutingError(f"{field_name} must be a portable identifier")


def _validate_version(value: str, field_name: str) -> None:
    if not isinstance(value, str) or _VERSION.fullmatch(value) is None:
        raise RoutingError(f"{field_name} must be a semantic version")


def _validate_digest(value: str, field_name: str) -> None:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise RoutingError(f"{field_name} must be a SHA-256 digest")


def _validate_workspace_scope(value: str) -> None:
    if (
        not isinstance(value, str)
        or _IDENTIFIER.fullmatch(value) is None
        or ".." in value
        or "//" in value
        or "\\" in value
    ):
        raise RoutingError("workspace_scope must be a portable identifier")


@dataclass(frozen=True, slots=True)
class ModelProfile:
    """A provider/model deployment selected by a role policy."""

    profile_id: str
    version: str
    provider: str
    model: str
    knowledge_cutoff: str | None = None
    cutoff_evidence: str | None = None

    def __post_init__(self) -> None:
        _validate_identifier(self.profile_id, "profile_id")
        _validate_version(self.version, "profile version")
        _validate_identifier(self.provider, "provider")
        _validate_identifier(self.model, "model")
        for name, value in (
            ("knowledge_cutoff", self.knowledge_cutoff),
            ("cutoff_evidence", self.cutoff_evidence),
        ):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise RoutingError(f"{name} must be non-empty when set")


@dataclass(frozen=True, slots=True)
class RolePolicy:
    """The versioned policy selecting skills, tools, and a model profile."""

    role: Role
    version: str
    model_profile: str
    skills: tuple[SkillRef | str, ...] = ()
    allowed_tools: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.role not in _ROLES:
            raise RoutingError(f"unsupported Thesis role: {self.role}")
        _validate_version(self.version, "role policy version")
        _validate_identifier(self.model_profile, "model_profile")
        tools = tuple(self.allowed_tools)
        if any(not isinstance(tool, str) or not tool.strip() for tool in tools):
            raise RoutingError("allowed_tools must contain non-empty names")
        if len(tools) != len(set(tools)):
            raise RoutingError("allowed_tools must be unique")
        object.__setattr__(self, "allowed_tools", tuple(sorted(tools)))
        object.__setattr__(
            self,
            "skills",
            tuple(
                SkillRef.parse(skill) if isinstance(skill, str) else skill
                for skill in self.skills
            ),
        )


@dataclass(frozen=True, slots=True)
class PolicyBundle:
    """Immutable platform and policy identities included in provenance."""

    bundle_id: str
    version: str
    bundle_sha256: str
    policy_sha256: str

    def __post_init__(self) -> None:
        _validate_identifier(self.bundle_id, "bundle_id")
        _validate_version(self.version, "bundle version")
        _validate_digest(self.bundle_sha256, "bundle_sha256")
        _validate_digest(self.policy_sha256, "policy_sha256")

    @property
    def hashes(self) -> dict[str, str]:
        """Return the named hashes that define this policy bundle."""
        return {
            "bundle": self.bundle_sha256,
            "policy": self.policy_sha256,
        }


@dataclass(frozen=True, slots=True)
class RouteDecision:
    """Resolved role, deployment, skills, and effective tool policy."""

    role: Role
    role_version: str
    profile: ModelProfile
    skill_bundle: SkillBundle
    allowed_tools: tuple[str, ...]
    bundle_hashes: Mapping[str, str]
    policy_hash: str

    def __post_init__(self) -> None:
        _validate_digest(self.policy_hash, "policy_hash")
        object.__setattr__(
            self,
            "bundle_hashes",
            MappingProxyType(dict(sorted(self.bundle_hashes.items()))),
        )

    @property
    def model(self) -> str:
        """Return the configured model deployment name."""
        return self.profile.model


@dataclass(frozen=True, slots=True)
class ModelResponse:
    """Provider response carrying output content or an immutable output reference."""

    output: str | None = None
    output_reference: str | None = None

    def __post_init__(self) -> None:
        if self.output is None and not self.output_reference:
            raise RoutingError("model response must contain output or output_reference")
        if self.output is not None and not isinstance(self.output, str):
            raise RoutingError("model output must be text")
        if self.output_reference is not None and (
            not isinstance(self.output_reference, str)
            or not self.output_reference.strip()
        ):
            raise RoutingError("output_reference must be non-empty when set")

    @property
    def output_hash(self) -> str | None:
        """Return the output hash when the provider returned output content."""
        return None if self.output is None else content_hash(self.output)


class ModelProvider(Protocol):
    """Injected provider boundary; no concrete network client is owned here."""

    def invoke(
        self,
        *,
        profile: ModelProfile,
        prompt: PromptContext,
        allowed_tools: tuple[str, ...],
        response_format: Mapping[str, object] | None = None,
    ) -> ModelResponse | str: ...


@dataclass(frozen=True, slots=True)
class InvocationProvenance:
    """Redacted immutable record for one model invocation."""

    invocation_id: str
    created_at: datetime
    role: Role
    role_version: str
    provider: str
    model_profile: str
    model: str
    model_version: str
    bundle_hashes: Mapping[str, str]
    skill_hashes: Mapping[str, str]
    skill_bundle_hash: str
    policy_hash: str
    allowed_tools: tuple[str, ...]
    input_hash: str
    input_references: tuple[str, ...]
    output_hash: str | None
    output_reference: str | None
    workspace_scope: str
    context_id: str | None
    knowledge_cutoff: str | None = None
    cutoff_evidence: str | None = None
    schema_version: Literal[1] = 1

    def __post_init__(self) -> None:
        if not self.invocation_id:
            raise RoutingError("invocation_id is required")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise RoutingError("invocation timestamp must include a timezone")
        if self.role not in _ROLES:
            raise RoutingError(f"unsupported Thesis role: {self.role}")
        _validate_workspace_scope(self.workspace_scope)
        for name, digest in (
            ("input_hash", self.input_hash),
            ("skill_bundle_hash", self.skill_bundle_hash),
            ("policy_hash", self.policy_hash),
        ):
            _validate_digest(digest, name)
        for name, digest in self.bundle_hashes.items():
            _validate_digest(digest, f"bundle hash {name}")
        for name, digest in self.skill_hashes.items():
            _validate_digest(digest, f"skill hash {name}")
        if self.output_hash is not None:
            _validate_digest(self.output_hash, "output_hash")
        if self.output_hash is None and self.output_reference is None:
            raise RoutingError("provenance must contain output hash or reference")
        object.__setattr__(self, "created_at", self.created_at.astimezone(UTC))
        object.__setattr__(self, "allowed_tools", tuple(sorted(self.allowed_tools)))
        object.__setattr__(
            self, "input_references", tuple(sorted(set(self.input_references)))
        )
        object.__setattr__(
            self,
            "bundle_hashes",
            MappingProxyType(dict(sorted(self.bundle_hashes.items()))),
        )
        object.__setattr__(
            self,
            "skill_hashes",
            MappingProxyType(dict(sorted(self.skill_hashes.items()))),
        )

    def as_dict(self) -> dict[str, object]:
        """Return a portable redacted audit representation."""
        return {
            "allowedTools": list(self.allowed_tools),
            "bundleHashes": dict(self.bundle_hashes),
            "contextId": self.context_id,
            "createdAt": self.created_at.isoformat().replace("+00:00", "Z"),
            "inputHash": self.input_hash,
            "inputReferences": list(self.input_references),
            "invocationId": self.invocation_id,
            "knowledgeCutoff": self.knowledge_cutoff,
            "model": self.model,
            "modelProfile": self.model_profile,
            "modelVersion": self.model_version,
            "outputHash": self.output_hash,
            "outputReference": self.output_reference,
            "policyHash": self.policy_hash,
            "provider": self.provider,
            "role": self.role,
            "roleVersion": self.role_version,
            "schemaVersion": self.schema_version,
            "skillBundleHash": self.skill_bundle_hash,
            "skillHashes": dict(self.skill_hashes),
            "cutoffEvidence": self.cutoff_evidence,
            "workspaceScope": self.workspace_scope,
        }


@dataclass(frozen=True, slots=True)
class InvocationResult:
    """Provider output paired with its immutable invocation provenance."""

    response: ModelResponse
    provenance: InvocationProvenance

    @property
    def output(self) -> str | None:
        return self.response.output


class ModelRouter:
    """Resolve policy roles and invoke only through injected provider boundaries."""

    def __init__(
        self,
        *,
        role_policies: Mapping[Role, RolePolicy],
        model_profiles: Mapping[str, ModelProfile],
        providers: Mapping[str, ModelProvider],
        skills: SkillRegistry,
        bundle: PolicyBundle | None = None,
        bundle_hashes: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] = _utc_now,
        id_factory: Callable[[], str] = _new_id,
    ) -> None:
        self._role_policies = dict(role_policies)
        self._model_profiles = dict(model_profiles)
        self._providers = dict(providers)
        self._skills = skills
        if bundle is not None and bundle_hashes is not None:
            raise RoutingError("provide bundle or bundle_hashes, not both")
        selected_hashes = (
            bundle.hashes if bundle is not None else dict(bundle_hashes or {})
        )
        for name, digest in selected_hashes.items():
            _validate_digest(digest, f"bundle hash {name}")
        self._bundle_hashes = dict(sorted(selected_hashes.items()))
        self._clock = clock
        self._id_factory = id_factory
        self._invocations: list[InvocationProvenance] = []

    @property
    def invocations(self) -> tuple[InvocationProvenance, ...]:
        """Return detached invocation records in creation order."""
        return tuple(self._invocations)

    def resolve(self, role: Role | str) -> RouteDecision:
        """Resolve a role through the configured policy and profile registries."""
        if role not in _ROLES:
            raise RoutingError(f"unsupported Thesis role: {role}")
        try:
            policy = self._role_policies[role]
        except KeyError as error:
            raise RoutingError(f"no policy configured for role: {role}") from error
        if policy.role != role:
            raise RoutingError(f"policy role mismatch for {role}")
        try:
            profile = self._model_profiles[policy.model_profile]
        except KeyError as error:
            raise RoutingError(
                f"model profile is not configured: {policy.model_profile}"
            ) from error
        try:
            bundle = self._skills.bundle(policy.skills)
        except (KeyError, ValueError) as error:
            raise RoutingError(f"skills cannot be resolved for role: {role}") from error
        requested_by_skill = {
            tool for skill in bundle.skills for tool in skill.allowed_tools
        }
        allowed_tools = set(policy.allowed_tools)
        if requested_by_skill:
            allowed_tools.intersection_update(requested_by_skill)
        policy_hash = content_hash(
            {
                "allowedTools": sorted(allowed_tools),
                "modelProfile": profile.profile_id,
                "modelVersion": profile.version,
                "role": policy.role,
                "roleVersion": policy.version,
                "skills": bundle.hashes,
            }
        )
        return RouteDecision(
            role=role,
            role_version=policy.version,
            profile=profile,
            skill_bundle=bundle,
            allowed_tools=tuple(sorted(allowed_tools)),
            bundle_hashes=self._bundle_hashes,
            policy_hash=policy_hash,
        )

    def invoke(
        self,
        role: Role | str,
        context: PromptContext,
        *,
        output_reference: str | None = None,
        response_format: Mapping[str, object] | None = None,
    ) -> InvocationResult:
        """Invoke the selected injected provider and record redacted provenance.

        ``response_format`` is an optional provider output constraint (for
        example ``{"type": "json_object"}``) forwarded only to providers whose
        ``invoke`` declares it, so older providers keep working unchanged.
        """
        route = self.resolve(role)
        _validate_workspace_scope(context.workspace_scope)
        invocation_id = self._id_factory()
        created_at = self._clock()
        provider = self._providers.get(route.profile.provider)
        if provider is None:
            provenance = self._provenance(
                invocation_id=invocation_id,
                created_at=created_at,
                route=route,
                context=context,
                output_hash=None,
                output_reference=f"error://{invocation_id}",
            )
            self._invocations.append(provenance)
            raise ModelInvocationError("model provider is not configured", provenance)

        try:
            raw_response = provider.invoke(
                profile=route.profile,
                prompt=context,
                allowed_tools=route.allowed_tools,
                response_format=response_format,
            )
            response = (
                raw_response
                if isinstance(raw_response, ModelResponse)
                else ModelResponse(output=raw_response)
            )
        except Exception as error:
            provenance = self._provenance(
                invocation_id=invocation_id,
                created_at=created_at,
                route=route,
                context=context,
                output_hash=None,
                output_reference=f"error://{invocation_id}",
            )
            self._invocations.append(provenance)
            detail = str(error).strip() or type(error).__name__
            raise ModelInvocationError(
                f"injected model provider failed: {detail}", provenance
            ) from error

        if output_reference is not None:
            response = ModelResponse(
                output=response.output,
                output_reference=output_reference,
            )
        provenance = self._provenance(
            invocation_id=invocation_id,
            created_at=created_at,
            route=route,
            context=context,
            output_hash=response.output_hash,
            output_reference=response.output_reference,
        )
        self._invocations.append(provenance)
        return InvocationResult(response=response, provenance=provenance)

    def _provenance(
        self,
        *,
        invocation_id: str,
        created_at: datetime,
        route: RouteDecision,
        context: PromptContext,
        output_hash: str | None,
        output_reference: str | None,
    ) -> InvocationProvenance:
        return InvocationProvenance(
            invocation_id=invocation_id,
            created_at=created_at,
            role=route.role,
            role_version=route.role_version,
            provider=route.profile.provider,
            model_profile=route.profile.profile_id,
            model=route.profile.model,
            model_version=route.profile.version,
            bundle_hashes=route.bundle_hashes,
            skill_hashes=route.skill_bundle.hashes,
            skill_bundle_hash=route.skill_bundle.bundle_hash,
            policy_hash=route.policy_hash,
            allowed_tools=route.allowed_tools,
            input_hash=context.input_hash,
            input_references=context.references,
            output_hash=output_hash,
            output_reference=output_reference,
            workspace_scope=context.workspace_scope,
            context_id=context.context_id,
            knowledge_cutoff=route.profile.knowledge_cutoff,
            cutoff_evidence=route.profile.cutoff_evidence,
        )


__all__ = [
    "InvocationProvenance",
    "InvocationResult",
    "ModelInvocationError",
    "ModelProfile",
    "ModelProvider",
    "ModelResponse",
    "ModelRouter",
    "PolicyBundle",
    "Role",
    "RolePolicy",
    "RouteDecision",
    "RoutingError",
]
