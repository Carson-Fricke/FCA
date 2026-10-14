"""Trusted tool context attached by the runtime to every dispatch.

A model can choose a tool and its arguments, but it can never choose its own
attribution.  The session constructs one :class:`ToolContext` from durable state
and hands it to the tool layer out of band, so workspace, chat, conversation,
sub-agent, workflow, phase, invocation, and idempotency attribution are always
runtime-owned.  The context is immutable and default-deny: a tool is permitted
only when it appears in ``allowed_tools``.
"""

from __future__ import annotations

from dataclasses import dataclass


class ToolContextError(ValueError):
    """Raised when a tool context is built from invalid trusted state."""


def _require_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ToolContextError(f"{name} must be non-empty text")
    return value.strip()


def _optional_text(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _require_text(value, name)


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Immutable, runtime-owned attribution for one tool invocation.

    ``intent_epoch`` is the owning execution's epoch at the moment the context
    was built; the completion tools stamp it into their request so the engine
    can reject a stale transition and the agent can re-derive the contract.
    """

    workspace_id: str
    chat_id: str
    conversation_id: str
    actor_id: str
    sub_agent_id: str | None = None
    parent_sub_agent_id: str | None = None
    workflow_execution_id: str | None = None
    phase_attempt_id: str | None = None
    trigger_message_id: str | None = None
    model_invocation_id: str | None = None
    tool_invocation_id: str | None = None
    idempotency_key: str | None = None
    #: The exact agent-facing tool policy. ``None`` means the policy was never
    #: configured (unrestricted); an empty tuple is a configured empty policy
    #: and permits nothing.
    allowed_tools: tuple[str, ...] | None = None
    intent_epoch: int = 0
    phase_executor: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "workspace_id", _require_text(self.workspace_id, "workspace_id")
        )
        object.__setattr__(self, "chat_id", _require_text(self.chat_id, "chat_id"))
        object.__setattr__(
            self,
            "conversation_id",
            _require_text(self.conversation_id, "conversation_id"),
        )
        object.__setattr__(self, "actor_id", _require_text(self.actor_id, "actor_id"))
        for name in (
            "sub_agent_id",
            "parent_sub_agent_id",
            "workflow_execution_id",
            "phase_attempt_id",
            "trigger_message_id",
            "model_invocation_id",
            "tool_invocation_id",
            "idempotency_key",
        ):
            object.__setattr__(self, name, _optional_text(getattr(self, name), name))
        if self.allowed_tools is not None:
            tools = tuple(dict.fromkeys(self.allowed_tools))
            for tool in tools:
                _require_text(tool, "allowed_tools entry")
            object.__setattr__(self, "allowed_tools", tuple(sorted(tools)))
        if self.intent_epoch < 0:
            raise ToolContextError("intent_epoch must not be negative")

    def permits(self, tool_name: str) -> bool:
        """Return whether the effective policy allows ``tool_name``.

        A configured policy permits exactly its entries; only an unconfigured
        policy (``None``) is unrestricted.
        """
        if self.allowed_tools is None:
            return True
        return tool_name in self.allowed_tools

    def with_policy(self, allowed_tools: tuple[str, ...]) -> ToolContext:
        """Return a copy carrying a different effective allowed-tool policy."""
        return ToolContext(
            workspace_id=self.workspace_id,
            chat_id=self.chat_id,
            conversation_id=self.conversation_id,
            actor_id=self.actor_id,
            sub_agent_id=self.sub_agent_id,
            parent_sub_agent_id=self.parent_sub_agent_id,
            workflow_execution_id=self.workflow_execution_id,
            phase_attempt_id=self.phase_attempt_id,
            trigger_message_id=self.trigger_message_id,
            model_invocation_id=self.model_invocation_id,
            tool_invocation_id=self.tool_invocation_id,
            idempotency_key=self.idempotency_key,
            allowed_tools=allowed_tools,
            intent_epoch=self.intent_epoch,
            phase_executor=self.phase_executor,
        )

    def as_dict(self) -> dict[str, object]:
        """Return the canonical, credential-free attribution mapping."""
        return {
            "workspaceId": self.workspace_id,
            "chatId": self.chat_id,
            "conversationId": self.conversation_id,
            "actorId": self.actor_id,
            "subAgentId": self.sub_agent_id,
            "parentSubAgentId": self.parent_sub_agent_id,
            "workflowExecutionId": self.workflow_execution_id,
            "phaseAttemptId": self.phase_attempt_id,
            "triggerMessageId": self.trigger_message_id,
            "modelInvocationId": self.model_invocation_id,
            "toolInvocationId": self.tool_invocation_id,
            "idempotencyKey": self.idempotency_key,
            "intentEpoch": self.intent_epoch,
            "phaseExecutor": self.phase_executor,
        }


__all__ = [
    "ToolContext",
    "ToolContextError",
]
