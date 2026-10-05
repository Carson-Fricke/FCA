"""Hybrid knowledge search over the rebuildable index.

Search combines four deterministic retrieval paths over one
:class:`~thesis.knowledge.index.SearchIndex`:

- lexical retrieval: lowercased term overlap with term-frequency weighting;
- metadata filtering: tag, source, page-id, and effective-date windowing
  (a page is applicable when the query date lies within
  ``effective_start..effective_end``, with open ends allowed);
- hyperlink expansion: pages linked from or to lexical matches, retrieved at
  a deliberately lower weight; and
- semantic retrieval: cosine similarity between the query embedding and the
  page embedding from the injected :class:`EmbeddingPort`.

Weight constants are explicit and fixed, and ordering is deterministic:
score descending, then page id ascending for ties.  Every hit carries the
full citation provenance of the corpus state it came from: Markdown path,
Git revision, page version, source record, effective dates, and trust tier.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from thesis.knowledge.index import (
    EmbeddingPort,
    IndexEntry,
    SearchIndex,
    TrustState,
    TrustTier,
    embedding_identity,
    embedding_operation,
    tokenize,
)


class KnowledgeSearchError(ValueError):
    """Raised when queries, embedders, or index shapes are invalid."""


class MatchedVia(StrEnum):
    """The retrieval paths that can surface one search hit."""

    LEXICAL = "lexical"
    METADATA = "metadata"
    HYPERLINK = "hyperlink"
    SEMANTIC = "semantic"


#: Weight of the lexical term-overlap component of a hit score.
LEXICAL_WEIGHT = 1.0
#: Weight of the semantic cosine-similarity component of a hit score.
SEMANTIC_WEIGHT = 0.75
#: Multiplier applied to the referring page's lexical score when a hit is
#: produced by hyperlink expansion; expansion is always the weakest path.
HYPERLINK_WEIGHT = 0.25

_HASHING_SEED = b"thesis-knowledge-embedder-v1"


def _token_bucket(token: str, dim: int) -> int:
    digest = hashlib.blake2b(
        token.encode("utf-8"), key=_HASHING_SEED, digest_size=8
    ).digest()
    return int.from_bytes(digest, "big") % dim


class HashingEmbedder:
    """The dependency-free default :class:`EmbeddingPort` implementation.

    Deterministic hashed bag of words: tokens are lowercased alphanumeric
    runs, each token is hashed with a fixed seed into one of ``dim`` buckets,
    bucket counts are accumulated, and the vector is L2-normalized.  It is a
    replaceable baseline, not a contract; production embedders implement the
    same protocol.
    """

    def __init__(self, dim: int = 256) -> None:
        if isinstance(dim, bool) or not isinstance(dim, int) or dim < 1:
            raise KnowledgeSearchError("dim must be a positive integer")
        self._dim = dim

    @property
    def dim(self) -> int:
        """The fixed dimensionality of produced vectors."""
        return self._dim

    @property
    def model_id(self) -> str:
        return "hashed-bag-of-words"

    @property
    def cache_identity(self) -> str:
        return f"{self.model_id}:v1:{self._dim}"

    def embed(self, text: str) -> list[float]:
        """Return the L2-normalized hashed bag-of-words vector of ``text``."""
        if not isinstance(text, str):
            raise KnowledgeSearchError("text must be a string")
        vector = [0.0] * self._dim
        for token in tokenize(text):
            vector[_token_bucket(token, self._dim)] += 1.0
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0.0:
            return vector
        return [value / norm for value in vector]


def _checked_str_tuple(values: object, label: str) -> tuple[str, ...]:
    if isinstance(values, str) or not isinstance(values, (tuple, list)):
        raise KnowledgeSearchError(f"{label} must be a tuple of strings")
    for value in values:
        if not isinstance(value, str) or not value:
            raise KnowledgeSearchError(f"{label} must contain non-empty strings")
    return tuple(values)


@dataclass(frozen=True, slots=True)
class SearchQuery:
    """One hybrid search request.

    ``tags`` is conjunctive (every requested tag must be present),
    ``source_url`` and ``page_ids`` are exact-match filters, and ``as_of``
    drives effective-date applicability.  All filters are optional.
    """

    text: str = ""
    tags: tuple[str, ...] = ()
    as_of: date | None = None
    source_url: str | None = None
    page_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise KnowledgeSearchError("text must be a string")
        if self.source_url is not None and (
            not isinstance(self.source_url, str) or not self.source_url
        ):
            raise KnowledgeSearchError("source_url must be a non-empty string or None")
        if self.as_of is not None and not isinstance(self.as_of, date):
            raise KnowledgeSearchError("as_of must be a datetime.date or None")
        object.__setattr__(self, "tags", _checked_str_tuple(self.tags, "tags"))
        object.__setattr__(
            self, "page_ids", _checked_str_tuple(self.page_ids, "page_ids")
        )


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One search result with its complete citation provenance."""

    page_id: str
    path: str
    revision: str
    version: int
    title: str
    source_url: str
    source_publisher: str | None
    source_content_hash: str
    created: date
    retrieved: date
    effective_start: date | None
    effective_end: date | None
    trust_tier: TrustTier
    trust_state: TrustState
    score: float
    matched_via: frozenset[MatchedVia]


def _cosine(first: list[float] | tuple[float, ...], second: tuple[float, ...]) -> float:
    if len(first) != len(second):
        raise KnowledgeSearchError(
            f"embedding dimension mismatch: {len(first)} != {len(second)}"
        )
    dot = 0.0
    first_norm = 0.0
    second_norm = 0.0
    for left, right in zip(first, second, strict=True):
        dot += left * right
        first_norm += left * left
        second_norm += right * right
    if dot == 0.0 or first_norm == 0.0 or second_norm == 0.0:
        return 0.0
    return dot / (math.sqrt(first_norm) * math.sqrt(second_norm))


def _applicable(entry: IndexEntry, as_of: date | None) -> bool:
    """Whether ``entry`` is applicable at ``as_of`` (open ends allowed)."""
    if as_of is None:
        return True
    if entry.effective_start is not None and as_of < entry.effective_start:
        return False
    if entry.effective_end is not None and as_of > entry.effective_end:
        return False
    return True


def _hit_from_entry(
    entry: IndexEntry, score: float, matched_via: frozenset[MatchedVia]
) -> SearchHit:
    return SearchHit(
        page_id=entry.page_id,
        path=entry.path,
        revision=entry.revision,
        version=entry.version,
        title=entry.title,
        source_url=entry.source_url,
        source_publisher=entry.source_publisher,
        source_content_hash=entry.source_content_hash,
        created=entry.created,
        retrieved=entry.retrieved,
        effective_start=entry.effective_start,
        effective_end=entry.effective_end,
        trust_tier=entry.trust_tier,
        trust_state=entry.trust_state,
        score=score,
        matched_via=matched_via,
    )


class KnowledgeSearch:
    """Deterministic hybrid retrieval over one built index."""

    def __init__(self, index: SearchIndex, embedder: EmbeddingPort) -> None:
        if not isinstance(index, SearchIndex):
            raise KnowledgeSearchError("index must be a SearchIndex")
        self._index = index
        self._embedder = embedder
        self._by_id: dict[str, IndexEntry] = {
            entry.page_id: entry for entry in index.entries
        }

    @property
    def index(self) -> SearchIndex:
        """The index this searcher queries."""
        return self._index

    def search(self, query: SearchQuery) -> list[SearchHit]:
        """Return hits for ``query`` in deterministic order.

        Ordering is score descending, then page id ascending for ties.  A
        page is eligible only when it passes every supplied metadata filter
        and the effective-date window.  Pages that pass the filters without
        any text match are returned with a zero score marked ``metadata``
        when an explicit metadata filter was supplied.
        """
        return embedding_operation(
            self._embedder, lambda backend: self._search(query, backend)
        )

    def _search(self, query: SearchQuery, embedder: EmbeddingPort) -> list[SearchHit]:
        if not isinstance(query, SearchQuery):
            raise KnowledgeSearchError("query must be a SearchQuery")
        if self._index.embedding_model and self._index.embedding_model != (
            embedding_identity(embedder)
        ):
            raise KnowledgeSearchError("query embedding model does not match the index")
        query_tokens = frozenset(tokenize(query.text))
        query_vector = embedder.embed(query.text) if query.text.strip() else None
        filter_tags = frozenset(query.tags)
        filter_ids = frozenset(query.page_ids)
        has_metadata_filter = (
            bool(filter_tags) or query.source_url is not None or bool(filter_ids)
        )

        candidates: list[tuple[IndexEntry, set[MatchedVia]]] = []
        for entry in self._index.entries:
            if not _applicable(entry, query.as_of):
                continue
            if filter_tags and not filter_tags.issubset(entry.tags):
                continue
            if query.source_url is not None and entry.source_url != query.source_url:
                continue
            if filter_ids and entry.page_id not in filter_ids:
                continue
            via: set[MatchedVia] = set()
            if has_metadata_filter:
                via.add(MatchedVia.METADATA)
            candidates.append((entry, via))

        lexical: dict[str, float] = {}
        semantic: dict[str, float] = {}
        for entry, via in candidates:
            total = entry.total_terms
            if query_tokens and total > 0:
                overlap = sum(
                    count
                    for token, count in entry.terms.items()
                    if token in query_tokens
                )
                if overlap > 0:
                    lexical[entry.page_id] = overlap / total
                    via.add(MatchedVia.LEXICAL)
            if query_vector is not None and entry.embedding:
                similarity = _cosine(query_vector, entry.embedding)
                if similarity > 0.0:
                    semantic[entry.page_id] = similarity
                    via.add(MatchedVia.SEMANTIC)

        expansion: dict[str, float] = {}
        if lexical:
            expansion = self._expansion_scores(lexical, candidates)
            for entry, via in candidates:
                if entry.page_id in expansion:
                    via.add(MatchedVia.HYPERLINK)

        hits: list[SearchHit] = []
        for entry, via in candidates:
            if not via:
                continue
            score = (
                LEXICAL_WEIGHT * lexical.get(entry.page_id, 0.0)
                + SEMANTIC_WEIGHT * semantic.get(entry.page_id, 0.0)
                + expansion.get(entry.page_id, 0.0)
            )
            hits.append(_hit_from_entry(entry, score, frozenset(via)))
        hits.sort(key=lambda hit: (-hit.score, hit.page_id))
        return hits

    def _expansion_scores(
        self,
        lexical: Mapping[str, float],
        candidates: list[tuple[IndexEntry, set[MatchedVia]]],
    ) -> dict[str, float]:
        """Score candidates linked from or to lexical matches.

        The expansion score of a candidate is :data:`HYPERLINK_WEIGHT` times
        the best lexical score among pages that link to it or that it links
        to.  Pages that already matched lexically are not re-scored.
        """
        referrers: dict[str, set[str]] = {}
        targets_by_page: dict[str, set[str]] = {}
        for entry in self._index.entries:
            targets = set(entry.hyperlink_targets)
            targets_by_page[entry.page_id] = targets
            for target in targets:
                referrers.setdefault(target, set()).add(entry.page_id)
        scores: dict[str, float] = {}
        for entry, _via in candidates:
            if entry.page_id in lexical:
                continue
            adjacent = referrers.get(entry.page_id, set()) | targets_by_page.get(
                entry.page_id, set()
            )
            best = max((lexical.get(page_id, 0.0) for page_id in adjacent), default=0.0)
            if best > 0.0:
                scores[entry.page_id] = HYPERLINK_WEIGHT * best
        return scores


__all__ = [
    "HYPERLINK_WEIGHT",
    "LEXICAL_WEIGHT",
    "SEMANTIC_WEIGHT",
    "EmbeddingPort",
    "HashingEmbedder",
    "KnowledgeSearch",
    "KnowledgeSearchError",
    "MatchedVia",
    "SearchHit",
    "SearchQuery",
]
