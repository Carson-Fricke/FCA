"""Local Thesis API composition for the no-Azure development stack.

Binds the real chat surface (T211) and the workspace DATA routes (T231) to a
single uvicorn process on 127.0.0.1:8000, with the harness-hosted MCP
boundary and CORS for the Svelte dev server.  The serving stack itself (conversations, control tools, workflow
runtime, launcher) is composed by :func:`thesis.http.serving.build_serving_stack`
— the same composition `thesis serve` uses — and only the local harness
transport and logging differ here; see .github/runbooks/thesis-local-stack.md.

Usage:
    uv run python scripts/local/thesis_local_api.py
Env:
    THESIS_API__BEARER_TOKEN (required)
    HARNESS_API_BEARER_TOKEN (optional, enables the remote status client)
"""

from __future__ import annotations

import logging
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
from thesis.agent.runtime.control import ControlToolService  # noqa: E402
from thesis.chat.workflow_launcher import daemon_thread  # noqa: E402
from thesis.cosmos.stack import (  # noqa: E402
    open_cosmos_chat_stack,
    open_execution_store,
)
from thesis.http.application import (  # noqa: E402
    create_app,
    require_single_worker,
    serving_workspace_store,
)
from thesis.http.serving import (  # noqa: E402
    build_serving_stack,
    open_serving_model_provider,
)
from thesis.http.workspaces import build_local_workspace_data  # noqa: E402
from thesis.integrations.harness_activation import (  # noqa: E402
    HarnessBoundary,
)
from thesis.integrations.mcp import (  # noqa: E402
    HarnessMcpError,
    HarnessMcpTopology,
    harness_mcp_topology_from_env,
)
from thesis.workspaces.local import default_workspace_root  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
LOGGER = logging.getLogger("thesis.local_api")

WORKSPACE_ROOT = str(default_workspace_root())
HARNESS_BASE_URL = "http://127.0.0.1:8001"


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
    os.environ.setdefault("HARNESS_MCP_URL", HARNESS_BASE_URL)
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
        f"({len(composition.tool_names)} tools via {_mcp_mode(topology)})",
        file=sys.stderr,
    )
    return composition.boundary, provider


class _LoggingControlToolService(ControlToolService):
    """Control surface that logs operator-input and transition decisions."""

    def request_operator_input(self, workspace_id, sub_agent_id, *, message, reason=""):  # noqa: ANN001, ANN201
        request = super().request_operator_input(
            workspace_id, sub_agent_id, message=message, reason=reason
        )
        LOGGER.info(
            "operator.input.request node=%s reason=%s",
            sub_agent_id,
            reason or "-",
        )
        return request

    def complete_phase(self, workspace_id, sub_agent_id, *, completion):  # noqa: ANN001, ANN201
        LOGGER.info("transition.request kind=phase node=%s", sub_agent_id)
        try:
            record = super().complete_phase(
                workspace_id, sub_agent_id, completion=completion
            )
        except Exception as error:
            LOGGER.warning(
                "transition.reject kind=phase node=%s error=%s", sub_agent_id, error
            )
            raise
        LOGGER.info(
            "transition.accept kind=phase node=%s status=%s",
            sub_agent_id,
            record.status.value,
        )
        return record

    def complete_sub_agent(self, workspace_id, sub_agent_id, *, result):  # noqa: ANN001, ANN201
        LOGGER.info("transition.request kind=sub_agent node=%s", sub_agent_id)
        try:
            record = super().complete_sub_agent(
                workspace_id, sub_agent_id, result=result
            )
        except Exception as error:
            LOGGER.warning(
                "transition.reject kind=sub_agent node=%s error=%s", sub_agent_id, error
            )
            raise
        LOGGER.info(
            "transition.accept kind=sub_agent node=%s status=%s",
            sub_agent_id,
            record.status.value,
        )
        return record


require_single_worker()
_harness, _harness_tools = _harness_composition()

# Conversations persist in Cosmos DB (the local emulator by default) and every
# message/event shares one per-chat timeline served by the unified stream.
chat_service, timeline = open_cosmos_chat_stack()

workspace_store = serving_workspace_store()
repositories = getattr(workspace_store, "provider", None)
workspace_data = build_local_workspace_data(
    workspace_root=WORKSPACE_ROOT,
    harness_boundary=_harness,
    harness_base_url=HARNESS_BASE_URL,
    chat_service=chat_service,
    workspace_store=workspace_store,
    workspace_repositories=repositories,
)

# One serving composition owns the conversations service, control tools,
# workflow runtime, and launcher; the local composition only swaps in the
# logging control service and the local workspace tree.
model_provider = open_serving_model_provider()
execution_store = (
    open_execution_store()
    if model_provider is not None and _harness_tools is not None
    else None
)
stack = build_serving_stack(
    chat_service=chat_service,
    model_provider=model_provider,
    harness_tools=_harness_tools,
    execution_store=execution_store,
    workspace_root=WORKSPACE_ROOT,
    workspace_repositories=repositories,
    detach=daemon_thread,
    control_factory=_LoggingControlToolService,
    on_turn_error=lambda error: print(
        f"TURN ERROR: {error[2]!r}", file=sys.stderr, flush=True
    ),
)

app = create_app(
    chat_service=stack.chat_service,
    workspace_data=workspace_data,
    conversations=stack.conversations,
    workflow_runtime=stack.workflow_runtime,
    workflow_registry=stack.workflow_registry,
    harness_tool_provider=stack.harness_tool_provider,
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
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
