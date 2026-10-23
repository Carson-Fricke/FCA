"""Local Thesis API composition with a configured local or remote harness.

Binds the real chat surface (T211) and the workspace DATA routes (T231) to a
single uvicorn process on 127.0.0.1:8000, with the harness-hosted MCP
boundary and CORS for the Svelte dev server.  The serving stack itself (conversations, control tools, workflow
runtime, launcher) is composed by :func:`thesis.application.composition.build_serving_stack`
— the same composition `thesis serve` uses — and only the local harness
transport, CORS, and embedded worker differ here; see .github/runbooks/thesis-local-stack.md.

Usage:
    uv run python scripts/local/thesis_local_api.py
Env:
    THESIS_API__BEARER_TOKEN (required)
    HARNESS_API_BEARER_TOKEN (optional, enables the remote status client)
"""

from __future__ import annotations

import logging
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
from thesis.application.composition import (  # noqa: E402
    build_serving_stack,
    build_serving_worker,
    open_serving_model_provider,
)
from thesis.application.workflow_launcher import daemon_thread  # noqa: E402
from thesis.http.application import (  # noqa: E402
    create_app,
    serving_workspace_store,
)
from thesis.http.harness import HarnessDataService  # noqa: E402
from thesis.http.workspaces import build_local_workspace_data  # noqa: E402
from thesis.integrations.harness_activation import (  # noqa: E402
    HarnessBoundary,
)
from thesis.integrations.mcp import (  # noqa: E402
    HarnessMcpError,
    HarnessMcpTopology,
    harness_mcp_topology_from_env,
)
from thesis.persistence.investigations import open_investigations  # noqa: E402
from thesis.persistence.stack import open_submission_store  # noqa: E402
from thesis.workspaces.local import default_workspace_root  # noqa: E402
from thesis_runtime.adapters.persistence import (  # noqa: E402
    open_runtime_persistence,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

WORKSPACE_ROOT = str(default_workspace_root())
HARNESS_BASE_URL = harness_mcp_topology_from_env().require_harness_url()


def _is_local_url(topology: HarnessMcpTopology) -> bool:
    """Return True when the MCP URL points at the loopback harness API."""
    url = topology.harness_url or ""
    return "127.0.0.1" in url or "localhost" in url


def _mcp_mode(topology: HarnessMcpTopology) -> str:
    """Return a short label for the selected MCP transport."""
    if _is_local_url(topology):
        return "local-http"
    return "remote-http"


def _harness_composition() -> tuple[HarnessBoundary | None, HarnessToolProvider | None]:
    """Open exactly one harness MCP client and derive both consumers from it.

    Local and remote harnesses both use the harness HTTP ``/mcp`` endpoint: the
    local stack points at the local harness API (default
    ``http://127.0.0.1:8001``) and remote points at the deployed service, so
    there is one MCP mechanism and no Windows stdio subprocess path.  A single
    topology-aware client is initialized once with a bounded retry, and the
    backtest boundary and chat tool provider are derived from that same client.
    When MCP stays unreachable after retries, both surfaces fail closed; the
    error is logged only after retries are exhausted. Thesis never spawns a
    harness process.
    """
    topology = harness_mcp_topology_from_env()
    try:
        composition: HarnessMcpComposition = open_harness_mcp_composition(topology)
    except (HarnessMcpError, OSError) as error:
        print(
            f"harness MCP tool catalog unavailable after retries; chat replies "
            f"without tools and backtest launches fail closed: {error}",
            file=sys.stderr,
        )
        return None, None
    provider = open_harness_tool_provider(client=composition.client)
    print(
        f"harness MCP tool catalog ready; chat tool calling enabled "
        f"({len(composition.tool_names)} tools via {_mcp_mode(topology)} "
        f"at {topology.require_harness_url()})",
        file=sys.stderr,
    )
    return composition.boundary, provider


_harness, _harness_tools = _harness_composition()

# Conversations persist in Cosmos DB (the local emulator by default) and every
# message/event shares one per-chat timeline served by the unified stream.
persistence = open_runtime_persistence()

investigations = open_investigations(persistence.chat)
chat_service, timeline = persistence.chat, persistence.timeline
submissions = open_submission_store()

workspace_store = serving_workspace_store()
repositories = getattr(workspace_store, "provider", None)
workspace_data = build_local_workspace_data(
    workspace_root=WORKSPACE_ROOT,
    submissions=submissions,
    harness_boundary=_harness,
    harness_base_url=HARNESS_BASE_URL,
    chat_service=chat_service,
    workspace_store=workspace_store,
    workspace_repositories=repositories,
)

# One serving composition owns the conversations service, control tools,
# workflow runtime, and launcher. Local serving uses the same control service
# as deployment; it only selects the local workspace tree and external adapters.
model_provider = open_serving_model_provider()
execution_store = persistence.executions
stack = build_serving_stack(
    chat_service=chat_service,
    investigations=investigations,
    model_provider=model_provider,
    harness_tools=_harness_tools,
    execution_store=execution_store,
    work_store=persistence.work,
    submissions=submissions,
    workspace_root=WORKSPACE_ROOT,
    workspace_repositories=repositories,
    detach=daemon_thread,
    on_turn_error=lambda error: print(
        f"TURN ERROR: {error[2]!r}", file=sys.stderr, flush=True
    ),
)

app = create_app(
    chat_service=stack.chat_service,
    investigations=investigations,
    workspace_data=workspace_data,
    conversations=stack.conversations,
    workflow_runtime=stack.workflow_runtime,
    workflow_registry=stack.workflow_registry,
    harness_tool_provider=stack.harness_tool_provider,
    harness_data_service=HarnessDataService(submissions=submissions),
    operator_policies=stack.operator_policies,
    timeline=timeline,
    in_memory=False,
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
    # Local convenience: an embedded worker uses exactly the durable ownership
    # boundary; deployed API replicas never start one.
    from threading import Thread

    worker = build_serving_worker(
        stack,
        persistence.work,
        persistence.ownership,
        persistence.executions,
        recovery_records=persistence.executions.identities,
    )
    Thread(target=worker.run, name="thesis-local-worker", daemon=True).start()
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
