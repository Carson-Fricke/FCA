"""Outgoing model envelopes and runtime activity persistence; never incoming delivery."""

import json
import time
import uuid
from collections.abc import Callable, Mapping

from thesis_runtime.contracts.activity import ModelOutput, RuntimeTool, activity_data
from thesis_runtime.contracts.context import ToolContext
from thesis_runtime.contracts.datum import tool_result_payload
from thesis_runtime.contracts.messages import MessageContent, SessionMessage
from thesis_runtime.contracts.records import (
    ChatMessage,
    MessageRole,
    RecordNotFound,
    TranscriptItemKind,
)
from thesis_runtime.conversations.delivery import Anchor
from thesis_runtime.conversations.service import ChatService
from thesis_runtime.sessions.session import SessionTurn
from thesis_runtime.subagents.store import SubAgentStore
from thesis_runtime.tools.registry import SessionPersistenceError, ToolExecution


def persist_turn(
    chat: ChatService,
    store: SubAgentStore,
    context: ToolContext,
    turn: SessionTurn,
    *,
    activity: str | None = None,
) -> tuple[ChatMessage, ...]:
    """Persist outgoing envelopes and runtime start activity, never inputs."""
    if turn.message.direction != "outgoing":
        return ()
    content = turn.message.content
    activity_call_id = (content.result or {}).get("toolCallId")
    reasoning = (content.result or {}).get("reasoning")
    root = context.sub_agent_id is None
    metadata = (
        RuntimeTool(
            context.sub_agent_id,
            "started" if activity == "started" else "finished",
            activity_call_id if isinstance(activity_call_id, str) else None,
        )
        if activity is not None
        else ModelOutput(context.sub_agent_id, turn.invocation_id)
    )
    data = {"turn": "assistant", **activity_data(metadata)}
    if activity is None:
        data["sessionMessage"] = turn.message.as_dict()
    if turn.started_at is not None:
        data["startedAt"] = turn.started_at.isoformat()
    if activity is not None:
        data["ownerActorId"] = context.actor_id
        data["workflowExecutionId"] = context.workflow_execution_id
        record = (
            None
            if root
            else store.get_sub_agent(context.workspace_id, context.sub_agent_id or "")
        )
        if record is not None and record.chat_id == context.chat_id:
            data["phase"] = record.phase
    messages: list[ChatMessage] = []

    if activity == "started":
        messages.append(
            chat.create_message(
                context.workspace_id,
                context.chat_id,
                actor_id=context.actor_id,
                body=f"Running {content.source}",
                role=MessageRole.TOOL,
                kind=TranscriptItemKind.TOOL if root else TranscriptItemKind.SUB_AGENT,
                execution_id=context.workflow_execution_id,
                data={
                    **data,
                    "tool": content.source,
                    "arguments": dict(turn.arguments or {}),
                },
            )
        )
    elif content.text.strip() or content.requests:
        messages.append(
            chat.create_message(
                context.workspace_id,
                context.chat_id,
                actor_id=context.actor_id,
                message_id=turn.message.message_id,
                body=content.text.strip() or "Tool calls requested",
                reasoning=reasoning if isinstance(reasoning, str) else None,
                role=MessageRole.ASSISTANT,
                kind=TranscriptItemKind.MESSAGE
                if root
                else TranscriptItemKind.SUB_AGENT,
                execution_id=context.workflow_execution_id,
                data=data,
            )
        )
    return tuple(messages)


def execute_runtime_tool(
    chat: ChatService,
    store: SubAgentStore,
    deliver: Callable[..., ChatMessage],
    context: ToolContext,
    tool: str,
    arguments: Mapping[str, object],
    execute: Callable[[], ToolExecution],
) -> ToolExecution:
    """Record an internal subcall without inventing a provider-call envelope."""
    from datetime import UTC, datetime

    call_id = f"runtime-{uuid.uuid4().hex}"
    started_at = datetime.now(UTC)
    started = time.monotonic()
    try:
        persist_turn(
            chat,
            store,
            context,
            SessionTurn(
                message=SessionMessage(
                    f"activity-{uuid.uuid4().hex}",
                    "outgoing",
                    MessageContent("", tool, result={"toolCallId": call_id}),
                ),
                started_at=started_at,
                arguments=arguments,
            ),
            activity="started",
        )
    except Exception as error:
        raise SessionPersistenceError(
            "could not persist runtime tool activity"
        ) from error
    if not chat.admit_conversation_dispatch(context.workspace_id, context.chat_id):
        raise RecordNotFound("conversation has been deleted")
    result = execute()
    try:
        payload = json.loads(result.as_content())
        payload.update(
            datums=[datum.as_dict() for datum in result.datums],
            startedAt=started_at.isoformat(),
            durationMs=int((time.monotonic() - started) * 1000),
            arguments=dict(arguments),
            toolCallId=call_id,
        )
        deliver(
            Anchor(context.workspace_id, context.chat_id, context.sub_agent_id),
            SessionMessage(
                f"message-{uuid.uuid4().hex}",
                "incoming",
                MessageContent(result.text, "runtime", result=payload),
            ),
            actor_id=context.actor_id,
            body=result.text or f"{tool} result",
            tool_result=tool_result_payload(
                call_id=call_id,
                tool=tool,
                datum_type=tool,
                ok=result.ok,
                structured=result.structured,
                text=result.text,
                summary=result.text or f"{tool} result",
                failure=result.failure,
            ),
            execution_id=context.workflow_execution_id,
            schedule=False,
        )
    except Exception as error:
        raise SessionPersistenceError(
            "could not persist runtime tool activity"
        ) from error
    return result
