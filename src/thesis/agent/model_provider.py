"""Workflow model-role adapter over the OpenRouter chat provider.

The workflow ``ModelRouter`` invokes roles through the narrow
:class:`~thesis.agent.router.ModelProvider` boundary, which receives a rendered
:class:`~thesis.agent.context.PromptContext` rather than a chat transcript.
:class:`OpenRouterModelProvider` bridges that boundary to the concrete
:class:`~thesis.providers.openrouter.OpenRouterProvider`: it renders the prompt
as provider chat messages (an optional system instruction plus the prompt's
labelled rendering) and returns the completion content as a
:class:`~thesis.agent.router.ModelResponse`.

Failures stay closed: transport and rejection errors propagate so
:class:`~thesis.agent.router.ModelRouter` records the failed invocation and
wraps the fault in :class:`~thesis.agent.router.ModelInvocationError`.
"""

from __future__ import annotations

from thesis.agent.context import PromptContext
from thesis.agent.router import ModelProfile, ModelResponse
from thesis.providers.invocations import ChatMessage as ProviderChatMessage
from thesis.providers.invocations import ProviderRejectedError
from thesis.providers.openrouter import OpenRouterProvider
from thesis.providers.transports import DEFAULT_SYSTEM_PROMPT


class OpenRouterModelProvider:
    """Invoke an OpenRouter provider as a policy-role ``ModelProvider``."""

    __slots__ = ("_provider", "_system_prompt")

    def __init__(
        self,
        provider: OpenRouterProvider,
        *,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    ) -> None:
        if not isinstance(provider, OpenRouterProvider):
            raise TypeError("provider must be an OpenRouterProvider")
        self._provider = provider
        self._system_prompt = system_prompt

    def invoke(
        self,
        *,
        profile: ModelProfile,
        prompt: PromptContext,
        allowed_tools: tuple[str, ...],
    ) -> ModelResponse:
        """Complete one policy-role prompt and return its output content."""
        del profile, allowed_tools
        messages: list[ProviderChatMessage] = []
        if self._system_prompt:
            messages.append(
                ProviderChatMessage(role="system", content=self._system_prompt)
            )
        messages.append(ProviderChatMessage(role="user", content=prompt.rendered))
        completion = self._provider.complete(messages=messages, role="primary")
        output = completion.content or completion.reasoning
        if not output:
            raise ProviderRejectedError("openrouter returned an empty completion")
        return ModelResponse(output=output)


__all__ = [
    "OpenRouterModelProvider",
]
