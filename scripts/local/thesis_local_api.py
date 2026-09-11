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

from thesis.chat.responder import ChatReplyService  # noqa: E402
from thesis.chat.service import ChatService  # noqa: E402
from thesis.chat.streaming import InMemoryChatStreamBroker  # noqa: E402
from thesis.http.application import create_app  # noqa: E402
from thesis.http.workspaces import build_local_workspace_data  # noqa: E402
from thesis.integrations.mcp import (  # noqa: E402
    HarnessMcpBoundary,
    HarnessMcpClient,
    StdioMcpTransport,
)
from thesis.providers.openrouter import (  # noqa: E402
    OpenRouterProfile,
    OpenRouterProvider,
)
from thesis.providers.transports import (  # noqa: E402
    EnvironmentApiKeySource,
    HttpOpenRouterTransport,
    openrouter_reply_provider,
)

WORKSPACE_ROOT = ".local/thesis-workspaces"
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
    return completed.returncode, completed.stdout, completed.stderr


def _harness_mcp_boundary() -> HarnessMcpBoundary | None:
    """Connect Thesis to the harness MCP stdio server, or fall back to direct."""
    if os.environ.get("THESIS_HARNESS_MCP", "1").strip().lower() in (
        "0",
        "false",
        "no",
    ):
        return None
    try:
        return HarnessMcpBoundary.connect(
            HarnessMcpClient(StdioMcpTransport(("harness", "mcp")))
        )
    except Exception as error:  # boundary is optional; never block startup
        print(
            f"harness MCP unavailable, using the direct process boundary: {error}",
            file=sys.stderr,
        )
        return None


workspace_data = build_local_workspace_data(
    workspace_root=WORKSPACE_ROOT,
    harness_runner=_real_subprocess_runner,
    harness_boundary=_harness_mcp_boundary(),
    harness_base_url=HARNESS_BASE_URL,
)

chat_service = ChatService()
chat_stream_broker = InMemoryChatStreamBroker()

# Wire a live model provider only when a key is present; otherwise chat stays
# a durable message surface with no reply, rather than failing every send.
reply_service = None
if os.environ.get("OPENROUTER_API_KEY"):
    provider_profile = OpenRouterProfile(
        profile_id="local-openrouter",
        model=os.environ.get("THESIS_MODEL", "openai/gpt-4o-mini"),
    )
    reply_service = ChatReplyService(
        chat_service=chat_service,
        broker=chat_stream_broker,
        provider=openrouter_reply_provider(
            OpenRouterProvider(
                provider_profile,
                EnvironmentApiKeySource(),
                HttpOpenRouterTransport(provider_profile.base_url),
            )
        ),
    )

app = create_app(
    chat_service=chat_service,
    workspace_data=workspace_data,
    chat_stream_broker=chat_stream_broker,
    chat_reply_service=reply_service,
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
