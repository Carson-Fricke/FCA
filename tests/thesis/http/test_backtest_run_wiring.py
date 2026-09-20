"""Composition coverage for the backtest run-import tool-loop wiring.

Building the app with both a workspace data bundle and a harness tool provider
must construct the run coordinator over the SAME backtest service the HTTP
routes read, and attach its observer to the conversations service so
tool-loop backtests are imported into that store.
"""

from __future__ import annotations

from collections.abc import Mapping

from thesis.agent.harness_tools import open_harness_tool_provider
from thesis.agent.runtime.conversation import Conversations
from thesis.chat import ChatService, InMemoryChatStreamBroker
from thesis.http.application import create_app
from thesis.http.workspaces import build_local_workspace_data
from thesis.integrations.mcp import HarnessMcpTopology


class _NoopHandler:
    def handle(self, request: object) -> dict[str, object] | None:
        if not isinstance(request, Mapping):
            raise TypeError("request must be a mapping")
        message = dict(request)
        if "id" not in message:
            return None
        return {"jsonrpc": "2.0", "id": message.get("id"), "result": {}}


def _conversations() -> Conversations:
    return Conversations(
        chat=ChatService(),
        broker=InMemoryChatStreamBroker(),
        reply_for=lambda workspace_id, chat_id, history: iter(()),
    )


def test_create_app_wires_the_backtest_coordinator_and_observer() -> None:
    bundle = build_local_workspace_data()
    provider = open_harness_tool_provider(
        HarnessMcpTopology(thesis_location="local", harness_location="local"),
        in_process_handler=_NoopHandler(),
    )
    conversations = _conversations()

    app = create_app(
        bearer_token="thesis-test-token",
        workspace_data=bundle,
        conversations=conversations,
        harness_tool_provider=provider,
    )

    coordinator = app.state.backtest_run_coordinator
    assert coordinator is not None
    assert coordinator.service is bundle.backtests
    assert app.state.backtest_service is bundle.backtests
    assert conversations.tool_result_observer is not None


def test_create_app_without_a_tool_provider_leaves_the_observer_unset() -> None:
    bundle = build_local_workspace_data()
    conversations = _conversations()

    app = create_app(
        bearer_token="thesis-test-token",
        workspace_data=bundle,
        conversations=conversations,
    )

    assert getattr(app.state, "backtest_run_coordinator", None) is None
    assert conversations.tool_result_observer is None
