"""Local Thesis API composition for the no-Azure development stack.

Binds the real chat surface (T211), the workspace DATA routes (T231), and
the screener router to a single uvicorn process on 127.0.0.1:8000, with a
LocalHarnessProcess backtest-launch boundary and CORS for the Svelte dev
server.  This is the composition `thesis serve` will eventually take over;
see .github/runbooks/thesis-local-stack.md.

Usage:
    uv run python scripts/local/thesis_local_api.py
Env:
    THESIS_API__BEARER_TOKEN (required)
    HARNESS_API_BEARER_TOKEN (optional, enables the remote status client)
"""

from __future__ import annotations

import os
import sys

import uvicorn

if __name__ != "__main__":
    raise SystemExit("run as a script: uv run python scripts/local/thesis_local_api.py")

sys.path.insert(0, "src")

from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

from thesis.agent.harness_tools import (  # noqa: E402
    HarnessMcpComposition,
    HarnessToolProvider,
    open_harness_mcp_composition,
    open_harness_tool_provider,
)
from thesis.agent.hypothesis_skills import (  # noqa: E402
    with_hypothesis_development_skills,
)
from thesis.agent.reply import reply_provider_for  # noqa: E402
from thesis.agent.router import (  # noqa: E402
    ModelProfile,
    Role,
    RolePolicy,
)
from thesis.agent.skills import SkillRegistry  # noqa: E402
from thesis.agent.tool_dispatch import with_harness_mcp  # noqa: E402
from thesis.chat.responder import ChatReplyService  # noqa: E402
from thesis.chat.streaming import InMemoryChatStreamBroker  # noqa: E402
from thesis.cosmos.stack import open_cosmos_chat_stack  # noqa: E402
from thesis.http.application import (  # noqa: E402
    create_app,
    serving_workspace_store,
)
from thesis.http.workspaces import build_local_workspace_data  # noqa: E402
from thesis.integrations.harness import ProcessResult  # noqa: E402
from thesis.integrations.harness_activation import (  # noqa: E402
    HarnessBoundary,
    LocalHarnessProcess,
)
from thesis.integrations.mcp import (  # noqa: E402
    HarnessMcpError,
    HarnessMcpTopology,
    harness_mcp_topology_from_env,
)
from thesis.providers.openrouter import (  # noqa: E402
    OpenRouterProfile,
    OpenRouterProvider,
)
from thesis.providers.transports import (  # noqa: E402
    EnvironmentApiKeySource,
    HttpOpenRouterTransport,
)
from thesis.research.hypothesis import WorkspaceHypothesisStore  # noqa: E402
from thesis.research.indicators import WorkspaceIndicatorStore  # noqa: E402
from thesis.research.models import WorkspaceResearchModelStore  # noqa: E402
from thesis.strategies.validation import (  # noqa: E402
    StrategyValidationRequest,
    StrategyValidationResult,
)
from thesis.workflows.local import McpBacktestRunner  # noqa: E402
from thesis.workflows.runtime import build_workflow_runtime  # noqa: E402
from thesis.workspaces.local import (  # noqa: E402
    LocalWorkspaceProvider,
    default_workspace_root,
)

WORKSPACE_ROOT = str(default_workspace_root())
HARNESS_BASE_URL = "http://127.0.0.1:8001"


def _real_subprocess_runner(argv):
    """Run a real local harness process via the uv-managed interpreter."""
    import subprocess

    completed = subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        shell=False,
    )
    return ProcessResult(completed.returncode, completed.stdout, completed.stderr)


def _mcp_disabled() -> bool:
    return os.environ.get("THESIS_HARNESS_MCP", "1").strip().lower() in (
        "0",
        "false",
        "no",
    )


def _is_local_url(topology: HarnessMcpTopology) -> bool:
    """Return True when the MCP URL points at the loopback harness API."""
    url = topology.harness_url or ""
    return "127.0.0.1" in url or "localhost" in url


def _mcp_mode(topology: HarnessMcpTopology) -> str:
    """Return a short label for the selected MCP transport."""
    if _is_local_url(topology):
        return "local-http"
    if topology.harness_location == "remote":
        return "remote-http"
    return "local-stdio"


def _harness_composition() -> tuple[HarnessBoundary | None, HarnessToolProvider | None]:
    """Open exactly one harness MCP client and derive both consumers from it.

    Local and remote harnesses both use the harness HTTP ``/mcp`` endpoint: the
    local stack points at the local harness API (default
    ``http://127.0.0.1:8001``) and remote points at the deployed service, so
    there is one MCP mechanism and no Windows stdio subprocess path.  A single
    topology-aware client is initialized once with a bounded retry, and the
    backtest boundary and chat tool provider are derived from that same client.
    When MCP is disabled or stays unreachable after retries, the composition
    falls back to the direct local process boundary (loopback only) and a
    tool-free chat; the error is logged only after retries are exhausted.
    """
    os.environ.setdefault("HARNESS_MCP_URL", HARNESS_BASE_URL)
    topology = harness_mcp_topology_from_env()
    if _mcp_disabled():
        return LocalHarnessProcess(_real_subprocess_runner), None
    try:
        composition: HarnessMcpComposition = open_harness_mcp_composition(topology)
    except (HarnessMcpError, OSError) as error:
        print(
            f"harness MCP tool catalog unavailable after retries; chat replies "
            f"without tools: {error}",
            file=sys.stderr,
        )
        if _is_local_url(topology) or topology.harness_location == "local":
            return LocalHarnessProcess(_real_subprocess_runner), None
        return None, None
    provider = open_harness_tool_provider(client=composition.client)
    print(
        f"harness MCP tool catalog ready; chat tool calling enabled "
        f"({len(composition.tool_names)} tools via {_mcp_mode(topology)})",
        file=sys.stderr,
    )
    return composition.boundary, provider


class _WorkflowModelProvider:
    """Adapt the OpenRouter chat client to the router's provider port."""

    def __init__(self, provider: OpenRouterProvider) -> None:
        self._provider = provider

    def invoke(
        self,
        *,
        profile,  # noqa: ANN001
        prompt,  # noqa: ANN001
        allowed_tools,
        response_format=None,  # noqa: ANN001
    ):
        del profile, allowed_tools
        from thesis.agent.router import ModelResponse
        from thesis.providers.invocations import ChatMessage as ProviderChatMessage

        completion = self._provider.complete(
            messages=[ProviderChatMessage(role="user", content=prompt.rendered)],
            response_format=response_format,
        )
        return ModelResponse(output=completion.content)


class _StructuralStrategyValidator:
    """Accept a structurally valid bundle for local composition."""

    def validate(self, request: StrategyValidationRequest) -> StrategyValidationResult:
        return StrategyValidationResult(
            True, (), validation_id=f"local-{request.bundle_sha256[:16]}"
        )


def _development_runtime(
    model_provider: OpenRouterProvider,
    harness_tools: HarnessToolProvider,
):
    """Compose the workflow runtime for the falsification-first pipeline."""
    skills = SkillRegistry()
    profiles = {
        "local-openrouter": ModelProfile(
            profile_id="local-openrouter",
            version="1.0.0",
            provider="openrouter",
            model=os.environ.get("THESIS_MODEL", "openai/gpt-4o-mini"),
        )
    }

    def policy(role: Role) -> RolePolicy:
        base = RolePolicy(
            role=role,
            version="1.0.0",
            model_profile="local-openrouter",
            allowed_tools=(),
        )
        return with_hypothesis_development_skills(with_harness_mcp(base))

    runner = McpBacktestRunner(harness_tools)
    workspaces = LocalWorkspaceProvider(WORKSPACE_ROOT)
    return build_workflow_runtime(
        role_policies={
            "research": policy("research"),
            "critique": policy("critique"),
            "coding": policy("coding"),
        },
        model_profiles=profiles,
        providers={"openrouter": _WorkflowModelProvider(model_provider)},
        skills=skills,
        runner=runner,
        strategy_validator=_StructuralStrategyValidator(),
        hypothesis_store=WorkspaceHypothesisStore(WORKSPACE_ROOT),
        indicator_store=WorkspaceIndicatorStore(workspaces),
        research_model_store=WorkspaceResearchModelStore(workspaces),
    )


_harness, _harness_tools = _harness_composition()

# Conversations persist in Cosmos DB (the local emulator by default) and every
# message/event shares one per-chat timeline served by the unified stream.
chat_service, timeline = open_cosmos_chat_stack()
chat_stream_broker = InMemoryChatStreamBroker()

workspace_data = build_local_workspace_data(
    workspace_root=WORKSPACE_ROOT,
    harness_boundary=_harness,
    harness_base_url=HARNESS_BASE_URL,
    chat_service=chat_service,
    workspace_store=serving_workspace_store(),
)

# Wire a live model provider only when a key is present; otherwise chat stays
# a durable message surface with no reply, rather than failing every send.
reply_service = None
workflow_runtime = None
harness_tools = None
if os.environ.get("OPENROUTER_API_KEY"):
    model_name = os.environ.get("THESIS_MODEL", "openai/gpt-4o-mini")
    provider_profile = OpenRouterProfile(
        profile_id="local-openrouter",
        model=model_name,
    )
    model_provider = OpenRouterProvider(
        provider_profile,
        EnvironmentApiKeySource(),
        HttpOpenRouterTransport(provider_profile.base_url),
    )
    harness_tools = _harness_tools
    if _harness_tools is not None:
        workflow_runtime = _development_runtime(model_provider, _harness_tools)
    reply_service = ChatReplyService(
        chat_service=chat_service,
        broker=chat_stream_broker,
        provider=reply_provider_for(model_provider, harness_tools),
        development_runtime=workflow_runtime,
    )

app = create_app(
    chat_service=chat_service,
    workspace_data=workspace_data,
    chat_stream_broker=chat_stream_broker,
    chat_reply_service=reply_service,
    workflow_runtime=workflow_runtime,
    harness_tool_provider=harness_tools,
    timeline=timeline,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
