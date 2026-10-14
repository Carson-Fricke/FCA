"""Global knowledge-library search HTTP surface.

The knowledge corpus is global rather than workspace-scoped. The router
resolves a :class:`KnowledgeSearchService` from app state; with no searcher
configured the local composition answers an empty result set instead of
failing, so the UI's knowledge search degrades cleanly.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from thesis.knowledge.index import TrustTier
from thesis.knowledge.search import KnowledgeSearch, SearchHit, SearchQuery

#: Maximum number of knowledge hits returned for one query.
_HIT_LIMIT = 20


def _trust_tier(hit: SearchHit) -> str:
    """Map the corpus trust tier onto the UI's two-tier vocabulary."""
    if hit.trust_tier is TrustTier.WORKSPACE_PROMOTED:
        return "workspace"
    return "external"


def _hit_document(hit: SearchHit) -> dict[str, object]:
    """Shape one search hit for the UI knowledge-search contract."""
    return {
        "title": hit.title,
        "path": hit.path,
        "revision": hit.revision,
        "trust_tier": _trust_tier(hit),
        "excerpt": "",
    }


class KnowledgeSearchService:
    """Read-only search across an optional built knowledge index."""

    def __init__(self, search: KnowledgeSearch | None = None) -> None:
        self._search = search

    def search(self, query: str) -> list[dict[str, object]]:
        """Return ranked hits for ``query``; empty when no index is configured."""
        if self._search is None or not query.strip():
            return []
        hits = self._search.search(SearchQuery(text=query))
        return [_hit_document(hit) for hit in hits[:_HIT_LIMIT]]


def get_knowledge_search_service(request: Request) -> KnowledgeSearchService:
    """Resolve the service from app state so applications control the index."""
    service = getattr(request.app.state, "knowledge_search_service", None)
    if service is None:
        service = KnowledgeSearchService()
        request.app.state.knowledge_search_service = service
    if not isinstance(service, KnowledgeSearchService):
        raise RuntimeError(
            "app.state.knowledge_search_service must be a KnowledgeSearchService"
        )
    return service


def create_knowledge_router() -> APIRouter:
    """Create the global knowledge search router."""
    router = APIRouter(prefix="/v1", tags=["knowledge"])

    @router.get("/knowledge/search")
    def search_knowledge(
        q: Annotated[str, Query(min_length=1, max_length=512)],
        service: Annotated[
            KnowledgeSearchService, Depends(get_knowledge_search_service)
        ],
    ) -> dict[str, object]:
        return {"items": service.search(q)}

    return router


__all__ = [
    "KnowledgeSearchService",
    "create_knowledge_router",
    "get_knowledge_search_service",
]
