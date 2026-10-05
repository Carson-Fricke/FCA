"""Corpus loading and validation for the Global Knowledge Library.

:func:`KnowledgeCorpus.load` walks the canonical Markdown tree, parses every
page with the strict front-matter contract, builds the link graph, and
captures a SHA-256 snapshot of the loaded bytes, allowing a
workflow to cite the exact corpus state it used.

Loading is deliberately two-phase:

- files that do not begin with a ``---`` delimiter are not structured corpus
  pages (corpus READMEs, scratch notes) and are skipped;
- files that begin front matter must satisfy the pinned schema, or
  :class:`thesis.knowledge.metadata.MetadataError` aborts the load.

Structural problems that should not abort a load — duplicate page ids,
dangling links, unresolved relationships, trust mismatches — are collected
and reported by :meth:`CorpusSnapshot.validate` as structured problem lines.
Search indexes are derived, rebuildable data and never canonical content;
nothing here writes them.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from thesis.knowledge.links import LinkGraph, PageNode, normalize_corpus_path
from thesis.knowledge.metadata import (
    PageMetadata,
    TrustState,
    TrustTier,
    parse_front_matter,
)

_DELIMITER = "---"


class CorpusError(ValueError):
    """Raised when the corpus cannot be loaded or a page cannot be resolved."""


@dataclass(frozen=True, slots=True)
class CorpusPage:
    """One canonical corpus page: parsed metadata, verbatim body, and path."""

    metadata: PageMetadata
    body: str
    path: str

    def __post_init__(self) -> None:
        if not isinstance(self.metadata, PageMetadata):
            raise CorpusError("metadata must be a PageMetadata")
        if not isinstance(self.body, str):
            raise CorpusError("body must be a string")
        if not isinstance(self.path, str) or not self.path.strip():
            raise CorpusError("path must be a non-empty string")
        object.__setattr__(self, "path", normalize_corpus_path(self.path))

    @property
    def page_id(self) -> str:
        """The stable page identity."""
        return self.metadata.page_id


class KnowledgeCorpus:
    """A read-only loader over the canonical Markdown corpus at ``root``."""

    def __init__(self, root: Path) -> None:
        if not isinstance(root, Path):
            raise CorpusError("root must be a pathlib.Path")
        self._root = root

    @property
    def root(self) -> Path:
        """The corpus root directory."""
        return self._root

    def load(self) -> CorpusSnapshot:
        """Walk, parse, link, and revision the corpus into a snapshot.

        Pages are every ``*.md`` file under ``root`` (recursively, posix
        relative paths) that begins with a ``---`` front-matter delimiter;
        other Markdown files are not corpus pages and are skipped.  Duplicate
        page ids do not abort the load: the first path wins and the conflict
        is reported by :meth:`CorpusSnapshot.validate`.

        ``revision`` is a ``sha256:`` snapshot ID over sorted relative paths
        and the exact Markdown bytes read, including unstructured Markdown.
        It changes for uncommitted edits and requires no Git installation.
        """
        if not self._root.is_dir():
            raise CorpusError(f"corpus root is not a directory: {self._root}")
        resolved_root = self._root.resolve()
        # Check directories too: rglob does not traverse symlinked directories.
        for candidate in self._root.rglob("*"):
            if not candidate.resolve().is_relative_to(resolved_root):
                raise CorpusError("corpus entry escapes its root")
        pages: dict[str, CorpusPage] = {}
        nodes: list[PageNode] = []
        id_paths: dict[str, str] = {}
        duplicate_ids: list[tuple[str, str, str]] = []
        markdown_files = sorted(
            self._root.rglob("*.md"),
            key=lambda path: path.relative_to(self._root).as_posix(),
        )
        digest = hashlib.sha256()
        for path in markdown_files:
            relative = path.relative_to(self._root).as_posix()
            if not path.resolve().is_relative_to(resolved_root):
                raise CorpusError("corpus page escapes its root")
            data = path.read_bytes()
            name = relative.encode("utf-8")
            digest.update(len(name).to_bytes(8, "big"))
            digest.update(name)
            digest.update(len(data).to_bytes(8, "big"))
            digest.update(data)
            text = data.decode("utf-8")
            if not _has_front_matter(text):
                continue
            metadata, body = parse_front_matter(text)
            page_id = metadata.page_id
            first_path = id_paths.setdefault(page_id, relative)
            if first_path != relative:
                duplicate_ids.append((page_id, first_path, relative))
                continue
            page = CorpusPage(metadata=metadata, body=body, path=relative)
            pages[page_id] = page
            nodes.append(PageNode(path=relative, metadata=metadata))
        graph = LinkGraph(nodes, strict=False)
        revision = f"sha256:{digest.hexdigest()}"
        return CorpusSnapshot(
            root=self._root,
            revision=revision,
            pages=pages,
            graph=graph,
            duplicate_ids=tuple(duplicate_ids),
        )


def _has_front_matter(text: str) -> bool:
    return text.split("\n", 1)[0].strip() == _DELIMITER


@dataclass(frozen=True, slots=True)
class CorpusSnapshot:
    """An immutable view of one corpus byte snapshot (not a Git commit).

    ``pages`` maps page id to page; ``graph`` is the lenient link graph over
    the loaded pages; ``duplicate_ids`` records ``(page_id, first_path,
    duplicate_path)`` conflicts, so ``is_unique_ids`` is False exactly when a
    load saw two files claiming one id.
    """

    root: Path
    revision: str
    pages: Mapping[str, CorpusPage]
    graph: LinkGraph
    duplicate_ids: tuple[tuple[str, str, str], ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.root, Path):
            raise CorpusError("root must be a pathlib.Path")
        if not isinstance(self.revision, str) or not self.revision:
            raise CorpusError("revision must be a non-empty string")
        if not isinstance(self.pages, Mapping):
            raise CorpusError("pages must be a mapping of page id to CorpusPage")
        checked: dict[str, CorpusPage] = {}
        for page_id, page in self.pages.items():
            if not isinstance(page, CorpusPage):
                raise CorpusError("pages must contain CorpusPage values")
            if page.page_id != page_id:
                raise CorpusError(
                    f"pages key {page_id!r} does not match page id {page.page_id!r}"
                )
            checked[page_id] = page
        object.__setattr__(self, "pages", MappingProxyType(dict(checked)))
        if not isinstance(self.graph, LinkGraph):
            raise CorpusError("graph must be a LinkGraph")
        duplicates: list[tuple[str, str, str]] = []
        for entry in self.duplicate_ids:
            if (
                not isinstance(entry, tuple)
                or len(entry) != 3
                or not all(isinstance(part, str) and part for part in entry)
            ):
                raise CorpusError(
                    "duplicate_ids entries must be (page_id, first_path, "
                    "duplicate_path) string triples"
                )
            duplicates.append(entry)
        object.__setattr__(self, "duplicate_ids", tuple(duplicates))

    @property
    def is_unique_ids(self) -> bool:
        """Whether no two loaded files claimed the same page id."""
        return not self.duplicate_ids

    def page(self, path_or_id: str) -> CorpusPage:
        """Return the page with this id or repository-relative corpus path."""
        if not isinstance(path_or_id, str) or not path_or_id.strip():
            raise CorpusError("path_or_id must be a non-empty string")
        direct = self.pages.get(path_or_id)
        if direct is not None:
            return direct
        normalized = normalize_corpus_path(path_or_id)
        for page in self.pages.values():
            if page.path == normalized:
                return page
        raise CorpusError(f"unknown page id or path: {path_or_id!r}")

    def validate(self) -> list[str]:
        """Return structured problem lines for this snapshot, sorted.

        Lines are prefixed ``duplicate-id:``, ``dangling-link:``,
        ``dangling-relationship:``, ``duplicate-relationship:``,
        ``duplicate-path:``, or ``trust-mismatch:``.  An empty list means the
        snapshot is structurally clean.
        """
        problems: list[str] = []
        for page_id, first_path, duplicate_path in sorted(self.duplicate_ids):
            problems.append(
                f"duplicate-id: page id {page_id!r} at {duplicate_path!r} "
                f"(first seen at {first_path!r})"
            )
        problems.extend(self.graph.problems)
        for page_id in sorted(self.pages):
            metadata = self.pages[page_id].metadata
            if (
                metadata.trust_tier is TrustTier.PLATFORM_POLICY
                and metadata.trust_state is not TrustState.PUBLISHED
            ):
                problems.append(
                    f"trust-mismatch: platform_policy page {page_id!r} must be "
                    f"published (state: {metadata.trust_state.value})"
                )
            if (
                metadata.trust_tier is TrustTier.WORKSPACE_PROMOTED
                and metadata.trust_state is TrustState.DRAFT
            ):
                problems.append(
                    f"trust-mismatch: workspace_promoted page {page_id!r} "
                    "cannot be draft"
                )
        return sorted(problems)


__all__ = [
    "CorpusError",
    "CorpusPage",
    "CorpusSnapshot",
    "KnowledgeCorpus",
]
