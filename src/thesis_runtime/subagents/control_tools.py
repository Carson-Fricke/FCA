"""Thesis-owned chat control tools: spawn, completion and operator input.

Each handler closes over the immutable
:class:`~thesis_runtime.contracts.context.ToolContext` built by the runtime, so
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

from thesis_runtime.contracts.context import ToolContext
from thesis_runtime.contracts.datum import DatumType, datum_from_result
from thesis_runtime.contracts.records import SubAgentKind, SubAgentRecord
from thesis_runtime.subagents.control import (
    CONTROL_TOOL_SPECS,
    TOOL_REQUEST_OPERATOR_INPUT,
    TOOL_REQUEST_PHASE_COMPLETION,
    TOOL_REQUEST_SUB_AGENT_COMPLETION,
    TOOL_REQUEST_WORKFLOW_INPUT,
    TOOL_SPAWN_SUB_AGENT,
    ControlToolSpec,
    OperatorInputRequest,
    PhaseCompletionRejected,
    SpawnResult,
    StaleCompletionError,
)
from thesis_runtime.subagents.policy import SpawnDenial, SpawnPolicy
from thesis_runtime.subagents.specs import SubAgentSpec
from thesis_runtime.tools.registry import RegisteredTool, ToolExecution

#: Notified after a child is durably created so a runner can execute it.
SpawnObserver = Callable[[SubAgentRecord, str], None]

#: Control tools the top-level chat conversation may call.
CONTROL_CHAT_TOOL_NAMES: tuple[str, ...] = (
    TOOL_SPAWN_SUB_AGENT,
    TOOL_REQUEST_OPERATOR_INPUT,
)

#: Control tools only a sub-agent/phase conversation may call: the caller
#: returns data to its own caller and the node goes terminal.
CONTROL_NODE_TOOL_NAMES: tuple[str, ...] = (
    TOOL_REQUEST_PHASE_COMPLETION,
    TOOL_REQUEST_SUB_AGENT_COMPLETION,
)

#: The structural control tools every workflow phase agent carries: pausing
#: for operator input and handing the phase back through its completion
#: contract. Delegation tools are not structural; a phase agent receives them
#: only when its own configuration adds them.
CONTROL_PHASE_TOOL_NAMES: tuple[str, ...] = (
    TOOL_REQUEST_WORKFLOW_INPUT,
    TOOL_REQUEST_PHASE_COMPLETION,
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
        prompt: str,
        parent_sub_agent_id: str | None = None,
        workflow_execution_id: str | None = None,
        phase_attempt_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> SpawnResult: ...

    def request_operator_input(
        self,
        workspace_id: str,
        sub_agent_id: str,
        *,
        message: str,
        reason: str = "",
    ) -> OperatorInputRequest: ...

    def complete_sub_agent(
        self,
        workspace_id: str,
        sub_agent_id: str,
        *,
        result: Mapping[str, object],
    ) -> SubAgentRecord: ...

    def complete_phase(
        self,
        workspace_id: str,
        sub_agent_id: str,
        *,
        completion: Mapping[str, object],
        expected_attempt_id: str | None,
    ) -> SubAgentRecord: ...


def _function_tool(spec: ControlToolSpec) -> dict[str, object]:
    return {
        "name": spec.name,
        "description": spec.description,
        "parameters": dict(spec.input_schema),
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
    phase_completion_schema: Mapping[str, object] | None = None,
) -> tuple[RegisteredTool, ...]:
    """Return the conversation-scoped control tools bound to ``runtime``.

    The chat scope and a node scope differ exactly in the caller's ability to
    return data to its own caller.  The two node routes are disjoint by
    construction: a workflow phase node is advertised
    ``request_phase_completion`` (its result is the phase contract the workflow
    validates and applies), and every other node is advertised
    ``request_sub_agent_completion`` (its result is a plain sub-agent result a
    chat caller can consume).  ``phase_completion_schema`` is the owning
    phase's declared completion JSON Schema; when supplied, the
    ``request_phase_completion`` tool advertises it so the provider
    constrains the contract at decode time — the engine still validates the
    accepted call deterministically.
    """

    def handle_spawn(
        arguments: Mapping[str, object], context: ToolContext
    ) -> ToolExecution:
        spec = _spawn_spec(arguments)
        if isinstance(spec, str):
            return _failure(TOOL_SPAWN_SUB_AGENT, spec)
        authorization: Mapping[str, object] | None = None
        if policy is not None:
            decision = policy.authorize(
                requested_role=spec.role,
                requested_tools=spec.allowed_tools,
                requested_budget=spec.delegated_budget,
                # An unconfigured caller policy means there is no set to narrow
                # against; an explicit policy passes its exact entries.
                caller_tools=context.allowed_tools or (),
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
            prompt=task,
            parent_sub_agent_id=context.sub_agent_id,
            workflow_execution_id=context.workflow_execution_id,
            phase_attempt_id=context.phase_attempt_id,
            idempotency_key=context.idempotency_key,
        )
        if on_spawn is not None:
            on_spawn(result.sub_agent, task)
        structured = dict(result.as_dict())
        # The handle the caller's transcript renders: the child's identity and
        # delegated task travel with the datum so the spawn card can present
        # the child without a second lookup.
        structured["role"] = spec.role
        structured["task"] = task
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

    def handle_operator_input(
        arguments: Mapping[str, object], context: ToolContext
    ) -> ToolExecution:
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
        structured: dict[str, object] = {
            "subAgentId": context.sub_agent_id,
            "question": message.strip(),
            "reason": resolved_reason,
        }
        if context.sub_agent_id is not None:
            # The asking node owns the pause: the unified input path moves the
            # node to needs_input so the operator's answer resumes this same
            # conversation.  The question itself renders as this tool result at
            # the caller's anchor; no chat message is published.
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

    def handle_request_sub_agent_completion(
        arguments: Mapping[str, object],
        context: ToolContext,
    ) -> ToolExecution:
        return _handle_completion(arguments, TOOL_REQUEST_SUB_AGENT_COMPLETION)

    def handle_request_phase_completion(
        arguments: Mapping[str, object],
        context: ToolContext,
    ) -> ToolExecution:
        return _handle_completion(arguments, TOOL_REQUEST_PHASE_COMPLETION)

    def _handle_completion(arguments: Mapping[str, object], tool: str) -> ToolExecution:
        sub_agent_id = context.sub_agent_id
        if sub_agent_id is None:
            # The completion tools are only meaningful for a node conversation:
            # a caller returns data to its own caller, never to the chat.
            return _failure(tool, "completion requires a sub-agent caller")
        completion = arguments.get("completion")
        if not isinstance(completion, Mapping) or not completion:
            return _failure(tool, "completion must be a non-empty JSON object")
        stamped = {**dict(completion), "intentEpoch": context.intent_epoch}
        try:
            if tool == TOOL_REQUEST_PHASE_COMPLETION:
                record = runtime.complete_phase(
                    context.workspace_id,
                    sub_agent_id,
                    completion=stamped,
                    expected_attempt_id=context.phase_attempt_id,
                )
            else:
                record = runtime.complete_sub_agent(
                    context.workspace_id, sub_agent_id, result=stamped
                )
        except (StaleCompletionError, PhaseCompletionRejected) as error:
            return _failure(tool, str(error))
        structured: dict[str, object] = {
            "subAgentId": record.sub_agent_id,
            "status": record.status.value,
            "result": dict(record.result or {}),
        }
        return ToolExecution(
            ok=True,
            structured=structured,
            text="Completion recorded; the result was delivered to the caller.",
            terminal=True,
            datums=(
                datum_from_result(
                    DatumType.SUB_AGENT_STATUS.value,
                    ok=True,
                    structured=structured,
                ),
            ),
        )

    # A workflow phase agent is offered the wrapped operator-input tool instead
    # of the plain one: identical behavior, and it flags the workflow card so
    # the phase shows as awaiting operator input. Handled by the same handler.
    in_workflow = context.workflow_execution_id is not None
    operator_tool = (
        _SPECS[TOOL_REQUEST_WORKFLOW_INPUT]
        if in_workflow
        else _SPECS[TOOL_REQUEST_OPERATOR_INPUT]
    )
    completion_spec = _SPECS[TOOL_REQUEST_PHASE_COMPLETION]
    if phase_completion_schema is not None:
        completion_spec = ControlToolSpec(
            name=TOOL_REQUEST_PHASE_COMPLETION,
            description=completion_spec.description,
            input_schema={
                "type": "object",
                "properties": {"completion": dict(phase_completion_schema)},
                "required": ["completion"],
            },
        )
    tools = [
        RegisteredTool(
            descriptor=_function_tool(_SPECS[TOOL_SPAWN_SUB_AGENT]),
            handler=handle_spawn,
        ),
        RegisteredTool(
            descriptor=_function_tool(operator_tool),
            handler=handle_operator_input,
            operation="interact",
        ),
    ]
    if context.sub_agent_id is not None:
        if context.phase_executor:
            # The workflow owns this node's completion: only the phase contract
            # is advertised, never the plain sub-agent return.
            tools.append(
                RegisteredTool(
                    descriptor=_function_tool(completion_spec),
                    handler=handle_request_phase_completion,
                )
            )
        else:
            tools.append(
                RegisteredTool(
                    descriptor=_function_tool(
                        _SPECS[TOOL_REQUEST_SUB_AGENT_COMPLETION]
                    ),
                    handler=handle_request_sub_agent_completion,
                )
            )
    return tuple(tools)


__all__ = [
    "CONTROL_CHAT_TOOL_NAMES",
    "CONTROL_NODE_TOOL_NAMES",
    "CONTROL_PHASE_TOOL_NAMES",
    "ControlToolRuntime",
    "SpawnObserver",
    "control_registered_tools",
]
