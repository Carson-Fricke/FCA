"""Rebuildable knowledge-search index data.

The Global Knowledge Library keeps its canonical content in versioned
Markdown; the search index is derived, rebuildable data and never a second
canonical copy.  This module holds the pure-python index structures:

- the :class:`EmbeddingPort` protocol that decouples the index and search
  layers from any concrete embedding implementation;
- the shared vocabulary (trust tiers and states, relationship kinds) used by
  ingestion, indexing, and search; and
- :func:`build_index` / :meth:`SearchIndex.serialize` / :func:`load_index`,
  which are deterministic: the same pages, embedder, and corpus revision
  always produce a byte-identical serialization.

No heavy index dependency is permitted here: tokenization, term counts, and
cosine-ready vectors are plain python structures, and serialization is JSON.
Embeddings are stored at fixed six-decimal precision so that repeated builds
and round-trips are exact.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

#: Fixed decimal places for stored embedding values; keeps serialization
#: byte-identical across rebuilds and exact across load round-trips.
EMBEDDING_DECIMALS = 6

_INDEX_FORMAT_VERSION = 1


class KnowledgeIndexError(ValueError):
    """Raised when page records or serialized index data are invalid."""


class EmbeddingPort(Protocol):
    """A replaceable text-embedding boundary.

    Production may back this with any embedding service; the default
    dependency-free implementation is
    :class:`thesis.knowledge.search.HashingEmbedder`.
    """

    def embed(self, text: str) -> list[float]:
        """Return one embedding vector for ``text``."""
        ...


def embedding_identity(embedder: EmbeddingPort) -> str:
    """Identify the vector space, including backend-specific configuration."""
    return str(
        getattr(embedder, "cache_identity", None)
        or getattr(embedder, "model_id", None)
        or f"{type(embedder).__module__}.{type(embedder).__qualname__}"
    )


def embedding_operation[T](
    embedder: EmbeddingPort, operation: Callable[[EmbeddingPort], T]
) -> T:
    """Bind optional fallback providers for the entire operation, not each text."""
    runner = getattr(embedder, "run_operation", None)
    return runner(operation) if runner is not None else operation(embedder)


class RelationshipKind(StrEnum):
    """The typed relationships a knowledge page may declare."""

    SUPERSEDES = "supersedes"
    CONTRADICTS = "contradicts"
    RELATES = "relates"


class TrustTier(StrEnum):
    """The knowledge-library trust tiers."""

    UNTRUSTED = "untrusted"
    CURATED = "curated"
    WORKSPACE_PROMOTED = "workspace_promoted"
    PLATFORM_POLICY = "platform_policy"


class TrustState(StrEnum):
    """The curation lifecycle state of a knowledge page."""

    DRAFT = "draft"
    PROPOSED = "proposed"
    PUBLISHED = "published"


@dataclass(frozen=True, slots=True)
class Relationship:
    """One typed relationship from a page to another corpus identity."""

    kind: RelationshipKind
    target: str

    def __post_init__(self) -> None:
        if not isinstance(self.kind, RelationshipKind):
            raise KnowledgeIndexError("relationship kind must be a RelationshipKind")
        if not isinstance(self.target, str) or not self.target:
            raise KnowledgeIndexError("relationship target must be a non-empty string")


def tokenize(text: str) -> list[str]:
    """Lowercase and split ``text`` into alphanumeric lexical tokens."""
    if not isinstance(text, str):
        raise KnowledgeIndexError("text must be a string")
    return _TOKEN_RE.findall(text.lower())


def _token_counts(text: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for token in tokenize(text):
        counts[token] = counts.get(token, 0) + 1
    return counts


def _checked_slug(value: str, label: str) -> str:
    if not isinstance(value, str) or _SLUG_RE.fullmatch(value) is None:
        raise KnowledgeIndexError(f"{label} must be a kebab-case slug")
    return value


def _checked_hex64(value: str, label: str) -> str:
    if not isinstance(value, str) or _HEX64_RE.fullmatch(value) is None:
        raise KnowledgeIndexError(f"{label} must be a 64-character hex digest")
    return value


def _checked_positive_int(value: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise KnowledgeIndexError(f"{label} must be a positive integer")
    return value


def _checked_date(value: date, label: str) -> date:
    if not isinstance(value, date):
        raise KnowledgeIndexError(f"{label} must be a datetime.date")
    return value


def _checked_optional_date(value: date | None, label: str) -> date | None:
    if value is None:
        return None
    return _checked_date(value, label)


def _checked_str_tuple(values: Sequence[str], label: str) -> tuple[str, ...]:
    prepared: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value:
            raise KnowledgeIndexError(f"{label} must contain non-empty strings")
        prepared.append(value)
    return tuple(prepared)


def _checked_collections(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, str) or not isinstance(values, (tuple, list)):
        raise KnowledgeIndexError("collections must be a tuple of strings")
    prepared: list[str] = []
    for value in values:
        if not isinstance(value, str) or _SLUG_RE.fullmatch(value) is None:
            raise KnowledgeIndexError("collections must be kebab-case tokens")
        if value in prepared:
            raise KnowledgeIndexError(f"duplicate collection: {value!r}")
        prepared.append(value)
    return tuple(prepared)


@dataclass(frozen=True, slots=True)
class PageRecord:
    """One canonical page as seen by the index and search layers.

    ``revision`` is the Git revision of the corpus state the record came
    from, so search results can cite the exact corpus state they used.
    """

    page_id: str
    path: str
    revision: str
    version: int
    title: str
    body: str
    created: date
    retrieved: date
    source_url: str
    source_content_hash: str
    effective_start: date | None = None
    effective_end: date | None = None
    source_publisher: str | None = None
    tags: tuple[str, ...] = ()
    collections: tuple[str, ...] = ()
    links: tuple[str, ...] = ()
    relationships: tuple[Relationship, ...] = ()
    trust_tier: TrustTier = TrustTier.UNTRUSTED
    trust_state: TrustState = TrustState.DRAFT

    def __post_init__(self) -> None:
        _checked_slug(self.page_id, "page_id")
        if not isinstance(self.path, str) or not self.path:
            raise KnowledgeIndexError("path must be a non-empty string")
        if not isinstance(self.revision, str) or not self.revision:
            raise KnowledgeIndexError("revision must be a non-empty string")
        _checked_positive_int(self.version, "version")
        if not isinstance(self.title, str) or not self.title:
            raise KnowledgeIndexError("title must be a non-empty string")
        if not isinstance(self.body, str):
            raise KnowledgeIndexError("body must be a string")
        _checked_date(self.created, "created")
        _checked_date(self.retrieved, "retrieved")
        if not isinstance(self.source_url, str) or not self.source_url:
            raise KnowledgeIndexError("source_url must be a non-empty string")
        _checked_hex64(self.source_content_hash, "source_content_hash")
        _checked_optional_date(self.effective_start, "effective_start")
        _checked_optional_date(self.effective_end, "effective_end")
        if self.effective_start is not None and self.effective_end is not None:
            if self.effective_start > self.effective_end:
                raise KnowledgeIndexError(
                    "effective_start must not be after effective_end"
                )
        if self.source_publisher is not None and not isinstance(
            self.source_publisher, str
        ):
            raise KnowledgeIndexError("source_publisher must be a string or None")
        object.__setattr__(self, "tags", _checked_str_tuple(self.tags, "tags"))
        object.__setattr__(self, "collections", _checked_collections(self.collections))
        object.__setattr__(self, "links", _checked_str_tuple(self.links, "links"))
        for relationship in self.relationships:
            if not isinstance(relationship, Relationship):
                raise KnowledgeIndexError(
                    "relationships must contain Relationship values"
                )
        if not isinstance(self.trust_tier, TrustTier):
            raise KnowledgeIndexError("trust_tier must be a TrustTier")
        if not isinstance(self.trust_state, TrustState):
            raise KnowledgeIndexError("trust_state must be a TrustState")

    @property
    def hyperlink_targets(self) -> tuple[str, ...]:
        """Every outbound target: declared links plus relationship targets."""
        targets = list(self.links)
        targets.extend(relationship.target for relationship in self.relationships)
        return tuple(dict.fromkeys(targets))


@dataclass(frozen=True, slots=True)
class IndexEntry:
    """The derived, searchable representation of one page."""

    page_id: str
    path: str
    revision: str
    version: int
    title: str
    created: date
    retrieved: date
    effective_start: date | None
    effective_end: date | None
    source_url: str
    source_publisher: str | None
    source_content_hash: str
    tags: tuple[str, ...]
    links: tuple[str, ...]
    relationships: tuple[Relationship, ...]
    trust_tier: TrustTier
    trust_state: TrustState
    collections: tuple[str, ...] = ()
    terms: Mapping[str, int] = field(default_factory=dict)
    embedding: tuple[float, ...] = ()

    def __post_init__(self) -> None:
        _checked_slug(self.page_id, "page_id")
        if not isinstance(self.path, str) or not self.path:
            raise KnowledgeIndexError("path must be a non-empty string")
        if not isinstance(self.revision, str) or not self.revision:
            raise KnowledgeIndexError("revision must be a non-empty string")
        _checked_positive_int(self.version, "version")
        if not isinstance(self.title, str) or not self.title:
            raise KnowledgeIndexError("title must be a non-empty string")
        _checked_date(self.created, "created")
        _checked_date(self.retrieved, "retrieved")
        _checked_optional_date(self.effective_start, "effective_start")
        _checked_optional_date(self.effective_end, "effective_end")
        if not isinstance(self.source_url, str) or not self.source_url:
            raise KnowledgeIndexError("source_url must be a non-empty string")
        if self.source_publisher is not None and not isinstance(
            self.source_publisher, str
        ):
            raise KnowledgeIndexError("source_publisher must be a string or None")
        _checked_hex64(self.source_content_hash, "source_content_hash")
        object.__setattr__(self, "tags", _checked_str_tuple(self.tags, "tags"))
        object.__setattr__(self, "links", _checked_str_tuple(self.links, "links"))
        object.__setattr__(self, "collections", _checked_collections(self.collections))
        for relationship in self.relationships:
            if not isinstance(relationship, Relationship):
                raise KnowledgeIndexError(
                    "relationships must contain Relationship values"
                )
        if not isinstance(self.trust_tier, TrustTier):
            raise KnowledgeIndexError("trust_tier must be a TrustTier")
        if not isinstance(self.trust_state, TrustState):
            raise KnowledgeIndexError("trust_state must be a TrustState")
        terms = dict(self.terms)
        for token, count in terms.items():
            if not isinstance(token, str) or not token:
                raise KnowledgeIndexError("terms keys must be non-empty strings")
            if isinstance(count, bool) or not isinstance(count, int) or count < 1:
                raise KnowledgeIndexError("terms counts must be positive integers")
        object.__setattr__(self, "terms", MappingProxyType(terms))
        embedding = tuple(float(value) for value in self.embedding)
        object.__setattr__(self, "embedding", embedding)

    @property
    def hyperlink_targets(self) -> tuple[str, ...]:
        """Every outbound target: declared links plus relationship targets."""
        targets = list(self.links)
        targets.extend(relationship.target for relationship in self.relationships)
        return tuple(dict.fromkeys(targets))

    @property
    def total_terms(self) -> int:
        """The total number of lexical tokens counted for the page."""
        return sum(self.terms.values())


@dataclass(frozen=True, slots=True)
class SearchIndex:
    """A deterministic, rebuildable index over one corpus revision."""

    corpus_revision: str
    entries: tuple[IndexEntry, ...]
    embedding_model: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.corpus_revision, str) or not self.corpus_revision:
            raise KnowledgeIndexError("corpus_revision must be a non-empty string")
        seen: set[str] = set()
        width: int | None = None
        for entry in self.entries:
            if not isinstance(entry, IndexEntry):
                raise KnowledgeIndexError("entries must contain IndexEntry values")
            if entry.page_id in seen:
                raise KnowledgeIndexError(f"duplicate index entry: {entry.page_id}")
            seen.add(entry.page_id)
            if width is not None and len(entry.embedding) != width:
                raise KnowledgeIndexError("index contains mixed embedding dimensions")
            width = len(entry.embedding)

    def serialize(self) -> bytes:
        """Return the canonical byte serialization of this index.

        JSON with sorted keys and embeddings rounded to
        :data:`EMBEDDING_DECIMALS`; identical indexes serialize to identical
        bytes.
        """
        payload = {
            "format_version": _INDEX_FORMAT_VERSION,
            "corpus_revision": self.corpus_revision,
            "entries": [_entry_payload(entry) for entry in self.entries],
        }
        if self.embedding_model:
            payload["embedding_model"] = self.embedding_model
        text = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        return text.encode("utf-8")


def _entry_payload(entry: IndexEntry) -> dict[str, object]:
    payload: dict[str, object] = {
        "page_id": entry.page_id,
        "path": entry.path,
        "revision": entry.revision,
        "version": entry.version,
        "title": entry.title,
        "created": entry.created.isoformat(),
        "retrieved": entry.retrieved.isoformat(),
        "effective_start": (
            None if entry.effective_start is None else entry.effective_start.isoformat()
        ),
        "effective_end": (
            None if entry.effective_end is None else entry.effective_end.isoformat()
        ),
        "source_url": entry.source_url,
        "source_publisher": entry.source_publisher,
        "source_content_hash": entry.source_content_hash,
        "tags": list(entry.tags),
        "links": list(entry.links),
        "relationships": [
            {"kind": relationship.kind.value, "target": relationship.target}
            for relationship in entry.relationships
        ],
        "trust_tier": entry.trust_tier.value,
        "trust_state": entry.trust_state.value,
        "terms": dict(entry.terms),
        "embedding": [round(value, EMBEDDING_DECIMALS) for value in entry.embedding],
    }
    # Emit the key only when non-empty: pre-collections index bytes stay stable.
    if entry.collections:
        payload["collections"] = list(entry.collections)
    return payload


def _date_from_payload(value: object, label: str) -> date:
    if not isinstance(value, str):
        raise KnowledgeIndexError(f"{label} must be an ISO date string")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise KnowledgeIndexError(f"{label} must be an ISO date string") from error


def _entry_from_payload(payload: object) -> IndexEntry:
    if not isinstance(payload, dict):
        raise KnowledgeIndexError("index entries must be JSON objects")
    required = (
        "page_id",
        "path",
        "revision",
        "version",
        "title",
        "created",
        "retrieved",
        "source_url",
        "source_content_hash",
        "trust_tier",
        "trust_state",
        "terms",
        "embedding",
    )
    for key in required:
        if key not in payload:
            raise KnowledgeIndexError(f"index entry is missing {key!r}")
    relationships: list[Relationship] = []
    for raw in payload.get("relationships", ()):
        if not isinstance(raw, dict) or "kind" not in raw or "target" not in raw:
            raise KnowledgeIndexError("relationships must be kind/target objects")
        try:
            kind = RelationshipKind(raw["kind"])
        except ValueError as error:
            raise KnowledgeIndexError(
                f"unknown relationship kind: {raw['kind']!r}"
            ) from error
        relationships.append(Relationship(kind=kind, target=raw["target"]))
    try:
        trust_tier = TrustTier(payload["trust_tier"])
        trust_state = TrustState(payload["trust_state"])
    except ValueError as error:
        raise KnowledgeIndexError(f"unknown trust vocabulary: {error}") from error
    raw_terms = payload["terms"]
    if not isinstance(raw_terms, dict):
        raise KnowledgeIndexError("terms must be a JSON object")
    raw_embedding = payload["embedding"]
    if not isinstance(raw_embedding, list):
        raise KnowledgeIndexError("embedding must be a JSON array")
    return IndexEntry(
        page_id=payload["page_id"],
        path=payload["path"],
        revision=payload["revision"],
        version=_checked_positive_int(payload["version"], "version"),
        title=payload["title"],
        created=_date_from_payload(payload["created"], "created"),
        retrieved=_date_from_payload(payload["retrieved"], "retrieved"),
        effective_start=(
            None
            if payload.get("effective_start") is None
            else _date_from_payload(payload["effective_start"], "effective_start")
        ),
        effective_end=(
            None
            if payload.get("effective_end") is None
            else _date_from_payload(payload["effective_end"], "effective_end")
        ),
        source_url=payload["source_url"],
        source_publisher=payload.get("source_publisher"),
        source_content_hash=payload["source_content_hash"],
        tags=tuple(payload.get("tags", ())),
        links=tuple(payload.get("links", ())),
        relationships=tuple(relationships),
        trust_tier=trust_tier,
        trust_state=trust_state,
        collections=tuple(payload.get("collections", ())),
        terms=raw_terms,
        embedding=tuple(raw_embedding),
    )


def load_index(data: bytes) -> SearchIndex:
    """Rebuild a :class:`SearchIndex` from :meth:`SearchIndex.serialize` bytes."""
    if not isinstance(data, (bytes, bytearray)):
        raise KnowledgeIndexError("index data must be bytes")
    try:
        payload = json.loads(bytes(data).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise KnowledgeIndexError("index data must be serialized index JSON") from error
    if not isinstance(payload, dict):
        raise KnowledgeIndexError("index payload must be a JSON object")
    if payload.get("format_version") != _INDEX_FORMAT_VERSION:
        raise KnowledgeIndexError(
            f"unsupported index format version: {payload.get('format_version')!r}"
        )
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, list):
        raise KnowledgeIndexError("index payload entries must be a JSON array")
    entries = tuple(_entry_from_payload(raw) for raw in raw_entries)
    revision = payload.get("corpus_revision")
    if not isinstance(revision, str) or not revision:
        raise KnowledgeIndexError("corpus_revision must be a non-empty string")
    model = payload.get("embedding_model", "")
    if not isinstance(model, str):
        raise KnowledgeIndexError("embedding_model must be a string")
    return SearchIndex(corpus_revision=revision, entries=entries, embedding_model=model)


def build_index(
    pages: Sequence[PageRecord],
    embedder: EmbeddingPort,
    corpus_revision: str,
) -> SearchIndex:
    """Build a deterministic search index over ``pages``.

    The same pages, embedder, and corpus revision always produce the same
    index: entries are sorted by page id, terms are lowercased token counts
    over title and body, and embeddings are stored at
    :data:`EMBEDDING_DECIMALS` precision.
    """
    return embedding_operation(
        embedder, lambda backend: _build_index(pages, backend, corpus_revision)
    )


def _build_index(
    pages: Sequence[PageRecord], embedder: EmbeddingPort, corpus_revision: str
) -> SearchIndex:
    if not isinstance(corpus_revision, str) or not corpus_revision:
        raise KnowledgeIndexError("corpus_revision must be a non-empty string")
    entries: list[IndexEntry] = []
    for page in pages:
        if not isinstance(page, PageRecord):
            raise KnowledgeIndexError("pages must contain PageRecord values")
        terms = _token_counts(f"{page.title}\n{page.body}")
        vector = embedder.embed(f"{page.title}\n{page.body}")
        if not isinstance(vector, list) or not all(
            isinstance(value, (int, float)) and not isinstance(value, bool)
            for value in vector
        ):
            raise KnowledgeIndexError(
                f"embedder returned a non-numeric vector for {page.page_id}"
            )
        entries.append(
            IndexEntry(
                page_id=page.page_id,
                path=page.path,
                revision=page.revision,
                version=page.version,
                title=page.title,
                created=page.created,
                retrieved=page.retrieved,
                effective_start=page.effective_start,
                effective_end=page.effective_end,
                source_url=page.source_url,
                source_publisher=page.source_publisher,
                source_content_hash=page.source_content_hash,
                tags=page.tags,
                links=page.links,
                relationships=page.relationships,
                trust_tier=page.trust_tier,
                trust_state=page.trust_state,
                collections=page.collections,
                terms=terms,
                embedding=tuple(
                    round(float(value), EMBEDDING_DECIMALS) for value in vector
                ),
            )
        )
    entries.sort(key=lambda entry: entry.page_id)
    return SearchIndex(
        corpus_revision=corpus_revision,
        entries=tuple(entries),
        embedding_model=embedding_identity(embedder),
    )


__all__ = [
    "EMBEDDING_DECIMALS",
    "EmbeddingPort",
    "IndexEntry",
    "KnowledgeIndexError",
    "PageRecord",
    "Relationship",
    "RelationshipKind",
    "SearchIndex",
    "TrustState",
    "TrustTier",
    "build_index",
    "load_index",
    "tokenize",
]
