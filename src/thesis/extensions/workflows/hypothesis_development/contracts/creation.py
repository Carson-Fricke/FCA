"""The chat request context that triggers one workflow execution."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from thesis_runtime.contracts.records import WorkflowExecution
from thesis_runtime.conversations.service import ChatService

from ..errors import (
    HypothesisDevelopmentIntakeError,
    WorkflowError,
)
from ..primitives import (
    _identifier,
    _iso,
    _text,
    _workspace_id,
)


@dataclass(frozen=True, slots=True)
class HypothesisDevelopmentRequest:
    """The chat request context that triggers one workflow execution."""

    workspace_id: str
    chat_id: str
    execution_id: str
    trigger_message_id: str
    requested_by: str
    ideas: str
    intent_epoch: int = 0
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        _workspace_id(self.workspace_id)
        for name in ("chat_id", "execution_id", "trigger_message_id", "requested_by"):
            _identifier(getattr(self, name), name)
        try:
            _text(self.ideas, "ideas", max_length=8_192)
        except WorkflowError as error:
            raise HypothesisDevelopmentIntakeError(str(error)) from error
        if self.intent_epoch < 0:
            raise HypothesisDevelopmentIntakeError("intent_epoch must not be negative")
        if self.created_at is not None:
            if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
                raise HypothesisDevelopmentIntakeError(
                    "created_at must be timezone-aware"
                )
            object.__setattr__(self, "created_at", self.created_at.astimezone(UTC))

    def as_dict(self) -> dict[str, object]:
        """Persist the operator request and trusted launch context."""
        return {
            "chatId": self.chat_id,
            "createdAt": None if self.created_at is None else _iso(self.created_at),
            "executionId": self.execution_id,
            "ideas": self.ideas,
            "intentEpoch": self.intent_epoch,
            "requestedBy": self.requested_by,
            "triggerMessageId": self.trigger_message_id,
            "workspaceId": self.workspace_id,
        }

    @classmethod
    def from_payload(
        cls, payload: Mapping[str, object]
    ) -> HypothesisDevelopmentRequest:
        """Decode the extension launch document without consulting chat history."""
        if "partition" in payload or "dependencies" in payload:
            raise HypothesisDevelopmentIntakeError(
                "outdated workflow launch contract; start a new execution"
            )
        epoch = payload.get("intentEpoch")
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
            raise HypothesisDevelopmentIntakeError(
                "intent epoch must be a non-negative integer"
            )
        created = payload.get("createdAt")
        return cls(
            workspace_id=str(payload["workspaceId"]),
            chat_id=str(payload["chatId"]),
            execution_id=str(payload["executionId"]),
            trigger_message_id=str(payload["triggerMessageId"]),
            requested_by=str(payload["requestedBy"]),
            ideas=str(payload["ideas"]),
            intent_epoch=epoch,
            created_at=None
            if created is None
            else datetime.fromisoformat(str(created)),
        )

    @classmethod
    def from_execution(
        cls,
        chat: ChatService,
        execution: WorkflowExecution,
        launch_request: Mapping[str, object],
    ) -> HypothesisDevelopmentRequest:
        """Recover the preserved research request and current execution context."""
        del chat
        request = cls.from_payload(launch_request)
        return cls(
            workspace_id=execution.workspace_id,
            chat_id=execution.chat_id,
            execution_id=execution.execution_id,
            trigger_message_id=execution.trigger_message_id,
            requested_by=execution.requested_by,
            ideas=request.ideas,
            intent_epoch=execution.intent_epoch,
            created_at=execution.created_at,
        )
