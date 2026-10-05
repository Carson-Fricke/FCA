"""Serving and workflow scope reach the model's merged tool catalog."""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from tests.thesis.knowledge.test_service import Repositories, hits, write_page

from thesis.agent.harness_tools import HarnessToolProvider
from thesis.agent.messages import ToolCorrelation, decode_transcript
from thesis.agent.runtime.context import ToolContext
from thesis.agent.runtime.conversation import Conversations
from thesis.agent.runtime.registry import build_session_registry
from thesis.chat import ChatService
from thesis.chat.contracts import MessageRole
from thesis.http import serving
from thesis.knowledge.service import KnowledgeScope, KnowledgeService
from thesis.providers.openrouter import OpenRouterProfile, OpenRouterProvider
from thesis.providers.session_adapter import project_native_messages
from thesis.workflows.registry.hypothesis_development.session_services import (
    HypothesisSessionServices,
)
from thesis.workflows.registry.hypothesis_development.spec import (
    build_hypothesis_development_workflow,
)
from thesis.workspaces.local import LocalWorkspaceProvider
from thesis.workspaces.manifest import load_workspace_manifest
from thesis.workspaces.repository import WorkspaceRepositoryProvider


class Key:
    def key(self) -> str:
        return "test-key"


class Transport:
    def post(self, *args: object, **kwargs: object) -> tuple[int, str]:
        raise AssertionError("no model calls during composition")


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

    class Runtime:
        def bind_session_services(self, **kwargs: Any) -> HypothesisSessionServices:
            return HypothesisSessionServices(
                **kwargs,
                harness_tools=None,
                workflow=lambda: build_hypothesis_development_workflow(
                    knowledge_scope=scope
                ),
            )

    runtime = Runtime()
    monkeypatch.setattr(
        serving, "build_serving_workflow_runtime", lambda *args, **kwargs: runtime
    )
    monkeypatch.setattr(
        serving,
        "RuntimeWorkflowLauncher",
        lambda *args, **kwargs: SimpleNamespace(workflows=None),
    )
    harness = SimpleNamespace(list_tools=lambda: ())
    serving.build_serving_stack(
        chat_service=ChatService(),
        model_provider=OpenRouterProvider(
            OpenRouterProfile(profile_id="test", model="test"), Key(), Transport()
        ),
        harness_tools=cast(HarnessToolProvider, harness),
        knowledge_service=knowledge,
    )
    for node in ("phase", "helper"):
        context = ToolContext(
            "alpha",
            "chat",
            node,
            "agent",
            sub_agent_id=node,
            workflow_execution_id="execution",
            allowed_tools=("search_knowledge", "read_knowledge"),
        )
        registry = captured["registry"](context)
        result = registry.dispatch("search_knowledge", {"query": "momentum"})
        assert result.ok
        assert result.structured["sources"] == (
            ["shared", "workspace"] if scope == "both" else [scope]
        )
        if scope != "both":
            other = "shared" if scope == "workspace" else "workspace"
            assert not registry.dispatch(
                "search_knowledge", {"query": "momentum", "scope": other}
            ).ok
            reference = hits(knowledge.search("alpha", "momentum", scope=other))[0][
                "reference"
            ]
            assert not registry.dispatch("read_knowledge", {"reference": reference}).ok


def test_registry_still_obeys_empty_tool_policy(tmp_path: Path) -> None:
    repositories = Repositories(tmp_path / "workspaces")
    service = KnowledgeService(cast(WorkspaceRepositoryProvider, repositories))
    registry = build_session_registry(
        context=ToolContext("alpha", "chat", "chat", "agent", allowed_tools=()),
        knowledge=service,
    )
    assert registry.catalog() == ()
    assert not registry.dispatch("search_knowledge", {"query": "momentum"}).ok


def test_persisted_legacy_knowledge_result_decodes_to_neutral_input() -> None:
    chat = ChatService()
    thread = chat.create_chat("alpha", title="Knowledge", owner_actor_id="operator")
    request = chat.create_message(
        "alpha",
        thread.chat_id,
        actor_id="agent",
        role=MessageRole.ASSISTANT,
        body="Searching knowledge",
        data={
            "toolCalls": [
                {
                    "id": "search-call",
                    "type": "function",
                    "function": {
                        "name": "search_knowledge",
                        "arguments": '{"query":"momentum"}',
                    },
                }
            ]
        },
    )
    result = {
        "callId": "search-call",
        "tool": "search_knowledge",
        "ok": True,
        "structured": {"hits": [{"page_id": "momentum"}]},
    }
    response = chat.create_message(
        "alpha",
        thread.chat_id,
        actor_id="agent",
        role=MessageRole.TOOL,
        body="Knowledge evidence",
        tool_result=result,
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


def test_workflow_configuration_validates_and_grants_retrieval() -> None:
    workflow = build_hypothesis_development_workflow(knowledge_scope="workspace")
    assert workflow.spec.knowledge_scope == "workspace"
    assert workflow._doctrine is not None
    assert {"search_knowledge", "read_knowledge"}.issubset(
        workflow._doctrine.phase_tools["model_creation"]
    )
    with pytest.raises(ValueError, match="knowledge_scope"):
        build_hypothesis_development_workflow(knowledge_scope=cast(Any, "unknown"))


def test_new_workspace_scaffolds_knowledge(tmp_path: Path) -> None:
    provider = LocalWorkspaceProvider(tmp_path / "workspaces")
    workspace = provider.create(load_workspace_manifest("config/thesis/workspace.yaml"))
    assert workspace.resolve_path("knowledge/.gitkeep", must_exist=True).is_file()


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
    assert registry.dispatch("search_knowledge", {"query": "momentum"}).ok
