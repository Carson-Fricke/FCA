"""Harness-aware chat reply provider for the Thesis agent.

The plain reply provider performs one completion and streams the text.  This
module adds a bounded function-calling loop over the quant-harness MCP tool
catalog so the agent can actually execute research:

* :func:`harness_agent_reply_provider` converts the discovered
  :class:`~thesis.agent.harness_tools.HarnessToolSpec` catalog into OpenAI
  function tools, sends it with every completion, dispatches any
  ``message.tool_calls`` through the :class:`HarnessToolProvider`, threads the
  structured results back as ``role: tool`` messages, and repeats until the
  model answers in text.
* :func:`reply_provider_for` selects the tool-aware provider when a harness tool
  surface is available and falls back to the plain provider otherwise, so an
  unavailable MCP server degrades to a normal reply instead of blocking startup
  or telling the operator it cannot see the harness.

Tool failures are never raised into the reply: transport, protocol, unknown
tool, and denial results are rendered as the tool message content so the model
can recover.  Model reasoning is read as a separate channel: it is streamed
before the answer with ``kind: "reasoning"`` and never enters the answer text.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence

from thesis.agent.harness_tools import (
    HarnessToolProvider,
    HarnessToolResult,
    HarnessToolSpec,
)
from thesis.chat.contracts import ChatMessage
from thesis.chat.responder import ReplyChunk, ReplyProvider
from thesis.providers.invocations import ChatMessage as ProviderChatMessage
from thesis.providers.invocations import Role, ToolCall
from thesis.providers.openrouter import OpenRouterProvider
from thesis.providers.transports import (
    DEFAULT_SYSTEM_PROMPT,
    HARNESS_TOOL_SYSTEM_PROMPT,
    openrouter_reply_provider,
)

_CHAT_ROLES = frozenset(("user", "assistant", "system", "tool"))

#: How many model/tool round trips to allow before forcing a text answer.
DEFAULT_MAX_TOOL_ITERATIONS = 6


def _chunks(text: str, chunk_size: int) -> Iterator[str]:
    for index in range(0, len(text), chunk_size):
        yield text[index : index + chunk_size]


def _reasoning_chunks(text: str, chunk_size: int) -> Iterator[ReplyChunk]:
    for index in range(0, len(text), chunk_size):
        yield ReplyChunk(text=text[index : index + chunk_size], kind="reasoning")


def _answer_stream(
    reasoning: str, text: str, chunk_size: int
) -> Iterator[str | ReplyChunk]:
    """Emit reasoning chunks first, then the answer text chunks."""
    yield from _reasoning_chunks(reasoning, chunk_size)
    yield from _chunks(text, chunk_size)


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
        if role in _CHAT_ROLES and body:
            messages.append(ProviderChatMessage(role=role, content=body))
    return messages


def _tool_catalog(
    tool_provider: HarnessToolProvider,
) -> tuple[dict[str, object], ...]:
    catalog: list[dict[str, object]] = []
    for spec in tool_provider.list_tools():
        catalog.append(_function_tool(spec))
    return tuple(catalog)


def _function_tool(spec: HarnessToolSpec) -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": spec.name,
            "description": spec.description,
            "parameters": dict(spec.input_schema),
        },
    }


def _result_text(result: HarnessToolResult) -> str:
    payload: dict[str, object] = {
        "tool": result.tool,
        "ok": result.ok,
        "structured": dict(result.structured),
        "text": result.text,
    }
    if result.failure is not None:
        payload["failure"] = result.failure.as_dict()
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)


def _error_text(call: ToolCall, message: str) -> str:
    return json.dumps(
        {"tool": call.name, "ok": False, "error": message},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _dispatch(tool_provider: HarnessToolProvider, call: ToolCall) -> str:
    """Execute one model tool call, always returning tool-message content."""
    raw = call.arguments.strip()
    if raw:
        try:
            arguments = json.loads(raw)
        except ValueError:
            return _error_text(call, "tool arguments were not valid JSON")
    else:
        arguments = {}
    if not isinstance(arguments, Mapping):
        return _error_text(call, "tool arguments must be a JSON object")
    try:
        result = tool_provider.call_tool(call.name, dict(arguments))
    except Exception as error:  # a tool fault must not fail the whole reply
        return _error_text(call, f"tool call failed: {error}")
    return _result_text(result)


def harness_agent_reply_provider(
    provider: OpenRouterProvider,
    tool_provider: HarnessToolProvider,
    *,
    system_prompt: str = HARNESS_TOOL_SYSTEM_PROMPT,
    chunk_size: int = 24,
    max_tool_iterations: int = DEFAULT_MAX_TOOL_ITERATIONS,
    role: Role = "primary",
) -> ReplyProvider:
    """Adapt an OpenRouter provider plus a harness tool surface into a reply.

    The returned callable runs a bounded loop: complete with the tool catalog,
    dispatch any requested tools, append their results as ``role: tool``
    messages, and repeat.  When the model answers in text (or the iteration cap
    is reached) the final text is streamed in deterministic chunks.  A missing
    or broken tool catalog degrades to a plain, tool-free completion.
    """
    if chunk_size < 1:
        raise ValueError("chunk_size must be a positive integer")
    if max_tool_iterations < 1:
        raise ValueError("max_tool_iterations must be a positive integer")

    def stream(history: Sequence[ChatMessage]) -> Iterator[str | ReplyChunk]:
        messages = _seed_messages(history, system_prompt)
        if not messages:
            return
        try:
            catalog = _tool_catalog(tool_provider)
        except Exception:
            catalog = ()
        if not catalog:
            completion = provider.complete(messages=messages, role=role)
            yield from _answer_stream(
                completion.reasoning, completion.content, chunk_size
            )
            return
        reasoning_parts: list[str] = []
        for _ in range(max_tool_iterations):
            completion = provider.complete(messages=messages, role=role, tools=catalog)
            if completion.reasoning:
                reasoning_parts.append(completion.reasoning)
            if not completion.tool_calls:
                yield from _answer_stream(
                    "".join(reasoning_parts), completion.content, chunk_size
                )
                return
            messages.append(
                ProviderChatMessage(
                    role="assistant",
                    content=completion.content,
                    tool_calls=completion.tool_calls,
                )
            )
            for call in completion.tool_calls:
                messages.append(
                    ProviderChatMessage(
                        role="tool",
                        content=_dispatch(tool_provider, call),
                        tool_call_id=call.id,
                    )
                )
        # The cap was reached with tools still pending: ask once more without
        # the catalog so the operator always receives a text answer.
        completion = provider.complete(messages=messages, role=role)
        if completion.reasoning:
            reasoning_parts.append(completion.reasoning)
        yield from _answer_stream(
            "".join(reasoning_parts), completion.content, chunk_size
        )

    return stream


def reply_provider_for(
    provider: OpenRouterProvider,
    tool_provider: HarnessToolProvider | None = None,
    *,
    system_prompt: str | None = None,
    chunk_size: int = 24,
    max_tool_iterations: int = DEFAULT_MAX_TOOL_ITERATIONS,
) -> ReplyProvider:
    """Select the tool-aware reply provider, or the plain one as a fallback.

    A ``None`` tool surface yields the single-shot provider with the hardened
    default prompt.  A supplied surface yields the harness tool-calling provider
    with the tool-aware prompt; if its catalog is unusable at request time the
    loop still answers without tools rather than failing the reply.
    """
    if tool_provider is None:
        return openrouter_reply_provider(
            provider,
            system_prompt=system_prompt or DEFAULT_SYSTEM_PROMPT,
            chunk_size=chunk_size,
        )
    return harness_agent_reply_provider(
        provider,
        tool_provider,
        system_prompt=system_prompt or HARNESS_TOOL_SYSTEM_PROMPT,
        chunk_size=chunk_size,
        max_tool_iterations=max_tool_iterations,
    )


__all__ = [
    "DEFAULT_MAX_TOOL_ITERATIONS",
    "harness_agent_reply_provider",
    "reply_provider_for",
]
