"""Link and relationship graph over parsed knowledge pages.

This layer resolves the outbound references of parsed pages — declared
``links`` (page ids or repository-relative corpus paths) and typed
``relationships`` — against the corpus identity space, and exposes inbound
accessors:

- :meth:`LinkGraph.backlinks`: pages whose ``links`` reference a page;
- :meth:`LinkGraph.superseded_by`: pages that declare ``supersedes`` on a page;
- :meth:`LinkGraph.contradictions`: pages that declare ``contradicts`` on a page.

By default the graph is strict: a dangling link or relationship target, a
duplicate id or path, a duplicate relationship entry, or an unknown
relationship type raises :class:`LinkError`.  The corpus loader constructs
the graph in lenient mode (``strict=False``) so structural problems are
collected and reported by ``CorpusSnapshot.validate()`` instead of aborting
the load.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from thesis.knowledge.metadata import PageMetadata, RelationshipType


class LinkError(ValueError):
    """Raised when page links or relationships do not resolve or are invalid."""


def normalize_corpus_path(path: str) -> str:
    """Normalize a repository-relative corpus path to posix form."""
    if not isinstance(path, str) or not path.strip():
        raise LinkError("corpus path must be a non-empty string")
    normalized = path.strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def relationship_type(raw: str) -> RelationshipType:
    """Return the relationship type for a raw front-matter string."""
    if not isinstance(raw, str):
        raise LinkError("relationship type must be a string")
    try:
        return RelationshipType(raw.strip())
    except ValueError as error:
        raise LinkError(f"unknown relationship type: {raw!r}") from error


@dataclass(frozen=True, slots=True)
class PageNode:
    """One parsed page as seen by the link layer.

    ``path`` is the repository-relative corpus path of the page, normalized
    to posix form; ``metadata`` carries the page id and its outbound
    references.
    """

    path: str
    metadata: PageMetadata

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not self.path.strip():
            raise LinkError("path must be a non-empty string")
        if not isinstance(self.metadata, PageMetadata):
            raise LinkError("metadata must be a PageMetadata")
        object.__setattr__(self, "path", normalize_corpus_path(self.path))


def _sorted_ids(values: set[str] | None) -> tuple[str, ...]:
    return tuple(sorted(values)) if values else ()


class LinkGraph:
    """Resolution graph over the ids, paths, links, and relationships of pages."""

    _by_id: dict[str, PageNode]
    _by_path: dict[str, PageNode]
    _backlinks: dict[str, set[str]]
    _superseded_by: dict[str, set[str]]
    _contradictions: dict[str, set[str]]

    def __init__(self, nodes: Sequence[PageNode], *, strict: bool = True) -> None:
        self._by_id = {}
        self._by_path = {}
        self._backlinks = {}
        self._superseded_by = {}
        self._contradictions = {}
        problems: list[str] = []
        for node in nodes:
            if not isinstance(node, PageNode):
                raise LinkError("nodes must contain PageNode values")
            self._register(node, strict, problems)
        for node in nodes:
            if self._by_id.get(node.metadata.page_id) is not node:
                continue
            self._register_edges(node, strict, problems)
        self._problems = tuple(problems)

    def _register(self, node: PageNode, strict: bool, problems: list[str]) -> None:
        page_id = node.metadata.page_id
        existing = self._by_id.get(page_id)
        if existing is not None:
            self._record(
                strict,
                problems,
                f"duplicate-id: page id {page_id!r} at {node.path!r} "
                f"(first seen at {existing.path!r})",
            )
            return
        collision = self._by_path.get(node.path)
        if collision is not None:
            self._record(
                strict,
                problems,
                f"duplicate-path: corpus path {node.path!r} is claimed by "
                f"{collision.metadata.page_id!r} and {page_id!r}",
            )
            return
        self._by_id[page_id] = node
        self._by_path[node.path] = node

    def _register_edges(
        self, node: PageNode, strict: bool, problems: list[str]
    ) -> None:
        page_id = node.metadata.page_id
        seen: set[tuple[str, str]] = set()
        for link in node.metadata.links:
            resolved = self.resolve(link)
            if resolved is None:
                self._record(
                    strict,
                    problems,
                    f"dangling-link: {page_id!r} links to {link!r}, which is "
                    "not a corpus page id or path",
                )
                continue
            self._backlinks.setdefault(resolved, set()).add(page_id)
        for entry in node.metadata.relationships:
            key = (entry.type.value, entry.target)
            if key in seen:
                self._record(
                    strict,
                    problems,
                    f"duplicate-relationship: {page_id!r} declares "
                    f"{key[0]} {key[1]!r} more than once",
                )
                continue
            seen.add(key)
            resolved = self.resolve(entry.target)
            if resolved is None:
                self._record(
                    strict,
                    problems,
                    f"dangling-relationship: {page_id!r} {key[0]} {key[1]!r} "
                    "does not resolve to a corpus page",
                )
                continue
            if entry.type is RelationshipType.SUPERSEDES:
                self._superseded_by.setdefault(resolved, set()).add(page_id)
            elif entry.type is RelationshipType.CONTRADICTS:
                self._contradictions.setdefault(resolved, set()).add(page_id)

    @staticmethod
    def _record(strict: bool, problems: list[str], message: str) -> None:
        if strict:
            raise LinkError(message)
        problems.append(message)

    @property
    def problems(self) -> tuple[str, ...]:
        """Structural problem lines collected in lenient mode."""
        return self._problems

    @property
    def page_ids(self) -> tuple[str, ...]:
        """All known page ids, sorted."""
        return tuple(sorted(self._by_id))

    def resolve(self, target: str) -> str | None:
        """Resolve a link or relationship target to a page id, or ``None``."""
        if not isinstance(target, str) or not target.strip():
            return None
        if target in self._by_id:
            return target
        node = self._by_path.get(normalize_corpus_path(target))
        return None if node is None else node.metadata.page_id

    def node(self, page_id: str) -> PageNode:
        """Return the node for ``page_id``; raises :class:`LinkError` if unknown."""
        node = self._by_id.get(page_id) if isinstance(page_id, str) else None
        if node is None:
            raise LinkError(f"unknown page id: {page_id!r}")
        return node

    def backlinks(self, page_id: str) -> tuple[str, ...]:
        """Page ids whose declared ``links`` resolve to ``page_id``, sorted."""
        self.node(page_id)
        return _sorted_ids(self._backlinks.get(page_id))

    def superseded_by(self, page_id: str) -> tuple[str, ...]:
        """Page ids declaring ``supersedes`` on ``page_id``, sorted."""
        self.node(page_id)
        return _sorted_ids(self._superseded_by.get(page_id))

    def contradictions(self, page_id: str) -> tuple[str, ...]:
        """Page ids declaring ``contradicts`` on ``page_id``, sorted."""
        self.node(page_id)
        return _sorted_ids(self._contradictions.get(page_id))


__all__ = [
    "LinkError",
    "LinkGraph",
    "PageNode",
    "normalize_corpus_path",
    "relationship_type",
]
