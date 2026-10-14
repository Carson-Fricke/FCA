"""Knowledge tool outcomes are compact before canonical model projection."""

from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from typing import cast

import pytest

from thesis.extensions.tools.knowledge import knowledge_registered_tools
from thesis.knowledge.service import (
    DATA_ONLY,
    MAX_READ_CHARS,
    KnowledgeScope,
    KnowledgeService,
    effective_sources,
)
from thesis_runtime.adapters.providers.session_adapter import project_native_messages
from thesis_runtime.contracts.context import ToolContext
from thesis_runtime.contracts.messages import (
    MessageContent,
    SessionMessage,
    ToolCorrelation,
    ToolRequest,
)
from thesis_runtime.tools.registry import SessionToolRegistry


class FakeKnowledge:
    """Return oversized canonical payloads while enforcing trusted scope."""

    def __init__(self) -> None:
        self.search_calls: list[dict[str, object]] = []
        self.read_calls: list[dict[str, object]] = []
        self.hits: list[dict[str, object]] = [
            {
                "title": f"Research {index}",
                "reference": f"knowledge:{source}:page-{index}:sha256:revision:binding",
                "page_id": f"page-{index}",
                "source": source,
                "score": 1.0 - index / 10,
                "content_hash": "a" * 64,
                "snapshot_id": "sha256:revision",
                "version": 2,
                "excerpt": "e" * (1200 if index else 40),
                "excerpt_offset": 100,
                "truncated": False,
                "collections": ["research"],
                "matched_via": ["lexical", "semantic"],
                "data_only": True,
                "content_notice": DATA_ONLY,
                "relationships": [{"reference": "giant-relationship" * 10000}],
                "metadata": "giant-metadata" * 10000,
                "provenance": {
                    "path": f"research/page-{index}.md",
                    "source_url": "https://example.org/research",
                    "source_publisher": "Example Research",
                    "source_content_hash": "b" * 64,
                    "trust_tier": "curated",
                    "trust_state": "published",
                    "created": "2026-01-01",
                    "retrieved": "2026-02-01",
                    "effective_start": None,
                    "effective_end": None,
                    "metadata": "giant-provenance" * 10000,
                },
            }
            for index, source in enumerate(["shared", "workspace"] * 4)
        ]
        self.search_result: dict[str, object] = {}
        self.read_result: dict[str, object] = {}

    def search(
        self,
        workspace_id: str,
        query: str,
        *,
        scope: KnowledgeScope,
        allowed_scope: KnowledgeScope,
        limit: int,
        allowed_collections: tuple[str, ...] | None,
    ) -> dict[str, object]:
        self.search_calls.append(
            {
                "workspace_id": workspace_id,
                "query": query,
                "scope": scope,
                "allowed_scope": allowed_scope,
                "limit": limit,
                "allowed_collections": allowed_collections,
            }
        )
        sources = effective_sources(scope, allowed_scope)
        # Deliberately ignore limit to exercise the tool's visible bound.
        self.search_result = {
            "data_only": True,
            "content_notice": DATA_ONLY,
            "sources": list(sources),
            "retrieval_baseline": "fake-embedder",
            "results": [hit for hit in self.hits if hit["source"] in sources],
            "metadata": "giant-envelope" * 10000,
        }
        return self.search_result

    def read(
        self,
        workspace_id: str,
        reference: str,
        *,
        allowed_scope: KnowledgeScope,
        max_chars: int,
        allowed_collections: tuple[str, ...] | None,
    ) -> dict[str, object]:
        self.read_calls.append(
            {
                "workspace_id": workspace_id,
                "reference": reference,
                "allowed_scope": allowed_scope,
                "max_chars": max_chars,
                "allowed_collections": allowed_collections,
            }
        )
        sources = effective_sources("both", allowed_scope)
        hit = next(
            (
                hit
                for hit in self.hits
                if hit["reference"] == reference and hit["source"] in sources
            ),
            None,
        )
        if hit is None:
            raise ValueError("invalid reference C:/private/path")
        self.read_result = hit | {
            "content": ("full research " * 2000)[:max_chars],
            "truncated": True,
            "total_chars": 28000,
        }
        return self.read_result


def registry(
    service: FakeKnowledge,
    scope: KnowledgeScope = "both",
    collections: tuple[str, ...] | None = None,
) -> SessionToolRegistry:
    return SessionToolRegistry(
        knowledge_registered_tools(
            cast(KnowledgeService, service),
            ToolContext("alpha", "chat", "conversation", "actor"),
            allowed_scope=scope,
            allowed_collections=collections,
        )
    )


@pytest.mark.parametrize("limit", [None, 1, 5])
def test_search_compacts_ranked_results_before_model_projection(
    limit: int | None,
) -> None:
    service = FakeKnowledge()
    before = deepcopy(service.hits)
    arguments: dict[str, object] = {"query": "research"}
    if limit is not None:
        arguments["limit"] = limit
    execution = registry(service).dispatch(
        "search_knowledge",
        arguments,
        context=ToolContext("alpha", "chat", "conversation", "actor"),
    )
    assert execution.ok
    expected_limit = 3 if limit is None else limit
    assert service.search_calls[0]["limit"] == expected_limit
    visible = dict(execution.structured)
    hits = cast(list[dict[str, object]], visible["results"])
    assert len(hits) == expected_limit
    assert visible == {
        "data_only": True,
        "content_notice": DATA_ONLY,
        "sources": ["shared", "workspace"],
        "retrieval_baseline": "fake-embedder",
        "results": hits,
    }
    for original, hit in zip(service.hits, hits, strict=False):
        assert hit == {
            key: original[key]
            for key in (
                "title",
                "reference",
                "page_id",
                "score",
                "source",
                "content_hash",
                "excerpt_offset",
            )
        } | {
            "excerpt": cast(str, original["excerpt"])[:400],
            "truncated": len(cast(str, original["excerpt"])) > 400,
            "provenance": {
                key: cast(Mapping[str, object], original["provenance"])[key]
                for key in (
                    "path",
                    "source_url",
                    "source_publisher",
                    "source_content_hash",
                    "trust_tier",
                    "trust_state",
                )
            },
        }
    assert service.hits == before
    assert len(cast(list[object], service.search_result["results"])) == 8
    assert "metadata" in service.search_result

    request = SessionMessage(
        "request",
        "outgoing",
        MessageContent(
            "",
            "agent",
            requests=(ToolRequest("call", "search_knowledge", json.dumps(arguments)),),
        ),
    )
    receipt = SessionMessage(
        "receipt",
        "incoming",
        MessageContent(
            "",
            "tool_result",
            correlation=ToolCorrelation("request", "call", "search_knowledge"),
            result=json.loads(execution.as_content()),
        ),
    )
    projected = project_native_messages((request, receipt))
    assert len(projected) == 2
    assert projected[1].role == "tool"
    assert projected[1].tool_call_id == "call"
    model_content = projected[1].content
    assert model_content == receipt.content.as_content()
    assert len(model_content) < 8000
    assert "giant-" not in model_content
    assert model_content.count(DATA_ONLY) == 1
    assert "relationships" not in model_content
    assert json.loads(model_content)["result"]["structured"] == visible
    assert (
        project_native_messages((request, SessionMessage.from_dict(receipt.as_dict())))
        == projected
    )


@pytest.mark.parametrize("scope", ["shared", "workspace", "both"])
@pytest.mark.parametrize("requested", [None, "both", "shared", "workspace"])
def test_search_preserves_trusted_scope_and_collections(
    scope: KnowledgeScope,
    requested: KnowledgeScope | None,
) -> None:
    service = FakeKnowledge()
    arguments: dict[str, object] = {"query": "research"}
    if requested is not None:
        arguments["scope"] = requested
    execution = registry(service, scope, ("research",)).dispatch(
        "search_knowledge",
        arguments,
        context=ToolContext("alpha", "chat", "conversation", "actor"),
    )
    call = service.search_calls[0]
    assert call["workspace_id"] == "alpha"
    assert call["scope"] == (scope if requested is None else requested)
    assert call["allowed_scope"] == scope
    assert call["allowed_collections"] == ("research",)
    if scope != "both" and requested not in (None, "both", scope):
        assert not execution.ok
        assert execution.failure is not None
        assert execution.failure["kind"] == "knowledge"
    else:
        assert execution.ok
        sources = effective_sources(scope if requested is None else requested, scope)
        assert execution.structured["sources"] == list(sources)
        assert all(
            hit["source"] in sources
            for hit in cast(list[dict[str, object]], execution.structured["results"])
        )


@pytest.mark.parametrize("direct", [False, True])
@pytest.mark.parametrize("value", [0, -1, 6, 20, True, False, 1.5, "3", None])
def test_search_rejects_limits_outside_model_contract(
    value: object, direct: bool
) -> None:
    service = FakeKnowledge()
    arguments = {"query": "x", "limit": value}
    result = (
        knowledge_registered_tools(
            cast(KnowledgeService, service),
            ToolContext("alpha", "chat", "conversation", "actor"),
        )[0].handler(arguments, ToolContext("alpha", "chat", "conversation", "actor"))
        if direct
        else registry(service).dispatch(
            "search_knowledge",
            arguments,
            context=ToolContext("alpha", "chat", "conversation", "actor"),
        )
    )
    assert not result.ok
    assert service.search_calls == []


@pytest.mark.parametrize("direct", [False, True])
@pytest.mark.parametrize(
    "value", [0, -1, MAX_READ_CHARS + 1, True, False, 1.5, "6000", None]
)
def test_read_rejects_invalid_content_bounds_before_service(
    value: object, direct: bool
) -> None:
    service = FakeKnowledge()
    arguments = {"reference": service.hits[0]["reference"], "max_chars": value}
    result = (
        knowledge_registered_tools(
            cast(KnowledgeService, service),
            ToolContext("alpha", "chat", "conversation", "actor"),
        )[1].handler(arguments, ToolContext("alpha", "chat", "conversation", "actor"))
        if direct
        else registry(service).dispatch(
            "read_knowledge",
            arguments,
            context=ToolContext("alpha", "chat", "conversation", "actor"),
        )
    )
    assert not result.ok
    assert service.read_calls == []


@pytest.mark.parametrize("max_chars", [None, 1, 9500, MAX_READ_CHARS])
def test_selected_read_retains_full_metadata_and_requested_content(
    max_chars: int | None,
) -> None:
    service = FakeKnowledge()
    tools = registry(service, "workspace", ("research",))
    search = tools.dispatch(
        "search_knowledge",
        {"query": "research"},
        context=ToolContext("alpha", "chat", "conversation", "actor"),
    )
    reference = cast(list[dict[str, object]], search.structured["results"])[0][
        "reference"
    ]
    arguments = {"reference": reference}
    if max_chars is not None:
        arguments["max_chars"] = max_chars
    execution = tools.dispatch(
        "read_knowledge",
        arguments,
        context=ToolContext("alpha", "chat", "conversation", "actor"),
    )
    assert execution.ok
    bound = 6000 if max_chars is None else max_chars
    assert service.read_calls == [
        {
            "workspace_id": "alpha",
            "reference": reference,
            "allowed_scope": "workspace",
            "allowed_collections": ("research",),
            "max_chars": bound,
        }
    ]
    assert execution.structured == service.read_result
    assert len(cast(str, execution.structured["content"])) == bound
    assert execution.structured["relationships"] == service.hits[1]["relationships"]
    assert execution.structured["provenance"] == service.hits[1]["provenance"]
    assert execution.structured["data_only"] is True
    assert execution.structured["content_notice"] == DATA_ONLY


@pytest.mark.parametrize("reference", ["sample.md", "../sample.md", "shared"])
def test_read_still_rejects_paths_and_out_of_scope_references(reference: str) -> None:
    service = FakeKnowledge()
    if reference == "shared":
        reference = cast(str, service.hits[0]["reference"])
    result = registry(service, "workspace").dispatch(
        "read_knowledge",
        {"reference": reference},
        context=ToolContext("alpha", "chat", "conversation", "actor"),
    )
    assert not result.ok
    assert result.structured == {"data_only": True, "content_notice": DATA_ONLY}
    assert "private/path" not in result.as_content()


def test_advertised_defaults_match_execution() -> None:
    tools = knowledge_registered_tools(
        cast(KnowledgeService, FakeKnowledge()),
        ToolContext("alpha", "chat", "conversation", "actor"),
    )
    search = cast(
        Mapping[str, Mapping[str, object]], tools[0].input_schema["properties"]
    )
    read = cast(Mapping[str, Mapping[str, object]], tools[1].input_schema["properties"])
    assert search["limit"]["default"] == 3
    assert search["limit"]["maximum"] == 5
    assert read["max_chars"]["default"] == 6000
    assert read["max_chars"]["maximum"] == MAX_READ_CHARS
