"""Composition coverage for the backtest run-import tool-loop wiring.

Building the app with both a workspace data bundle and a harness tool provider
must construct the run coordinator over the SAME backtest service the HTTP
routes read, and attach its observer to the chat reply service so tool-loop
backtests are imported into that store.
"""

from __future__ import annotations

from collections.abc import Mapping

from thesis.agent.harness_tools import open_harness_tool_provider
from thesis.chat import ChatService, InMemoryChatStreamBroker
from thesis.chat.responder import ChatReplyService
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


def test_create_app_wires_the_backtest_coordinator_and_observer() -> None:
    bundle = build_local_workspace_data()
    provider = open_harness_tool_provider(
        HarnessMcpTopology(thesis_location="local", harness_location="local"),
        in_process_handler=_NoopHandler(),
    )
    reply = ChatReplyService(
        chat_service=ChatService(),
        broker=InMemoryChatStreamBroker(),
        provider=lambda history: iter(()),
    )

    app = create_app(
        bearer_token="thesis-test-token",
        workspace_data=bundle,
        chat_reply_service=reply,
        harness_tool_provider=provider,
    )

    coordinator = app.state.backtest_run_coordinator
    assert coordinator is not None
    assert coordinator.service is bundle.backtests
    assert app.state.backtest_service is bundle.backtests
    assert reply.tool_result_observer is not None


def test_create_app_without_a_tool_provider_leaves_the_observer_unset() -> None:
    bundle = build_local_workspace_data()
    reply = ChatReplyService(
        chat_service=ChatService(),
        broker=InMemoryChatStreamBroker(),
        provider=lambda history: iter(()),
    )

    app = create_app(
        bearer_token="thesis-test-token",
        workspace_data=bundle,
        chat_reply_service=reply,
    )

    assert getattr(app.state, "backtest_run_coordinator", None) is None
    assert reply.tool_result_observer is None
