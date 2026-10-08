"""Concrete local transports for model providers (network lives here).

Providers own no network client: they call an injected transport.  These are
the workstation/local adapters that satisfy those ports with ``httpx`` and the
process environment, keeping credentials out of provider and agent code.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

import httpx

from thesis.providers.invocations import ProviderUnreachableError

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
    "execution: run_backtest and run_study launch jobs, get_job_status "
    "and get_job_result report state, list_artifacts enumerates outputs, and "
    "launch_trader starts a supervised session. Call a tool only when the "
    "operator's request needs it, pass typed arguments, and summarize the "
    "structured results as evidence. For run_backtest and run_study, use the "
    "exact config path the operator provides; never guess or invent a "
    "path, and if none is given, call backtest_config_template to obtain a "
    "starter configuration, adapt it, and pass it inline as the run_backtest "
    "`config` argument rather than searching for a file. When a tool call "
    "fails, report the returned error message verbatim instead of "
    "paraphrasing it."
)


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
        # Build (including JSON serialization) outside the network error boundary.
        request = self._client.build_request(
            "POST",
            f"{self._base_url}{path}",
            headers=dict(headers),
            json=dict(payload),
        )
        try:
            response = self._client.send(request)
        except (httpx.ConnectError, httpx.ConnectTimeout):
            # The provider can safely retry these known-unsent requests.
            raise
        except (httpx.TransportError, OSError) as error:
            raise ProviderUnreachableError(
                "openrouter transport is unreachable"
            ) from error
        return response.status_code, response.text


class EnvironmentApiKeySource:
    """Read a provider API key from the process environment."""

    def __init__(self, variable: str = "OPENROUTER_API_KEY") -> None:
        self._variable = variable

    def key(self) -> str:
        return os.environ.get(self._variable, "")


__all__ = [
    "DEFAULT_SYSTEM_PROMPT",
    "HARNESS_TOOL_SYSTEM_PROMPT",
    "EnvironmentApiKeySource",
    "HttpOpenRouterTransport",
]
