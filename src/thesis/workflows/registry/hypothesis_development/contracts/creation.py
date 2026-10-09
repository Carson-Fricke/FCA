"""The chat request context that triggers one workflow execution."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime

from thesis.chat.contracts import WorkflowExecution
from thesis.chat.service import ChatService
from thesis.experiments.mandate import LaunchPartition, declared_launch_partition

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
    partition: LaunchPartition | None = None
    dependencies: Mapping[str, object] = field(default_factory=dict)

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
        """Return the detached request representation for transport.

        The launch request is persisted from this document when the execution
        is created (``Workflow._record_launch``), so ``partition`` is the data
        partition the execution is bound to for its whole life. It is built
        from explicit conservative dependency declarations and never defaulted
        or changed by later phases. Resume reads the durable partition.
        """
        if self.partition is None:
            raise HypothesisDevelopmentIntakeError(
                "launch requires an explicit frozen partition"
            )
        if declared_launch_partition(self.dependencies) != self.partition:
            raise HypothesisDevelopmentIntakeError(
                "partition must match declared information and position dependencies"
            )
        return {
            "chatId": self.chat_id,
            "createdAt": None if self.created_at is None else _iso(self.created_at),
            "executionId": self.execution_id,
            "ideas": self.ideas,
            "intentEpoch": self.intent_epoch,
            "partition": self.partition.as_dict(),
            "dependencies": dict(self.dependencies),
            "requestedBy": self.requested_by,
            "triggerMessageId": self.trigger_message_id,
            "workspaceId": self.workspace_id,
        }

    @classmethod
    def from_execution(
        cls,
        chat: ChatService,
        execution: WorkflowExecution,
        launch_request: Mapping[str, object],
    ) -> HypothesisDevelopmentRequest:
        """Rebuild the runnable request for a durable execution.

        Used when an operator message resumes the hypothesis-development
        conversation: the original idea is recovered from the trigger message
        and everything else from the execution record.
        """
        try:
            trigger = chat.get_message(
                execution.workspace_id, execution.trigger_message_id
            )
            ideas = _ideas_from_trigger(trigger.body)
        except (LookupError, ValueError):
            ideas = "Continue the hypothesis-development conversation."
        dependencies = launch_request.get("dependencies")
        if not isinstance(dependencies, Mapping):
            raise HypothesisDevelopmentIntakeError(
                "durable launch dependency declaration is missing"
            )
        partition = LaunchPartition.from_dict(launch_request.get("partition"))
        if declared_launch_partition(dependencies) != partition:
            raise HypothesisDevelopmentIntakeError(
                "durable partition disagrees with its dependency declaration"
            )
        return cls(
            workspace_id=execution.workspace_id,
            chat_id=execution.chat_id,
            execution_id=execution.execution_id,
            trigger_message_id=execution.trigger_message_id,
            requested_by=execution.requested_by,
            ideas=ideas,
            intent_epoch=execution.intent_epoch,
            created_at=execution.created_at,
            partition=partition,
            dependencies=dict(dependencies),
        )


def _ideas_from_trigger(body: str) -> str:
    """Recover the operator's original idea from the workflow trigger message."""
    text = body.strip()
    if text.startswith("{"):
        try:
            document = json.loads(text)
        except json.JSONDecodeError:
            return text or "Continue the hypothesis-development conversation."
        if isinstance(document, Mapping):
            ideas = document.get("ideas")
            if isinstance(ideas, str) and ideas.strip():
                return ideas.strip()
    return text or "Continue the hypothesis-development conversation."
