"""Phase-scoped knowledge collections confine search, read, and tools."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from tests.thesis.knowledge.test_service import Repositories, hits, write_page

from thesis.agent.harness_tools import HarnessToolProvider
from thesis.agent.runtime.context import ToolContext
from thesis.agent.runtime.conversation import Conversations
from thesis.agent.runtime.knowledge_tools import knowledge_registered_tools
from thesis.agent.runtime.session import SessionToolRegistry
from thesis.chat import ChatService
from thesis.chat.contracts import ChatThread, SubAgentRecord, WorkflowExecution
from thesis.http import serving
from thesis.knowledge.index import (
    KnowledgeIndexError,
    PageRecord,
    build_index,
    load_index,
)
from thesis.knowledge.metadata import (
    MetadataError,
    PageMetadata,
    TrustState,
    TrustTier,
    parse_front_matter,
    render_front_matter,
)
from thesis.knowledge.search import HashingEmbedder
from thesis.knowledge.service import KnowledgeCollectionError, KnowledgeService
from thesis.providers.openrouter import OpenRouterProfile, OpenRouterProvider
from thesis.workflows.core.workflow import Workflow
from thesis.workflows.registry.hypothesis_development.session_services import (
    HypothesisSessionServices,
)
from thesis.workspaces.repository import WorkspaceRepositoryProvider


def _metadata(*, collections: tuple[str, ...] = ()) -> PageMetadata:
    return PageMetadata(
        page_id="sample",
        title="Sample",
        version=1,
        created=date(2026, 1, 1),
        retrieved=date(2026, 2, 1),
        source_url="https://example.org/research",
        source_content_hash="a" * 64,
        collections=collections,
        trust_tier=TrustTier.CURATED,
        trust_state=TrustState.PUBLISHED,
    )


def _description(registry: SessionToolRegistry, name: str) -> str:
    for spec in registry.catalog():
        function = spec.get("function")
        if isinstance(function, Mapping) and function.get("name") == name:
            return cast(str, function["description"])
    raise AssertionError(f"missing tool: {name}")


def test_front_matter_round_trips_collections() -> None:
    plain = _metadata()
    assert plain.collections == ()
    rendered = render_front_matter(plain)
    assert "collections" not in rendered
    assert parse_front_matter(rendered)[0] == plain

    tagged = _metadata(collections=("model-cards", "inference"))
    rendered = render_front_matter(tagged)
    assert rendered.index("collections:") > rendered.index("tags:")
    parsed, body = parse_front_matter(rendered)
    assert parsed == tagged
    assert parsed.collections == ("model-cards", "inference")
    assert body == ""


def test_explicit_empty_collections_parse_as_absent() -> None:
    rendered = render_front_matter(_metadata())
    text = rendered.replace("links: []", "collections: []\nlinks: []")
    parsed, _ = parse_front_matter(text)
    assert parsed.collections == ()
    assert parsed == _metadata()


@pytest.mark.parametrize(
    "value",
    ["model_cards", "Model-Cards", "model cards", "", "café"],
)
def test_invalid_collection_tokens_are_rejected(value: str) -> None:
    with pytest.raises(MetadataError):
        _metadata(collections=(value,))


def test_duplicate_and_non_list_collections_are_rejected() -> None:
    with pytest.raises(MetadataError, match="duplicate"):
        _metadata(collections=("model-cards", "model-cards"))
    rendered = render_front_matter(_metadata())
    scalar = rendered.replace("links: []", "links: []\ncollections: model-cards")
    with pytest.raises(MetadataError, match="list"):
        parse_front_matter(scalar)
    duplicated = rendered.replace(
        "links: []", "links: []\ncollections:\n  - model-cards\n  - model-cards"
    )
    with pytest.raises(MetadataError, match="duplicate"):
        parse_front_matter(duplicated)


def _record(page_id: str, collections: tuple[str, ...]) -> PageRecord:
    return PageRecord(
        page_id=page_id,
        path=f"{page_id}.md",
        revision="sha256:revision",
        version=1,
        title=page_id,
        body="body",
        created=date(2026, 1, 1),
        retrieved=date(2026, 2, 1),
        source_url="https://example.org/research",
        source_content_hash="a" * 64,
        collections=collections,
    )


def test_index_serialization_omits_empty_collections() -> None:
    embedder = HashingEmbedder()
    plain = build_index([_record("plain", ())], embedder, "rev-1")
    serialized = plain.serialize()
    assert b"collections" not in serialized
    assert load_index(serialized) == plain
    tagged = build_index([_record("tagged", ("model-cards",))], embedder, "rev-1")
    assert b'"collections":["model-cards"]' in tagged.serialize()
    assert load_index(tagged.serialize()) == tagged
    with pytest.raises(KnowledgeIndexError, match="kebab"):
        _record("bad", ("Model-Cards",))


@pytest.fixture
def environment(tmp_path: Path) -> tuple[KnowledgeService, Path]:
    repositories = Repositories(tmp_path / "workspaces")
    shared = tmp_path / "shared"
    shared.mkdir()
    service = KnowledgeService(cast(WorkspaceRepositoryProvider, repositories), shared)
    return service, shared


def _write_scoped_pages(shared: Path) -> None:
    write_page(
        shared,
        "origin",
        "Momentum origin.",
        path="origin.md",
        links=("nested/target.md",),
        collections=("model-cards",),
    )
    write_page(
        shared,
        "target",
        "Unrelated words.",
        path="nested/target.md",
        collections=("inference",),
    )
    write_page(
        shared,
        "outside",
        "Momentum outside.",
        path="outside.md",
        collections=("other",),
    )


def test_search_excludes_pages_outside_allowed_collections(environment) -> None:
    service, shared = environment
    _write_scoped_pages(shared)

    unrestricted = hits(service.search("alpha", "momentum", scope="shared"))
    assert {hit["page_id"] for hit in unrestricted} == {
        "origin",
        "target",
        "outside",
    }
    target = next(hit for hit in unrestricted if hit["page_id"] == "target")
    assert "hyperlink" in cast(list[str], target["matched_via"])

    filtered = hits(
        service.search(
            "alpha",
            "momentum",
            scope="shared",
            allowed_collections=("model-cards",),
        )
    )
    assert [hit["page_id"] for hit in filtered] == ["origin"]
    assert filtered[0]["collections"] == ["model-cards"]

    empty = service.search("alpha", "momentum", scope="shared", allowed_collections=())
    assert hits(empty) == []


def test_read_rejects_references_outside_allowed_collections(environment) -> None:
    service, shared = environment
    _write_scoped_pages(shared)
    references = {
        hit["page_id"]: hit["reference"]
        for hit in hits(service.search("alpha", "momentum", scope="shared"))
    }
    allowed = service.read(
        "alpha",
        cast(str, references["origin"]),
        allowed_collections=("model-cards",),
    )
    assert allowed["page_id"] == "origin"
    assert allowed["collections"] == ["model-cards"]
    with pytest.raises(ValueError, match="search again"):
        service.read(
            "alpha",
            cast(str, references["target"]),
            allowed_collections=("model-cards",),
        )
    with pytest.raises(KnowledgeCollectionError, match="allowed collections"):
        service.read(
            "alpha",
            cast(str, references["outside"]),
            allowed_collections=("model-cards",),
        )
    with pytest.raises(ValueError, match="search again"):
        service.read(
            "alpha",
            cast(str, references["origin"]),
            allowed_collections=(),
        )


def test_tool_dispatch_honors_collections(environment) -> None:
    service, shared = environment
    _write_scoped_pages(shared)
    context = ToolContext("alpha", "chat", "conversation", "actor")
    tools = knowledge_registered_tools(
        service,
        context,
        allowed_scope="both",
        allowed_collections=("model-cards", "inference"),
    )

    registry = SessionToolRegistry(tools)
    result = registry.dispatch("search_knowledge", {"query": "momentum"})
    assert result.ok
    assert {hit["page_id"] for hit in hits(dict(result.structured))} == {
        "origin",
        "target",
    }
    rejected = registry.dispatch(
        "search_knowledge", {"query": "momentum", "collections": ["model-cards"]}
    )
    assert not rejected.ok
    assert rejected.failure is not None
    assert rejected.failure["kind"] == "invalid_arguments"
    references = {
        hit["page_id"]: hit["reference"]
        for hit in hits(service.search("alpha", "momentum", scope="shared"))
    }
    excluded = registry.dispatch("read_knowledge", {"reference": references["outside"]})
    assert not excluded.ok
    assert excluded.failure is not None
    assert "allowed collections" in cast(str, excluded.failure["message"])
    assert registry.dispatch("read_knowledge", {"reference": references["origin"]}).ok


class _Key:
    def key(self) -> str:
        return "test-key"


class _Transport:
    def post(self, *args: object, **kwargs: object) -> tuple[int, str]:
        raise AssertionError("no model calls during composition")


def _serving_registry(
    tmp_path: Path,
    monkeypatch: Any,
    spec: object,
) -> tuple[ChatService, Any]:
    repositories = Repositories(tmp_path / "workspaces")
    shared = tmp_path / "shared"
    write_page(
        shared,
        "origin",
        "Momentum origin.",
        path="origin.md",
        collections=("model-cards",),
    )
    write_page(
        shared,
        "outside",
        "Momentum outside.",
        path="outside.md",
        collections=("other",),
    )
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
                workflow=lambda: Workflow(cast(Any, spec)),
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
    chat = ChatService()
    chat.persistence.save(
        ChatThread(
            workspace_id="alpha",
            created_at=datetime.now(UTC),
            created_by="actor",
            chat_id="chat",
            title="Knowledge",
            owner_actor_id="actor",
        )
    )
    chat.persistence.save(
        WorkflowExecution(
            workspace_id="alpha",
            created_at=datetime.now(UTC),
            created_by="actor",
            execution_id="execution",
            chat_id="chat",
            trigger_message_id="message-1",
            workflow_id="hypothesis-development",
            workflow_version="1.0.0",
            requested_by="actor",
            phase="trials",
        )
    )
    chat.persistence.save(
        SubAgentRecord(
            workspace_id="alpha",
            created_at=datetime.now(UTC),
            created_by="actor",
            sub_agent_id="phase-node",
            chat_id="chat",
            role="research",
            workflow_execution_id="execution",
            phase="critique",
            phase_executor=True,
            phase_attempt_id="execution:critique:0",
        )
    )
    chat.persistence.save(
        SubAgentRecord(
            workspace_id="alpha",
            created_at=datetime.now(UTC),
            created_by="actor",
            sub_agent_id="helper",
            chat_id="chat",
            role="research helper",
            parent_sub_agent_id="phase-node",
            workflow_execution_id="execution",
            phase_attempt_id="execution:critique:0",
            phase="critique",
            phase_executor=False,
            allowed_tools=("search_knowledge", "read_knowledge"),
        )
    )
    serving.build_serving_stack(
        chat_service=chat,
        model_provider=OpenRouterProvider(
            OpenRouterProfile(profile_id="test", model="test"),
            _Key(),
            _Transport(),
        ),
        harness_tools=cast(HarnessToolProvider, SimpleNamespace(list_tools=lambda: ())),
        knowledge_service=knowledge,
    )
    return chat, captured["registry"]


def test_serving_applies_phase_and_inherited_collections(
    tmp_path: Path, monkeypatch: Any
) -> None:
    spec = SimpleNamespace(
        knowledge_scope="both",
        phase_knowledge={
            "trials": ("model-cards",),
            "critique": ("other",),
        },
        completion_schemas={},
    )
    _, registry_for = _serving_registry(tmp_path, monkeypatch, spec)
    allowed_tools = ("search_knowledge", "read_knowledge")
    helper = ToolContext(
        "alpha",
        "chat",
        "helper",
        "agent",
        sub_agent_id="helper",
        parent_sub_agent_id="phase-node",
        workflow_execution_id="execution",
        phase_attempt_id="execution:critique:0",
        allowed_tools=allowed_tools,
    )
    result = registry_for(helper).dispatch("search_knowledge", {"query": "momentum"})
    assert result.ok
    assert [hit["page_id"] for hit in hits(dict(result.structured))] == ["origin"]

    phase_node = ToolContext(
        "alpha",
        "chat",
        "phase-node",
        "agent",
        sub_agent_id="phase-node",
        workflow_execution_id="execution",
        phase_executor=True,
        allowed_tools=allowed_tools,
    )
    result = registry_for(phase_node).dispatch(
        "search_knowledge", {"query": "momentum"}
    )
    assert result.ok
    assert [hit["page_id"] for hit in hits(dict(result.structured))] == ["outside"]


def test_helper_inherits_current_phase_knowledge_without_completion_authority(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from dataclasses import replace

    from thesis.agent.runtime.control_tools import (
        ControlToolRuntime,
        control_registered_tools,
    )
    from thesis.agent.runtime.store import PersistenceSubAgentStore

    schema = {"type": "object", "properties": {"verdict": {"type": "object"}}}
    spec = SimpleNamespace(
        knowledge_scope="workspace",
        phase_knowledge={"trials": ("model-cards",), "strategy_design": ("other",)},
        completion_schemas={"strategy_design": schema},
    )
    chat, _ = _serving_registry(tmp_path, monkeypatch, spec)
    store = PersistenceSubAgentStore(chat.persistence)
    record = store.get_sub_agent("alpha", "helper")
    assert record is not None and not record.phase_executor
    # Helpers may retain parent phase attribution while the workflow advances.
    record = replace(record, phase="strategy_design")
    store.save_sub_agent(record)
    service = HypothesisSessionServices(
        chat=chat,
        store=store,
        workflow=lambda: Workflow(cast(Any, spec)),
        harness_tools=None,
        activity=lambda context, tool, arguments, execute: execute(),
    )
    context = ToolContext(
        "alpha",
        "chat",
        "helper",
        "agent",
        sub_agent_id="helper",
        parent_sub_agent_id="phase-node",
        workflow_execution_id="execution",
        phase_attempt_id=record.phase_attempt_id,
        phase_executor=False,
        allowed_tools=record.allowed_tools,
    )
    bindings = service.resolve(context)
    assert bindings.knowledge_scope == "workspace"
    assert bindings.knowledge_collections == ("model-cards",)
    assert bindings.completion_schema is None
    assert bindings.strategy_authoring is None
    assert service.apply_completion("alpha", record, {"intentEpoch": 0}) is None
    assert "phase executor" in (
        service.validate_completion("alpha", record, {"intentEpoch": 0}) or ""
    )

    tools = control_registered_tools(
        cast(ControlToolRuntime, SimpleNamespace()),
        context,
        phase_completion_schema=bindings.completion_schema,
    )
    names = {
        cast(Mapping[str, object], tool.spec["function"])["name"] for tool in tools
    }
    assert "request_phase_completion" not in names
    assert "request_sub_agent_completion" in names

    chat.set_execution_phase("alpha", "execution", "strategy_design")
    bindings = service.resolve(context)
    assert bindings.knowledge_collections == ("other",)
    assert bindings.completion_schema is None
    assert bindings.strategy_authoring is None

    denied = service.resolve(replace(context, chat_id="other-chat"))
    assert denied.knowledge_collections == ()
    assert denied.completion_schema is None
    assert denied.strategy_authoring is None
