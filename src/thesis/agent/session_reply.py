"""Adapt the durable :class:`AgentSession` turn loop to the chat reply surface.

The top-level chat reply runs through the same unified session used by
chat-spawned sub-agents and workflow phase agents: it sends the merged harness,
control, and workflow tool catalog, persists each model and tool invocation, and
threads tool results back as ``tool`` turns.  This module projects the session's
durable turns onto the streamed :data:`ReplyProvider` surface the chat service
already consumes, so answer text, tool outcomes, and persistence are unchanged.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence

from thesis.agent.runtime.session import (
    DEFAULT_MAX_TOOL_ITERATIONS,
    AgentSession,
    CompletionProvider,
    SessionResult,
    SessionToolRegistry,
    SessionTurn,
)
from thesis.agent.runtime.store import SubAgentStore
from thesis.chat.contracts import ChatMessage
from thesis.chat.responder import ReplyChunk, ToolResultChunk
from thesis.providers.invocations import ChatMessage as ProviderChatMessage
from thesis.providers.invocations import Role

#: System prompt for the unified top-level chat agent.
AGENT_SYSTEM_PROMPT = (
    "You are the Thesis research agent. Use the available tools to execute "
    "deterministic research: start a workflow with start_workflow when the "
    "operator asks to develop or falsify an idea, spawn a helper with "
    "spawn_sub_agent for independent work, and call the quant-harness tools "
    "directly for backtests, studies, sweeps, and artifacts. A spawned helper "
    "runs asynchronously: call await_sub_agent to wait for its result, or end "
    "your turn and its result is delivered to this conversation when it "
    "finishes. Rely on the "
    "structured tool results as evidence and surface any failure instead of "
    "assuming success. Workflow phase transitions are owned by the workflow, "
    "never by you. "
    "Starting a workflow hands the work to it: it routes its own phases and "
    "data, asks the operator for anything it needs from inside the phase that "
    "needs it, and reports back as its own configuration dictates (some "
    "workflows report only at terminal, others report throughout their "
    "lifecycle). After start_workflow "
    "returns, reply with one short sentence saying you started it and stop "
    "there. Do not gather or prepare the workflow's inputs, do not spawn "
    "helpers to look for them, do not relay or answer the workflow's requests, "
    "and do not perform any of its work."
)

_CHAT_ROLES = frozenset(("user", "assistant", "system", "tool"))


def _role_name(role: object) -> str:
    value = getattr(role, "value", role)
    return value if isinstance(value, str) else str(role)


def _seed_messages(
    history: Sequence[ChatMessage], system_prompt: str
) -> list[ProviderChatMessage]:
    messages: list[ProviderChatMessage] = []
    if system_prompt:
        messages.append(ProviderChatMessage(role="system", content=system_prompt))
    for message in history:
        role = _role_name(message.role)
        body = message.body.strip()
        if role == "tool":
            # Root tool results lack their assistant tool-call envelope, so
            # replaying them would make the provider reject the request.
            # The assistant's summary already reflects what happened.
            continue
        if role in _CHAT_ROLES and body:
            messages.append(ProviderChatMessage(role=role, content=body))
    return messages


def _chunks(text: str, chunk_size: int) -> Iterator[str]:
    for index in range(0, len(text), chunk_size):
        yield text[index : index + chunk_size]


def _tool_chunks(turn: SessionTurn) -> Iterator[ToolResultChunk]:
    """Project one tool turn into one chat chunk per returned datum."""
    tool = turn.tool_name or "tool"
    if turn.datums:
        for datum in turn.datums:
            yield ToolResultChunk(
                tool=tool,
                call_id=turn.tool_call_id,
                ok=datum.ok,
                summary=(
                    datum.summary
                    or (datum.text.strip() if datum.text.strip() else None)
                    or f"{tool} result"
                ),
                structured=datum.structured,
                text=datum.text,
                failure=datum.failure,
                datum_type=datum.datum_type,
            )
        return
    payload: Mapping[str, object] = {}
    try:
        decoded = json.loads(turn.content)
    except ValueError:
        decoded = None
    if isinstance(decoded, Mapping):
        payload = decoded
    structured = payload.get("structured")
    failure = payload.get("failure")
    text = payload.get("text")
    summary = text if isinstance(text, str) and text.strip() else None
    yield ToolResultChunk(
        tool=tool,
        call_id=turn.tool_call_id,
        ok=bool(payload.get("ok")),
        summary=summary or f"{tool} result",
        structured=structured if isinstance(structured, Mapping) else {},
        text=text if isinstance(text, str) else "",
        failure=failure if isinstance(failure, Mapping) else None,
    )


class SessionReplyProvider:
    """Run one top-level chat turn through the durable agent session."""

    def __init__(
        self,
        *,
        workspace_id: str,
        chat_id: str,
        conversation_id: str,
        provider: CompletionProvider,
        registry: SessionToolRegistry,
        store: SubAgentStore | None = None,
        actor_id: str = "thesis-agent",
        role: Role = "primary",
        system_prompt: str = AGENT_SYSTEM_PROMPT,
        chunk_size: int = 24,
        max_tool_iterations: int = DEFAULT_MAX_TOOL_ITERATIONS,
        sub_agent_id: str | None = None,
    ) -> None:
        if chunk_size < 1:
            raise ValueError("chunk_size must be a positive integer")
        self._workspace_id = workspace_id
        self._chat_id = chat_id
        self._conversation_id = conversation_id
        self._provider = provider
        self._registry = registry
        self._store = store
        self._actor_id = actor_id
        self._role = role
        self._system_prompt = system_prompt
        self._chunk_size = chunk_size
        self._max_tool_iterations = max_tool_iterations
        self._sub_agent_id = sub_agent_id

    def __call__(
        self, history: Sequence[ChatMessage]
    ) -> Iterator[str | ReplyChunk | ToolResultChunk]:
        messages = _seed_messages(history, self._system_prompt)
        if not messages:
            return
        session = AgentSession(
            workspace_id=self._workspace_id,
            chat_id=self._chat_id,
            conversation_id=self._conversation_id,
            provider=self._provider,
            registry=self._registry,
            store=self._store,
            sub_agent_id=self._sub_agent_id,
            actor_id=self._actor_id,
            role=self._role,
            max_tool_iterations=self._max_tool_iterations,
        )
        yield from self._stream(session.run(messages))

    def _stream(
        self, result: SessionResult
    ) -> Iterator[str | ReplyChunk | ToolResultChunk]:
        for turn in result.turns:
            if turn.role == "assistant" and turn.content:
                yield from _chunks(turn.content, self._chunk_size)
            elif turn.role == "tool":
                yield from _tool_chunks(turn)


__all__ = [
    "AGENT_SYSTEM_PROMPT",
    "SessionReplyProvider",
]
