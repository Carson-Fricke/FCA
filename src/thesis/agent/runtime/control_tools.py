"""Thesis-owned chat control tools: spawn, await, and operator input.

Each handler closes over the immutable
:class:`~thesis.agent.runtime.context.ToolContext` built by the runtime, so
workspace, chat, conversation, parent, and actor attribution are runtime-owned
and cannot be supplied or overridden by the model.  A spawned child is created
through the same durable ``spawn_sub_agent`` command contract used by workflow
phases; the immediate result is the child handle, and the child runs
asynchronously.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Protocol

from thesis.agent.runtime.context import ToolContext
from thesis.agent.runtime.control import (
    CONTROL_TOOL_SPECS,
    TOOL_AWAIT_SUB_AGENT,
    TOOL_REQUEST_OPERATOR_INPUT,
    TOOL_SPAWN_SUB_AGENT,
    AwaitResult,
    ControlToolSpec,
    OperatorInputRequest,
    SpawnResult,
)
from thesis.agent.runtime.datum import DatumType, datum_from_result
from thesis.agent.runtime.policy import SpawnDenial, SpawnPolicy
from thesis.agent.runtime.session import RegisteredTool, ToolExecution
from thesis.agent.runtime.specs import SubAgentSpec
from thesis.chat.contracts import SubAgentKind, SubAgentRecord

#: Notified after a child is durably created so a runner can execute it.
SpawnObserver = Callable[[SubAgentRecord, str], None]

#: Publishes an operator question for an agent with no sub-agent record.
OperatorQuestionPublisher = Callable[[str, str, str, str], Mapping[str, object]]

CONTROL_CHAT_TOOL_NAMES: tuple[str, ...] = (
    TOOL_SPAWN_SUB_AGENT,
    TOOL_AWAIT_SUB_AGENT,
    TOOL_REQUEST_OPERATOR_INPUT,
)

_SPECS: dict[str, ControlToolSpec] = {spec.name: spec for spec in CONTROL_TOOL_SPECS}


class ControlToolRuntime(Protocol):
    """The durable control surface the handlers dispatch to."""

    def spawn(
        self,
        workspace_id: str,
        chat_id: str,
        *,
        spec: SubAgentSpec,
        actor_id: str,
        conversation_id: str,
        parent_sub_agent_id: str | None = None,
        workflow_execution_id: str | None = None,
        phase_attempt_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> SpawnResult: ...

    def await_sub_agent(self, workspace_id: str, sub_agent_id: str) -> AwaitResult: ...

    def request_operator_input(
        self,
        workspace_id: str,
        sub_agent_id: str,
        *,
        message: str,
        reason: str = "",
    ) -> OperatorInputRequest: ...


def _function_tool(spec: ControlToolSpec) -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": dict(spec.input_schema),
        },
    }


def _failure(tool: str, message: str) -> ToolExecution:
    return ToolExecution(
        ok=False, failure={"kind": "tool", "message": message, "tool": tool}
    )


def _spawn_spec(arguments: Mapping[str, object]) -> SubAgentSpec | str:
    role = arguments.get("role")
    if not isinstance(role, str) or not role.strip():
        return "role must be non-empty text"
    task = arguments.get("task")
    if not isinstance(task, str) or not task.strip():
        return "task must be non-empty text"
    kind_value = arguments.get("kind", SubAgentKind.CONVERSATION.value)
    if not isinstance(kind_value, str):
        return "kind must be 'conversation' or 'one_shot'"
    try:
        kind = SubAgentKind(kind_value)
    except ValueError:
        return "kind must be 'conversation' or 'one_shot'"
    allowed = arguments.get("allowed_tools", ())
    if not isinstance(allowed, (list, tuple)) or any(
        not isinstance(item, str) or not item.strip() for item in allowed
    ):
        return "allowed_tools must be a list of non-empty tool names"
    budget = arguments.get("delegated_budget", 0)
    if isinstance(budget, bool) or not isinstance(budget, int) or budget < 0:
        return "delegated_budget must be a non-negative integer"
    return SubAgentSpec(
        role=role.strip(),
        kind=kind,
        allowed_tools=tuple(item.strip() for item in allowed),
        delegated_budget=budget,
    )


def control_registered_tools(
    runtime: ControlToolRuntime,
    context: ToolContext,
    *,
    on_spawn: SpawnObserver | None = None,
    policy: SpawnPolicy | None = None,
    publish_operator_question: OperatorQuestionPublisher | None = None,
) -> tuple[RegisteredTool, ...]:
    """Return the chat-scoped control tools bound to ``runtime`` and ``context``."""

    def handle_spawn(arguments: Mapping[str, object]) -> ToolExecution:
        spec = _spawn_spec(arguments)
        if isinstance(spec, str):
            return _failure(TOOL_SPAWN_SUB_AGENT, spec)
        authorization: Mapping[str, object] | None = None
        if policy is not None:
            decision = policy.authorize(
                requested_role=spec.role,
                requested_tools=spec.allowed_tools,
                requested_budget=spec.delegated_budget,
                caller_tools=context.allowed_tools,
                parent_sub_agent_id=context.sub_agent_id,
            )
            if isinstance(decision, SpawnDenial):
                return ToolExecution(
                    ok=False,
                    failure={
                        "kind": "denied",
                        "code": decision.code,
                        "message": decision.message,
                        "tool": TOOL_SPAWN_SUB_AGENT,
                    },
                )
            spec = replace(
                spec,
                role=decision.role,
                allowed_tools=decision.allowed_tools,
                delegated_budget=decision.delegated_budget,
            )
            authorization = decision.as_dict()
        task = str(arguments["task"]).strip()
        result = runtime.spawn(
            context.workspace_id,
            context.chat_id,
            spec=spec,
            actor_id=context.actor_id,
            conversation_id=context.conversation_id,
            parent_sub_agent_id=context.sub_agent_id,
            workflow_execution_id=context.workflow_execution_id,
            phase_attempt_id=context.phase_attempt_id,
            idempotency_key=context.idempotency_key,
        )
        if on_spawn is not None:
            on_spawn(result.sub_agent, task)
        structured = dict(result.as_dict())
        if authorization is not None:
            structured["authorization"] = authorization
        return ToolExecution(
            ok=True,
            structured=structured,
            text="Sub-agent spawned; it runs asynchronously.",
            datums=(
                datum_from_result(
                    DatumType.SUB_AGENT_HANDLE.value,
                    ok=True,
                    structured=structured,
                    text="Sub-agent spawned; it runs asynchronously.",
                ),
            ),
        )

    def handle_await(arguments: Mapping[str, object]) -> ToolExecution:
        sub_agent_id = arguments.get("sub_agent_id")
        if not isinstance(sub_agent_id, str) or not sub_agent_id.strip():
            return _failure(TOOL_AWAIT_SUB_AGENT, "sub_agent_id must be non-empty")
        result = runtime.await_sub_agent(context.workspace_id, sub_agent_id.strip())
        structured = dict(result.as_dict())
        return ToolExecution(
            ok=True,
            structured=structured,
            datums=(
                datum_from_result(
                    DatumType.SUB_AGENT_STATUS.value, ok=True, structured=structured
                ),
            ),
        )

    def handle_operator_input(arguments: Mapping[str, object]) -> ToolExecution:
        message = arguments.get("message")
        if not isinstance(message, str) or not message.strip():
            return _failure(TOOL_REQUEST_OPERATOR_INPUT, "message must be non-empty")
        reason = arguments.get("reason")
        if reason is not None and (not isinstance(reason, str) or not reason.strip()):
            return _failure(
                TOOL_REQUEST_OPERATOR_INPUT,
                "reason must be non-empty text when set",
            )
        resolved_reason = "" if reason is None else reason
        if context.sub_agent_id is None:
            # The top-level chat is itself the operator's conversation: asking a
            # question needs no sub-agent record.  The question is published
            # durably and the turn ends so the operator can answer it.
            if publish_operator_question is None:
                return _failure(
                    TOOL_REQUEST_OPERATOR_INPUT,
                    "operator input requires a configured chat question surface",
                )
            published = publish_operator_question(
                context.workspace_id,
                context.chat_id,
                message.strip(),
                resolved_reason,
            )
            structured = dict(published)
            return ToolExecution(
                ok=True,
                structured=structured,
                text="Operator input requested; this turn is paused.",
                terminal=True,
                datums=(
                    datum_from_result(
                        DatumType.OPERATOR_QUESTION.value,
                        ok=True,
                        structured=structured,
                        text=message.strip(),
                    ),
                ),
            )
        request = runtime.request_operator_input(
            context.workspace_id,
            context.sub_agent_id,
            message=message.strip(),
            reason=resolved_reason,
        )
        structured = dict(request.as_dict())
        return ToolExecution(
            ok=True,
            structured=structured,
            text="Operator input requested; this turn is paused.",
            terminal=True,
            datums=(
                datum_from_result(
                    DatumType.OPERATOR_QUESTION.value,
                    ok=True,
                    structured=structured,
                    text=message.strip(),
                ),
            ),
        )

    return (
        RegisteredTool(
            spec=_function_tool(_SPECS[TOOL_SPAWN_SUB_AGENT]),
            handler=handle_spawn,
        ),
        RegisteredTool(
            spec=_function_tool(_SPECS[TOOL_AWAIT_SUB_AGENT]),
            handler=handle_await,
        ),
        RegisteredTool(
            spec=_function_tool(_SPECS[TOOL_REQUEST_OPERATOR_INPUT]),
            handler=handle_operator_input,
        ),
    )


__all__ = [
    "CONTROL_CHAT_TOOL_NAMES",
    "ControlToolRuntime",
    "OperatorQuestionPublisher",
    "SpawnObserver",
    "control_registered_tools",
]
