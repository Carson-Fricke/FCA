"""Local and Azure security boundaries for Thesis operations.

The boundaries here are deliberately thin: every check delegates to an existing
durable primitive (workspace manifests, execution events, imported artifacts,
consent grants, residency audit records, and the Foundry provider contract) and
only decides whether an act stays inside the selected boundary. Local mode and
Azure mode must expose the same workspace, event, artifact, consent, and audit
contracts, so a :class:`ModeParityReport` records the outcome of each boundary
kind and :func:`assert_local_azure_parity` refuses to accept a mode split that
weakens any of them.

The remaining helpers cover the cross-cutting Phase 6 threats: prompt injection
in untrusted text, secret exfiltration into payloads and manifests, confused
deputy tool requests, and provider-authentication failure that must still leave
the offline workspace available.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Literal

from thesis.activation.consent import ConsentGrant, RevokedGrant, is_active
from thesis.agent.residency import OperationAuditRecord
from thesis.artifacts.importer import ImportedArtifactReference
from thesis.execution.events import ExecutionEvent
from thesis.knowledge.ingest import is_instruction_like
from thesis.providers.foundry import RuntimeCapabilityReport


class SecurityError(Exception):
    """Base class for Thesis operational security failures."""


class SecurityBoundaryError(SecurityError):
    """A workspace, event, artifact, consent, or audit boundary was crossed."""


class SecretExfiltrationError(SecurityError):
    """Secret material was found in a payload that must not carry it."""


class ConfusedDeputyError(SecurityError):
    """A tool was requested outside the role policy that authorizes it."""


class BoundaryKind(StrEnum):
    """The boundary kinds whose local and Azure contracts must be equivalent."""

    WORKSPACE = "workspace"
    EVENT = "event"
    ARTIFACT = "artifact"
    CONSENT = "consent"
    AUDIT = "audit"


@dataclass(frozen=True, slots=True)
class BoundaryCheck:
    """One deterministic boundary-check outcome for a mode-parity report."""

    kind: BoundaryKind
    subject: str
    passed: bool
    detail: str = ""

    def __post_init__(self) -> None:
        if not self.subject:
            raise SecurityError("a boundary check requires a subject")
        if not isinstance(self.kind, BoundaryKind):
            raise SecurityError("boundary kind must be a BoundaryKind")

    def as_dict(self) -> dict[str, object]:
        """Return the audit representation of this check."""
        return {
            "detail": self.detail,
            "kind": self.kind.value,
            "passed": self.passed,
            "subject": self.subject,
        }


@dataclass(frozen=True, slots=True)
class ModeParityReport:
    """The boundary outcomes observed in one runtime mode (local or Azure)."""

    mode: Literal["local", "azure"]
    checks: tuple[BoundaryCheck, ...]

    def __post_init__(self) -> None:
        if self.mode not in ("local", "azure"):
            raise SecurityError("mode must be 'local' or 'azure'")
        object.__setattr__(
            self,
            "checks",
            tuple(sorted(self.checks, key=lambda check: check.kind.value)),
        )

    @property
    def passed(self) -> bool:
        """Return whether every recorded boundary check passed."""
        return all(check.passed for check in self.checks)

    def equivalent_to(self, other: ModeParityReport) -> bool:
        """Return whether two modes observed the same kinds and outcomes."""
        own = {(check.kind, check.passed) for check in self.checks}
        theirs = {(check.kind, check.passed) for check in other.checks}
        return own == theirs

    def as_dict(self) -> dict[str, object]:
        """Return the audit representation of this parity report."""
        return {
            "checks": [check.as_dict() for check in self.checks],
            "mode": self.mode,
            "passed": self.passed,
        }


def check_workspace_boundary(
    boundary_workspace: str, candidate_workspace: str
) -> BoundaryCheck:
    """Reject any act that references a workspace outside the selected one."""
    if not boundary_workspace or not candidate_workspace:
        raise SecurityBoundaryError("workspace boundary requires two workspace ids")
    if boundary_workspace != candidate_workspace:
        raise SecurityBoundaryError(
            "cross-workspace access denied: selected "
            f"{boundary_workspace!r} cannot reference {candidate_workspace!r}"
        )
    return BoundaryCheck(
        kind=BoundaryKind.WORKSPACE,
        subject=candidate_workspace,
        passed=True,
        detail="act stays within the selected workspace",
    )


def check_event_boundary(
    boundary_workspace: str, event: ExecutionEvent
) -> BoundaryCheck:
    """Reject an execution event addressed to a different workspace stream."""
    if event.workspace_id != boundary_workspace:
        raise SecurityBoundaryError(
            "execution event workspace does not match the selected workspace"
        )
    return BoundaryCheck(
        kind=BoundaryKind.EVENT,
        subject=event.execution_id,
        passed=True,
        detail=f"workspace={event.workspace_id}",
    )


def check_artifact_boundary(
    reference: ImportedArtifactReference, *, boundary_prefix: str
) -> BoundaryCheck:
    """Reject an artifact reference that is not confined beneath the boundary."""
    uri = reference.uri
    path = PurePosixPath(uri)
    if path.is_absolute() or ".." in path.parts:
        raise SecurityBoundaryError(
            "artifact reference is not a confined relative path"
        )
    prefix = boundary_prefix.strip("/")
    if prefix and uri != prefix and not uri.startswith(f"{prefix}/"):
        raise SecurityBoundaryError(
            f"artifact reference {uri!r} is outside the workspace boundary"
        )
    return BoundaryCheck(
        kind=BoundaryKind.ARTIFACT,
        subject=uri,
        passed=True,
        detail=f"prefix={prefix or '/'}",
    )


def check_consent_boundary(
    grant: ConsentGrant,
    *,
    at: datetime,
    revocations: Iterable[RevokedGrant] = (),
) -> BoundaryCheck:
    """Reject an activation act made under an inactive or revoked grant."""
    if not is_active(grant, at, revocations=tuple(revocations)):
        raise SecurityBoundaryError("consent grant is not active at the requested time")
    return BoundaryCheck(
        kind=BoundaryKind.CONSENT,
        subject=grant.grant_id,
        passed=True,
        detail=f"action={grant.action.value}; environment={grant.environment.value}",
    )


def check_audit_boundary(
    record: OperationAuditRecord, *, boundary_workspace: str
) -> BoundaryCheck:
    """Reject an audit record written for a different workspace residency."""
    if record.workspace_id != boundary_workspace:
        raise SecurityBoundaryError(
            "audit record workspace does not match the selected workspace"
        )
    return BoundaryCheck(
        kind=BoundaryKind.AUDIT,
        subject=record.operation_id,
        passed=True,
        detail=f"workspace={record.workspace_id}",
    )


def assert_local_azure_parity(local: ModeParityReport, azure: ModeParityReport) -> None:
    """Refuse a local/Azure split that observes different boundary outcomes."""
    if local.mode != "local" or azure.mode != "azure":
        raise SecurityError("parity requires a local report and an azure report")
    if not local.equivalent_to(azure):
        raise SecurityBoundaryError(
            "local and azure boundary checks are not equivalent"
        )


def scan_untrusted_text(text: str) -> bool:
    """Flag untrusted text that reads like an imperative agent instruction.

    The detector is heuristic and never rewrites content: a flagged page stays
    quoted external data at the lowest prompt precedence. Callers use the flag
    to route the material through :class:`~thesis.agent.context.UntrustedData`.
    """
    if not isinstance(text, str):
        raise SecurityError("untrusted text must be a string")
    return is_instruction_like(text)


_SECRET_KEY = re.compile(
    r"(?:^|[_-])(?:access[_-]?key|api[_-]?key|api[_-]?secret|bearer|"
    r"client[_-]?secret|connection[_-]?string|credential|password|"
    r"private[_-]?key|secret|token)(?:$|[_-])",
    re.IGNORECASE,
)
_SECRET_REFERENCE = re.compile(
    r"(?:\$\{[A-Z_][A-Z0-9_]*\}|"
    r"https://[A-Za-z0-9-]+\.vault\.azure\.net/secrets/"
    r"[A-Za-z0-9-]+(?:/[A-Fa-f0-9]+)?)"
)
_PRIVATE_KEY_MARKER = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")
_BEARER_VALUE = re.compile(r"\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{16,}")
_AWS_ACCESS_KEY = re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")


def _string_leaves(payload: object) -> Iterable[tuple[str, str | None, str]]:
    """Yield ``(path, key, text)`` for every string leaf in a JSON-ish payload."""
    stack: list[tuple[str, str | None, object]] = [("$", None, payload)]
    while stack:
        path, key, value = stack.pop()
        if isinstance(value, str):
            yield path, key, value
        elif isinstance(value, Mapping):
            for item_key, item in value.items():
                stack.append((f"{path}.{item_key}", str(item_key), item))
        elif isinstance(value, list | tuple):
            for index, item in enumerate(value):
                stack.append((f"{path}[{index}]", None, item))


def assert_no_secret_material(payload: object) -> None:
    """Fail closed when a payload carries a concrete secret value.

    Secret-store references (``${ENV_VAR}`` or an Azure Key Vault URL) are the
    only accepted forms; resolved tokens, private keys, bearer values, and
    access-key identifiers raise :class:`SecretExfiltrationError`.
    """
    for path, key, text in _string_leaves(payload):
        if (
            key is not None
            and _SECRET_KEY.search(key) is not None
            and _SECRET_REFERENCE.fullmatch(text) is None
        ):
            raise SecretExfiltrationError(
                f"secret value is not a secret-store reference at {path}"
            )
        if (
            _PRIVATE_KEY_MARKER.search(text) is not None
            or _BEARER_VALUE.search(text) is not None
            or _AWS_ACCESS_KEY.search(text) is not None
        ):
            raise SecretExfiltrationError(f"secret material detected at {path}")
    return None


def assert_tool_permitted(tool: str, *, allowed_tools: Iterable[str]) -> None:
    """Reject a confused-deputy tool request outside the role's allowlist."""
    allowed = tuple(sorted({name for name in allowed_tools}))
    if tool not in allowed:
        raise ConfusedDeputyError(
            f"tool {tool!r} is not permitted by the role policy; allowed={allowed}"
        )
    return None


def classify_provider_auth(report: RuntimeCapabilityReport) -> BoundaryCheck:
    """Classify provider authentication without ever weakening offline access."""
    provider = report.provider
    return BoundaryCheck(
        kind=BoundaryKind.AUDIT,
        subject=provider.provider,
        passed=report.offline_workspace_available,
        detail=(
            f"reason={provider.reason}; authenticated={provider.authenticated}; "
            f"model_backed={report.model_backed_capabilities_available}"
        ),
    )


__all__ = [
    "BoundaryCheck",
    "BoundaryKind",
    "ConfusedDeputyError",
    "ModeParityReport",
    "SecretExfiltrationError",
    "SecurityBoundaryError",
    "SecurityError",
    "assert_local_azure_parity",
    "assert_no_secret_material",
    "assert_tool_permitted",
    "check_artifact_boundary",
    "check_audit_boundary",
    "check_consent_boundary",
    "check_event_boundary",
    "check_workspace_boundary",
    "classify_provider_auth",
    "scan_untrusted_text",
]
