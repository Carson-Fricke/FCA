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
from thesis.agent.model_provider import OpenRouterModelProvider  # noqa: E402
from thesis.agent.reply import reply_provider_for  # noqa: E402
from thesis.chat.responder import ChatReplyService  # noqa: E402
from thesis.chat.service import ChatService  # noqa: E402
from thesis.chat.streaming import InMemoryChatStreamBroker  # noqa: E402
from thesis.http.application import create_app  # noqa: E402
from thesis.http.workspaces import build_local_workspace_data  # noqa: E402
from thesis.integrations.harness import ProcessResult  # noqa: E402
from thesis.integrations.harness_activation import LocalHarnessProcess  # noqa: E402
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
from thesis.workflows.local import build_local_workflow_runtime  # noqa: E402


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


workspace_data = build_local_workspace_data(
    workspace_root=WORKSPACE_ROOT,
    harness_boundary=_harness_boundary(),
    harness_base_url=HARNESS_BASE_URL,
)

chat_service = ChatService()
chat_stream_broker = InMemoryChatStreamBroker()

# Wire a live model provider only when a key is present; otherwise chat stays
# a durable message surface with no reply, rather than failing every send.
reply_service = None
workflow_runtime = None
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
    harness_tools = _harness_tool_provider()
    reply_service = ChatReplyService(
        chat_service=chat_service,
        broker=chat_stream_broker,
        provider=reply_provider_for(model_provider, harness_tools),
    )
    if harness_tools is not None:
        workflow_runtime = build_local_workflow_runtime(
            model_provider=OpenRouterModelProvider(model_provider),
            model=model_name,
            harness_tools=harness_tools,
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
