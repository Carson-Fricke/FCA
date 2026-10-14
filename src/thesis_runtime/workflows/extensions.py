"""Versioned JSON boundary between the workflow host and workflow extensions.

Extensions own input parsing, state folding, prompts, deterministic activities,
and reports. The host owns execution identity, accepted history and lifecycle.
An endpoint can execute locally or over a transport; no engine object crosses it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from thesis_runtime.contracts.records import WorkflowExecution
from thesis_runtime.workflows.execution_store import StoreTransition

PROTOCOL_VERSION = 2


class WorkflowExtension(Protocol):
    def invoke(
        self, operation: str, payload: Mapping[str, object]
    ) -> Mapping[str, object]:
        """Evaluate one versioned extension request and return detached JSON."""
        ...


def invoke_extension(
    endpoint: WorkflowExtension, operation: str, payload: Mapping[str, object]
) -> Mapping[str, object]:
    """Enforce the transport contract even for an in-process extension."""
    request = json.loads(
        json.dumps({**payload, "protocolVersion": PROTOCOL_VERSION}, allow_nan=False)
    )
    response = endpoint.invoke(operation, request)
    if not isinstance(response, Mapping):
        raise ValueError("workflow extension response must be an object")
    return json.loads(json.dumps(dict(response), allow_nan=False))


@dataclass(frozen=True, slots=True)
class RunRequest:
    workspace_id: str
    chat_id: str
    execution_id: str
    trigger_message_id: str
    requested_by: str
    intent_epoch: int
    payload: Mapping[str, object]

    def as_dict(self) -> dict[str, object]:
        return json.loads(json.dumps(dict(self.payload), allow_nan=False))

    @classmethod
    def bind(
        cls, execution: WorkflowExecution, payload: Mapping[str, object]
    ) -> RunRequest:
        identity = {
            "workspaceId": execution.workspace_id,
            "chatId": execution.chat_id,
            "executionId": execution.execution_id,
            "triggerMessageId": execution.trigger_message_id,
            "requestedBy": execution.requested_by,
            "intentEpoch": execution.intent_epoch,
        }
        if any(payload.get(key) != value for key, value in identity.items()):
            raise ValueError("workflow extension changed trusted execution identity")
        return cls(
            execution.workspace_id,
            execution.chat_id,
            execution.execution_id,
            execution.trigger_message_id,
            execution.requested_by,
            execution.intent_epoch,
            dict(payload),
        )


@dataclass(frozen=True, slots=True)
class StateSnapshot:
    """The host observes structural state and the accepted completion history."""

    phase: str
    intent_epoch: int
    transitions: tuple[StoreTransition, ...]

    @classmethod
    def decode(
        cls, payload: Mapping[str, object], transitions: tuple[StoreTransition, ...]
    ) -> StateSnapshot:
        phase, epoch = payload.get("phase"), payload.get("intentEpoch")
        if not isinstance(phase, str) or not phase.strip():
            raise ValueError("extension state requires a phase")
        if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0:
            raise ValueError("extension state requires a non-negative intentEpoch")
        return cls(phase, epoch, transitions)
