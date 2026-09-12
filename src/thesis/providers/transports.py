"""Concrete local transports for model providers (network lives here).

Providers own no network client: they call an injected transport.  These are
the workstation/local adapters that satisfy those ports with ``httpx`` and the
process environment, keeping credentials out of provider and agent code.
"""

from __future__ import annotations

import os
from collections.abc import Iterator, Mapping, Sequence
from typing import TYPE_CHECKING

import httpx

from thesis.providers.invocations import ChatMessage as ProviderChatMessage
from thesis.providers.openrouter import OpenRouterProvider

if TYPE_CHECKING:  # avoid importing chat types at module load
    from thesis.chat.contracts import ChatMessage
    from thesis.chat.responder import ReplyChunk, ReplyProvider

#: Default system instruction for a chat reply.
DEFAULT_SYSTEM_PROMPT = (
    "You are FCA Thesis, a concise, workspace-scoped quantitative research "
    "assistant. Answer the operator's latest message directly, in the same "
    "language as that message (default English), and reply with the answer "
    "only: never emit internal reasoning, analysis, planning, or "
    "chain-of-thought. The workspace is fixed by the active conversation: "
    "never ask the operator for a workspace name. When the operator shares a "
    "theory, edge, or research idea, the falsification-first "
    "hypothesis-development pipeline launches automatically and performs the "
    "hypothesis decomposition itself: never ask the operator to split a theory "
    "into sub-hypotheses, and never ask setup or clarification questions "
    "before research."
)

#: System instruction for a reply that may call the quant-harness MCP tools.
HARNESS_TOOL_SYSTEM_PROMPT = (
    DEFAULT_SYSTEM_PROMPT
    + " You can call the quant-harness tools for deterministic research "
    "execution: run_backtest and run_sweep launch jobs, get_backtest_status "
    "and get_run report state, list_artifacts enumerates outputs, and "
    "launch_trader starts a supervised session. Call a tool only when the "
    "operator's request needs it, pass typed arguments, and summarize the "
    "structured results as evidence. For run_backtest and run_sweep, use the "
    "exact config or spec path the operator provides; never guess or invent a "
    "path, and if none is given, call backtest_config_template to obtain a "
    "starter configuration, adapt it, and pass it inline as the run_backtest "
    "`config` argument rather than searching for a file. When a tool call "
    "fails, report the returned error message verbatim instead of "
    "paraphrasing it."
)
_CHAT_ROLES = frozenset(("user", "assistant", "system", "tool"))


class HttpOpenRouterTransport:
    """OpenRouter transport over ``httpx``; the only module that opens sockets."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 60.0,
        client: httpx.Client | None = None,
    ) -> None:
        if not isinstance(base_url, str) or not base_url.strip():
            raise ValueError("base_url must be a non-empty string")
        self._base_url = base_url.rstrip("/")
        self._client = client if client is not None else httpx.Client(timeout=timeout)

    def post(
        self,
        path: str,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
    ) -> tuple[int, str]:
        response = self._client.post(
            f"{self._base_url}{path}",
            headers=dict(headers),
            json=dict(payload),
        )
        return response.status_code, response.text


class EnvironmentApiKeySource:
    """Read a provider API key from the process environment."""

    def __init__(self, variable: str = "OPENROUTER_API_KEY") -> None:
        self._variable = variable

    def key(self) -> str:
        return os.environ.get(self._variable, "")


def openrouter_reply_provider(
    provider: OpenRouterProvider,
    *,
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    chunk_size: int = 24,
) -> ReplyProvider:
    """Adapt an :class:`OpenRouterProvider` into a streamed chat reply provider.

    The provider completes the request and any chain-of-thought is emitted
    first as reasoning chunks, followed by the answer text in deterministic
    chunks, so the SSE transport can keep the two channels separate while
    streaming progressively to the client.
    """
    if chunk_size < 1:
        raise ValueError("chunk_size must be a positive integer")

    def stream(history: Sequence[ChatMessage]) -> Iterator[str | ReplyChunk]:
        from thesis.chat.responder import ReplyChunk

        messages: list[ProviderChatMessage] = []
        if system_prompt:
            messages.append(ProviderChatMessage(role="system", content=system_prompt))
        for message in history:
            role = (
                message.role.value
                if hasattr(message.role, "value")
                else str(message.role)
            )
            body = message.body.strip()
            if role in _CHAT_ROLES and body:
                messages.append(ProviderChatMessage(role=role, content=body))
        if not messages:
            return
        completion = provider.complete(messages=messages)
        for index in range(0, len(completion.reasoning), chunk_size):
            yield ReplyChunk(
                text=completion.reasoning[index : index + chunk_size],
                kind="reasoning",
            )
        text = completion.content
        for index in range(0, len(text), chunk_size):
            yield text[index : index + chunk_size]

    return stream


__all__ = [
    "DEFAULT_SYSTEM_PROMPT",
    "HARNESS_TOOL_SYSTEM_PROMPT",
    "EnvironmentApiKeySource",
    "HttpOpenRouterTransport",
    "openrouter_reply_provider",
]
