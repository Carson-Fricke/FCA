"""CLI-equivalent workspace operations facade over composed services.

Every operation on this facade takes its workspace from the injected
:class:`AgentResidency` binding, never from a parameter, and appends one
immutable audit record through the residency store.  The facade composes
existing services (the chat service and the agent router); where a backing
service is not implemented yet, the operation returns a typed unavailable
outcome and records the attempt — it never pretends to succeed or
fabricates a result, mirroring the T173 ``StatusSurface`` pattern.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from thesis.agent.residency import (
    AgentResidency,
    OperationAuditRecord,
    OperationKind,
    OperationState,
    ResidencyError,
    validate_reference,
)
from thesis.agent.router import ModelRouter
from thesis.agent.skills import content_hash
from thesis.chat.contracts import ChatThread
from thesis.chat.service import ChatService

_OPERATION_PREFIX = "op-"
_OWNER = "thesis-agent"
_TOPIC_MAX = 8_192
_TITLE_MAX = 256
_SOURCE_MAX = 64_000

_UNAVAILABLE_REASON: dict[OperationKind, str] = {
    "edit_strategy_code": "strategy_editing_service_unavailable",
    "request_backtest": "backtest_service_unavailable",
    "deploy_paper": "deployment_gatekeeper_unavailable",
    "open_investigation": "chat_service_unavailable",
}
_ROUTE_ROLE: dict[OperationKind, str] = {
    "edit_strategy_code": "coding",
    "request_backtest": "primary",
    "deploy_paper": "primary",
    "open_investigation": "primary",
}


def _text(value: str, field: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ResidencyError(f"{field} must be non-empty text")
    if len(value) > maximum:
        raise ResidencyError(f"{field} is too large")
    return value


@dataclass(frozen=True, slots=True)
class EditRecord:
    """The typed outcome of one strategy-code edit request."""

    operation_id: str
    workspace_id: str
    strategy_ref: str
    source_sha256: str
    state: OperationState
    created_at: datetime
    reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        """Return the canonical outcome document values."""
        return {
            "createdAt": _iso(self.created_at),
            "operationId": self.operation_id,
            "reason": self.reason,
            "sourceSha256": self.source_sha256,
            "state": self.state,
            "strategyRef": self.strategy_ref,
            "workspaceId": self.workspace_id,
        }


@dataclass(frozen=True, slots=True)
class OperationReceipt:
    """The typed outcome of one backtest request."""

    operation_id: str
    workspace_id: str
    config_ref: str
    state: OperationState
    created_at: datetime
    reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        """Return the canonical outcome document values."""
        return {
            "configRef": self.config_ref,
            "createdAt": _iso(self.created_at),
            "operationId": self.operation_id,
            "reason": self.reason,
            "state": self.state,
            "workspaceId": self.workspace_id,
        }


@dataclass(frozen=True, slots=True)
class OperationOutcome:
    """The typed outcome of one paper deployment request."""

    operation_id: str
    workspace_id: str
    consent_recorded: bool
    state: OperationState
    created_at: datetime
    reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        """Return the canonical outcome document values."""
        return {
            "consentRecorded": self.consent_recorded,
            "createdAt": _iso(self.created_at),
            "operationId": self.operation_id,
            "reason": self.reason,
            "state": self.state,
            "workspaceId": self.workspace_id,
        }


@dataclass(frozen=True, slots=True)
class InvestigationSession:
    """The typed outcome of one hypothesis investigation request."""

    operation_id: str
    workspace_id: str
    topic: str
    investigation_id: str | None
    state: OperationState
    created_at: datetime
    reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        """Return the canonical outcome document values."""
        return {
            "createdAt": _iso(self.created_at),
            "investigationId": self.investigation_id,
            "operationId": self.operation_id,
            "reason": self.reason,
            "state": self.state,
            "topic": self.topic,
            "workspaceId": self.workspace_id,
        }


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


class WorkspaceOperations:
    """Present the CLI-equivalent operational surface of one residency."""

    def __init__(
        self,
        residency: AgentResidency,
        *,
        chat: ChatService | None = None,
        router: ModelRouter | None = None,
    ) -> None:
        self._residency = residency
        self._chat = chat
        self._router = router

    def bind_chat(self, chat: ChatThread) -> ChatThread:
        """Bind an incoming chat thread to this residency's workspace."""
        self._residency.assert_same_workspace(chat.workspace_id)
        return chat

    def edit_strategy_code(self, strategy_ref: str, new_source: str) -> EditRecord:
        """Request a workspace strategy edit; recorded, currently unavailable."""
        validate_reference(strategy_ref, "strategy_ref")
        _text(new_source, "new_source", maximum=_SOURCE_MAX)
        operation_id, created_at = self._begin("edit_strategy_code")
        inputs = {
            "sourceSha256": content_hash(new_source),
            "strategyRef": strategy_ref,
        }
        reason = _UNAVAILABLE_REASON["edit_strategy_code"]
        record = self._record(
            operation_id,
            "edit_strategy_code",
            inputs,
            created_at,
            status="unavailable",
            reason=reason,
        )
        return EditRecord(
            operation_id=record.operation_id,
            workspace_id=self._residency.workspace_id,
            strategy_ref=strategy_ref,
            source_sha256=content_hash(new_source),
            state="unavailable",
            created_at=created_at,
            reason=reason,
        )

    def request_backtest(self, config_ref: str) -> OperationReceipt:
        """Request a harness backtest; recorded, currently unavailable."""
        validate_reference(config_ref, "config_ref")
        operation_id, created_at = self._begin("request_backtest")
        inputs = {"configRef": config_ref}
        reason = _UNAVAILABLE_REASON["request_backtest"]
        record = self._record(
            operation_id,
            "request_backtest",
            inputs,
            created_at,
            status="unavailable",
            reason=reason,
        )
        return OperationReceipt(
            operation_id=record.operation_id,
            workspace_id=self._residency.workspace_id,
            config_ref=config_ref,
            state="unavailable",
            created_at=created_at,
            reason=reason,
        )

    def deploy_paper(self, consent_confirmation: str) -> OperationOutcome:
        """Request paper deployment; fails closed without wired consent."""
        _text(consent_confirmation, "consent_confirmation", maximum=_SOURCE_MAX)
        operation_id, created_at = self._begin("deploy_paper")
        inputs = {"consentConfirmed": True}
        reason = _UNAVAILABLE_REASON["deploy_paper"]
        record = self._record(
            operation_id,
            "deploy_paper",
            inputs,
            created_at,
            status="unavailable",
            reason=reason,
        )
        return OperationOutcome(
            operation_id=record.operation_id,
            workspace_id=self._residency.workspace_id,
            consent_recorded=True,
            state="unavailable",
            created_at=created_at,
            reason=reason,
        )

    def open_investigation(self, topic: str) -> InvestigationSession:
        """Open a durable investigation through the composed chat service."""
        _text(topic, "topic", maximum=_TOPIC_MAX)
        operation_id, created_at = self._begin("open_investigation")
        inputs = {"topic": topic}
        if self._chat is None:
            reason = _UNAVAILABLE_REASON["open_investigation"]
            record = self._record(
                operation_id,
                "open_investigation",
                inputs,
                created_at,
                status="unavailable",
                reason=reason,
            )
            return InvestigationSession(
                operation_id=record.operation_id,
                workspace_id=self._residency.workspace_id,
                topic=topic,
                investigation_id=None,
                state="unavailable",
                created_at=created_at,
                reason=reason,
            )
        investigation = self._chat.create_investigation(
            self._residency.workspace_id,
            title=topic[:_TITLE_MAX],
            objective=topic,
            owner_actor_id=_OWNER,
        )
        record = self._record(
            operation_id,
            "open_investigation",
            inputs,
            created_at,
            status="recorded",
            reason=None,
            detail_ref=investigation.investigation_id,
        )
        return InvestigationSession(
            operation_id=record.operation_id,
            workspace_id=self._residency.workspace_id,
            topic=topic,
            investigation_id=investigation.investigation_id,
            state="recorded",
            created_at=created_at,
        )

    def _begin(self, operation: OperationKind) -> tuple[str, datetime]:
        return (
            f"{_OPERATION_PREFIX}{self._residency.new_id()}",
            self._residency.now(),
        )

    def _record(
        self,
        operation_id: str,
        operation: OperationKind,
        inputs: Mapping[str, object],
        created_at: datetime,
        *,
        status: OperationState,
        reason: str | None,
        detail_ref: str | None = None,
    ) -> OperationAuditRecord:
        return self._residency.record_audit(
            operation_id=operation_id,
            operation=operation,
            status=status,
            inputs_digest=content_hash(inputs),
            created_at=created_at,
            reason=reason,
            route_ref=self._route_ref(operation),
            detail_ref=detail_ref,
        )

    def _route_ref(self, operation: OperationKind) -> str | None:
        if self._router is None:
            return None
        decision = self._router.resolve(_ROUTE_ROLE[operation])
        return f"{decision.role}@{decision.role_version}"


__all__ = [
    "EditRecord",
    "InvestigationSession",
    "OperationOutcome",
    "OperationReceipt",
    "OperationState",
    "WorkspaceOperations",
]
