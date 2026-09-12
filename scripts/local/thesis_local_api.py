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
    HarnessToolProvider,
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
from thesis.chat.service import ChatService  # noqa: E402
from thesis.chat.streaming import InMemoryChatStreamBroker  # noqa: E402
from thesis.http.application import create_app  # noqa: E402
from thesis.http.workspaces import build_local_workspace_data  # noqa: E402
from thesis.integrations.harness import ProcessResult  # noqa: E402
from thesis.integrations.harness_activation import (  # noqa: E402
    HarnessActivationRequest,
    LocalHarnessProcess,
)
from thesis.integrations.mcp import (  # noqa: E402
    HarnessMcpError,
    harness_mcp_topology_from_env,
    open_local_harness_boundary,
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
from thesis.strategies.validation import (  # noqa: E402
    StrategyValidationRequest,
    StrategyValidationResult,
)
from thesis.workflows.backtests import (  # noqa: E402
    BacktestRunRequest,
    BacktestRunResult,
)
from thesis.workflows.runtime import build_workflow_runtime  # noqa: E402


def _workspace_root() -> str:
    """Resolve the local workspace root; default to the OS local app data."""
    override = os.environ.get("THESIS_WORKSPACE_ROOT")
    if override and override.strip():
        return override.strip()
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME")
    if not base:
        base = os.path.expanduser("~/.local/share")
    return os.path.join(base, "FCA", "thesis-workspaces")


WORKSPACE_ROOT = _workspace_root()
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


def _harness_boundary():
    """Open the topology-aware harness boundary, falling back to direct local.

    The MCP boundary is preferred for both backtest launches and tool calls;
    when the MCP server cannot start and the harness is local, the direct
    ``harness backtest-job`` process boundary is used instead.  A remote harness
    never silently falls back, and a failure never blocks startup.
    """
    topology = harness_mcp_topology_from_env()
    if _mcp_disabled():
        return LocalHarnessProcess(_real_subprocess_runner)
    try:
        return open_local_harness_boundary(
            topology=topology, process_runner=_real_subprocess_runner
        )
    except Exception as error:  # boundary is optional; never block startup
        print(
            f"harness MCP boundary unavailable, using the direct process "
            f"boundary: {error}",
            file=sys.stderr,
        )
        if topology.harness_location == "local":
            return LocalHarnessProcess(_real_subprocess_runner)
        return None


def _harness_tool_provider() -> HarnessToolProvider | None:
    """Open the harness MCP tool catalog for the chat agent, or fall back.

    The provider is resolved from the same deployment topology as the boundary
    so the agent works against a local or remote harness.  A missing ``harness``
    executable, an unreachable remote endpoint, or an unusable catalog only
    disables tool calling: the chat agent still replies without tools and never
    reports that it cannot see the harness MCP server.
    """
    if _mcp_disabled():
        return None
    try:
        provider = open_harness_tool_provider(harness_mcp_topology_from_env())
        provider.client.initialize()
        provider.list_tools()
        return provider
    except (HarnessMcpError, OSError) as error:
        print(
            f"harness MCP tool catalog unavailable; chat replies without tools: "
            f"{error}",
            file=sys.stderr,
        )
        return None


class _WorkflowModelProvider:
    """Adapt the OpenRouter chat client to the router's provider port."""

    def __init__(self, provider: OpenRouterProvider) -> None:
        self._provider = provider

    def invoke(self, *, profile, prompt, allowed_tools):  # noqa: ANN001
        del profile, allowed_tools
        from thesis.agent.router import ModelResponse
        from thesis.providers.invocations import ChatMessage as ProviderChatMessage

        completion = self._provider.complete(
            messages=[ProviderChatMessage(role="user", content=prompt.rendered)]
        )
        return ModelResponse(output=completion.content)


class _BoundaryBacktestRunner:
    """Adapt the local harness activation boundary to the runner port."""

    def __init__(self, boundary) -> None:  # noqa: ANN001
        self._boundary = boundary

    def run(self, request: BacktestRunRequest) -> BacktestRunResult:
        import json
        import os
        import tempfile

        document = dict(request.bundle.harness_configuration)
        document.setdefault("run_id", request.run_id)
        descriptor, path = tempfile.mkstemp(prefix="thesis-backtest-", suffix=".json")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(document, handle)
            outcome = self._boundary.run_backtest(
                HarnessActivationRequest(
                    bundle_ref=request.bundle.bundle_sha256,
                    config_path=path,
                    action="backtest",
                    environment="paper",
                )
            )
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        raw = dict(outcome.raw)
        state = outcome.state or ("completed" if outcome.exit_code == 0 else "failed")
        return BacktestRunResult(
            job_id=outcome.job_id or request.run_id,
            state=state,
            mode="local",
            manifest=raw.get("manifest"),
            metrics=raw.get("metrics"),
            result_artifacts=raw.get("artifacts"),
        )


class _StructuralStrategyValidator:
    """Accept a structurally valid bundle for local composition."""

    def validate(self, request: StrategyValidationRequest) -> StrategyValidationResult:
        return StrategyValidationResult(
            True, (), validation_id=f"local-{request.bundle_sha256[:16]}"
        )


def _development_runtime(
    model_provider: OpenRouterProvider,
    boundary,  # noqa: ANN001
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

    runner = _BoundaryBacktestRunner(boundary) if boundary is not None else None
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
    )


_harness = _harness_boundary()
workspace_data = build_local_workspace_data(
    workspace_root=WORKSPACE_ROOT,
    harness_boundary=_harness,
    harness_base_url=HARNESS_BASE_URL,
)

chat_service = ChatService()
chat_stream_broker = InMemoryChatStreamBroker()

# Wire a live model provider only when a key is present; otherwise chat stays
# a durable message surface with no reply, rather than failing every send.
reply_service = None
workflow_runtime = None
if os.environ.get("OPENROUTER_API_KEY"):
    provider_profile = OpenRouterProfile(
        profile_id="local-openrouter",
        model=os.environ.get("THESIS_MODEL", "openai/gpt-4o-mini"),
    )
    model_provider = OpenRouterProvider(
        provider_profile,
        EnvironmentApiKeySource(),
        HttpOpenRouterTransport(provider_profile.base_url),
    )
    if _harness is not None:
        workflow_runtime = _development_runtime(model_provider, _harness)
    reply_service = ChatReplyService(
        chat_service=chat_service,
        broker=chat_stream_broker,
        provider=reply_provider_for(model_provider, _harness_tool_provider()),
        workflow_runtime=workflow_runtime,
        development_runtime=workflow_runtime,
    )

app = create_app(
    chat_service=chat_service,
    workspace_data=workspace_data,
    chat_stream_broker=chat_stream_broker,
    chat_reply_service=reply_service,
    workflow_runtime=workflow_runtime,
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
