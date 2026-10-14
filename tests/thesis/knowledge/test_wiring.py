"""Serving and workflow scope reach the model's merged tool catalog."""

import json
from pathlib import Path
from typing import Any, cast

import pytest
from tests.thesis.knowledge.test_service import Repositories, hits, write_page

from thesis.agent.harness_tools import HarnessToolProvider
from thesis.application import composition as serving
from thesis.extensions.tools.registry import build_session_registry
from thesis.knowledge.service import KnowledgeScope, KnowledgeService
from thesis.workspaces.repository import WorkspaceRepositoryProvider
from thesis_runtime.adapters.providers.openrouter import (
    OpenRouterProfile,
    OpenRouterProvider,
)
from thesis_runtime.adapters.providers.session_adapter import project_native_messages
from thesis_runtime.contracts.context import ToolContext
from thesis_runtime.contracts.messages import (
    MessageContent,
    SessionMessage,
    ToolCorrelation,
    ToolRequest,
    decode_transcript,
)
from thesis_runtime.contracts.records import MessageRole
from thesis_runtime.conversations import ChatService
from thesis_runtime.conversations.conversation import Conversations
from thesis_runtime.workflows.execution_store import InMemoryExecutionStore


class Key:
    def key(self) -> str:
        return "test-key"


class Transport:
    def post(self, *args: object, **kwargs: object) -> tuple[int, str]:
        raise AssertionError("no model calls during composition")

    def stream(self, *args: object, **kwargs: object):
        raise AssertionError("no model calls during composition")
        yield ""


@pytest.mark.parametrize("scope", ["both", "shared", "workspace"])
def test_serving_scopes_phase_and_helper_tools(
    tmp_path: Path, monkeypatch: Any, scope: KnowledgeScope
) -> None:
    repositories = Repositories(tmp_path / "workspaces")
    shared = tmp_path / "shared"
    write_page(shared)
    write_page(repositories.root / "alpha" / "knowledge")
    knowledge = KnowledgeService(
        cast(WorkspaceRepositoryProvider, repositories), shared
    )
    captured: dict[str, Any] = {}

    def conversations(**kwargs: Any) -> Conversations:
        captured["registry"] = kwargs["registry_factory"]
        return Conversations(**kwargs)

    monkeypatch.setattr(serving, "Conversations", conversations)

    class Harness(HarnessToolProvider):
        def __init__(self):
            pass

        def list_tools(self):
            return ()

    monkeypatch.setenv("THESIS_KNOWLEDGE_SCOPE", scope)
    chat = ChatService()
    thread = chat.create_chat("alpha", title="Knowledge", owner_actor_id="operator")
    trigger = chat.send_message(
        "alpha", thread.chat_id, actor_id="operator", body="Research momentum"
    )
    stack = serving.build_serving_stack(
        chat_service=chat,
        execution_store=InMemoryExecutionStore(),
        model_provider=OpenRouterProvider(
            OpenRouterProfile(profile_id="test", model="test"), Key(), Transport()
        ),
        harness_tools=Harness(),
        knowledge_service=knowledge,
    )
    assert stack.workflow_runtime is not None
    workflow = stack.workflow_runtime.build_hypothesis_development(chat=chat)
    request = workflow.launch(
        "alpha",
        thread.chat_id,
        actor_id="operator",
        inputs={"idea": trigger.body},
        trigger_message_id=trigger.message_id,
    )
    for node in ("phase", "helper"):
        context = ToolContext(
            "alpha",
            thread.chat_id,
            node,
            "agent",
            sub_agent_id=node,
            workflow_execution_id=request.execution_id,
            allowed_tools=("search_knowledge", "read_knowledge"),
        )
        registry = captured["registry"](context)
        result = registry.dispatch(
            "search_knowledge", {"query": "momentum"}, context=context
        )
        assert result.ok
        assert result.structured["sources"] == (
            ["shared", "workspace"] if scope == "both" else [scope]
        )
        if scope != "both":
            other = "shared" if scope == "workspace" else "workspace"
            assert not registry.dispatch(
                "search_knowledge",
                {"query": "momentum", "scope": other},
                context=context,
            ).ok
            reference = hits(knowledge.search("alpha", "momentum", scope=other))[0][
                "reference"
            ]
            assert not registry.dispatch(
                "read_knowledge", {"reference": reference}, context=context
            ).ok


def test_registry_still_obeys_empty_tool_policy(tmp_path: Path) -> None:
    repositories = Repositories(tmp_path / "workspaces")
    service = KnowledgeService(cast(WorkspaceRepositoryProvider, repositories))
    registry = build_session_registry(
        context=ToolContext("alpha", "chat", "chat", "agent", allowed_tools=()),
        knowledge=service,
    )
    assert registry.catalog() == ()
    assert not registry.dispatch(
        "search_knowledge",
        {"query": "momentum"},
        context=ToolContext("alpha", "chat", "chat", "agent", allowed_tools=()),
    ).ok


def test_canonical_knowledge_result_decodes_to_neutral_input() -> None:
    chat = ChatService()
    thread = chat.create_chat("alpha", title="Knowledge", owner_actor_id="operator")
    request_envelope = SessionMessage(
        "request-message",
        "outgoing",
        MessageContent(
            "Searching knowledge",
            "agent",
            requests=(
                ToolRequest("search-call", "search_knowledge", '{"query":"momentum"}'),
            ),
        ),
    )
    request = chat.create_message(
        "alpha",
        thread.chat_id,
        actor_id="agent",
        message_id="request-message",
        role=MessageRole.ASSISTANT,
        body="Searching knowledge",
        data={"sessionMessage": request_envelope.as_dict()},
    )
    result = {
        "callId": "search-call",
        "tool": "search_knowledge",
        "ok": True,
        "structured": {"hits": [{"page_id": "momentum"}]},
    }
    result_envelope = SessionMessage(
        "result-message",
        "incoming",
        MessageContent(
            "Knowledge evidence",
            "tool_result",
            correlation=ToolCorrelation(
                "request-message", "search-call", "search_knowledge"
            ),
            result=result,
        ),
    )
    response = chat.send_message(
        "alpha",
        thread.chat_id,
        actor_id="agent",
        body="Knowledge evidence",
        role=MessageRole.TOOL,
        tool_result=result,
        data={"sessionMessage": result_envelope.as_dict()},
    )
    canonical = decode_transcript((request, response))
    assert [message.direction for message in canonical] == ["outgoing", "incoming"]
    assert canonical[0].content.source == "agent"
    assert canonical[1].content.source == "tool_result"
    assert canonical[1].content.text == "Knowledge evidence"
    assert json.loads(canonical[1].content.as_content())["result"] == result
    assert canonical[1].content.correlation == ToolCorrelation(
        request.message_id, "search-call", "search_knowledge"
    )
    projected = project_native_messages(canonical)
    assert projected[1].role == "tool"
    assert projected[1].tool_call_id == "search-call"
    assert projected[1].content == canonical[1].content.as_content()


def test_serving_without_harness_exposes_knowledge(
    tmp_path: Path, monkeypatch: Any
) -> None:
    repositories = Repositories(tmp_path / "workspaces")
    shared = tmp_path / "shared"
    write_page(shared)
    captured: dict[str, Any] = {}

    def conversations(**kwargs: Any) -> Conversations:
        captured.update(kwargs)
        return Conversations(**kwargs)

    monkeypatch.setattr(serving, "Conversations", conversations)
    stack = serving.build_serving_stack(
        chat_service=ChatService(),
        model_provider=OpenRouterProvider(
            OpenRouterProfile(profile_id="test", model="test"), Key(), Transport()
        ),
        knowledge_service=KnowledgeService(
            cast(WorkspaceRepositoryProvider, repositories), shared
        ),
    )
    assert stack.workflow_runtime is None
    registry = captured["registry_factory"](
        ToolContext("alpha", "chat", "chat", "agent")
    )
    assert registry.dispatch(
        "search_knowledge",
        {"query": "momentum"},
        context=ToolContext("alpha", "chat", "chat", "agent"),
    ).ok
