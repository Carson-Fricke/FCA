"""Optional, rebuildable CLEO initialization projection.

Thesis may seed a workspace's CLEO project with a compact, version-pinned
projection of selected knowledge pages: citation records that name the
Markdown path, Git revision, page version, title, source provenance, and
trust tiers of the exact corpus state a workflow used, plus a bounded
excerpt of the body.  The projection is linked to global Markdown paths and
Git revisions — it is not a second canonical copy of corpus content, and it
must never import or invoke any CLEO ``Nexus`` or ``Brain`` facility:
``Nexus`` is a code-intelligence surface, ``Brain`` is a workspace memory
surface, and bulk-loading the corpus into either would pollute workspace
memory and context.

Every manifest therefore carries an explicit non-canonical guard
(``"cleo_projection": {"nexus": "not-used", "brain": "not-used",
"canonical": false}`` plus the :data:`PROJECTION_IS_NOT_CANONICAL` note),
and :meth:`ProjectionManifest.from_json` rejects any document whose guard or
note has been altered.

The projection is optional and fully rebuildable: :func:`build_projection`
and :func:`rebuild` regenerate the whole manifest from corpus state and keep
no incremental state.  Records are bounded citations — at most
:data:`EXCERPT_CHARS` body characters, never the page body — and
serialization is deterministic JSON with sorted keys, so identical inputs
produce byte-identical output.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Protocol

from thesis.knowledge.metadata import PageMetadata, TrustTier
from thesis.knowledge.trust import (
    TIER_PRECEDENCE,
    WorkspaceTrustOverlay,
    resolve_effective_tier,
)

_PROJECTION_FORMAT_VERSION: Final[int] = 1

_SLUG_RE: Final = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_HEX64_RE: Final = re.compile(r"^[0-9a-f]{64}$")

#: Maximum number of body characters carried by one record.  Records are
#: citations into the corpus, never content copies.
EXCERPT_CHARS: Final[int] = 200

#: The non-canonical note carried by every projection manifest.
PROJECTION_IS_NOT_CANONICAL: Final[str] = (
    "This projection is a derived, rebuildable citation manifest over the "
    "global knowledge corpus. It is never a second canonical copy of "
    "knowledge content and must never be treated as canonical."
)

#: The non-canonical CLEO-usage guard carried by every projection manifest:
#: the projection never uses CLEO Nexus or Brain and is never canonical.
NON_CANONICAL_GUARD: Final[Mapping[str, object]] = MappingProxyType(
    {"nexus": "not-used", "brain": "not-used", "canonical": False}
)

assert NON_CANONICAL_GUARD["nexus"] == "not-used"
assert NON_CANONICAL_GUARD["brain"] == "not-used"
assert NON_CANONICAL_GUARD["canonical"] is False
assert EXCERPT_CHARS > 0


class ProjectionError(ValueError):
    """Raised when projection inputs or serialized manifests are invalid."""


class ProjectionPage(Protocol):
    """Structural view of one corpus page used by the projection."""

    @property
    def metadata(self) -> PageMetadata:
        """The validated page metadata."""
        ...

    @property
    def body(self) -> str:
        """The verbatim page body."""
        ...

    @property
    def path(self) -> str:
        """The repository-relative corpus path of the page."""
        ...


class ProjectionCorpus(Protocol):
    """Structural view of a corpus snapshot used by the projection."""

    @property
    def pages(self) -> Mapping[str, ProjectionPage]:
        """Every loaded page, keyed by page id."""
        ...


@dataclass(frozen=True, slots=True)
class ProjectionRecord:
    """One citation record: a bounded pointer into the global corpus.

    A record never carries the page body — only the citation fields and a
    bounded excerpt of at most :data:`EXCERPT_CHARS` characters.
    """

    page_id: str
    path: str
    revision: str
    version: int
    title: str
    source_url: str
    source_content_hash: str
    global_tier: TrustTier
    effective_tier: TrustTier
    excerpt: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.page_id, str)
            or _SLUG_RE.fullmatch(self.page_id) is None
        ):
            raise ProjectionError("page_id must be a kebab-case slug")
        if not isinstance(self.path, str) or not self.path.strip():
            raise ProjectionError("path must be a non-empty string")
        if not isinstance(self.revision, str) or not self.revision:
            raise ProjectionError("revision must be a non-empty string")
        if (
            isinstance(self.version, bool)
            or not isinstance(self.version, int)
            or self.version < 1
        ):
            raise ProjectionError("version must be a positive integer")
        if not isinstance(self.title, str) or not self.title:
            raise ProjectionError("title must be a non-empty string")
        if not isinstance(self.source_url, str) or not self.source_url:
            raise ProjectionError("source_url must be a non-empty string")
        if (
            not isinstance(self.source_content_hash, str)
            or _HEX64_RE.fullmatch(self.source_content_hash) is None
        ):
            raise ProjectionError(
                "source_content_hash must be a lowercase 64-character hex digest"
            )
        if not isinstance(self.global_tier, TrustTier):
            raise ProjectionError("global_tier must be a TrustTier")
        if not isinstance(self.effective_tier, TrustTier):
            raise ProjectionError("effective_tier must be a TrustTier")
        if TIER_PRECEDENCE[self.effective_tier] < TIER_PRECEDENCE[self.global_tier]:
            raise ProjectionError(
                "effective_tier cannot be below the page's global tier"
            )
        if not isinstance(self.excerpt, str):
            raise ProjectionError("excerpt must be a string")
        if len(self.excerpt) > EXCERPT_CHARS:
            raise ProjectionError(f"excerpt must be at most {EXCERPT_CHARS} characters")


@dataclass(frozen=True, slots=True)
class ProjectionManifest:
    """A compact, version-pinned citation manifest over one corpus revision.

    Records are ordered by page id ascending, each pins the manifest's Git
    revision, and every serialization carries the non-canonical guard and
    note.
    """

    git_revision: str
    records: tuple[ProjectionRecord, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.git_revision, str) or not self.git_revision:
            raise ProjectionError("git_revision must be a non-empty string")
        if not isinstance(self.records, tuple):
            raise ProjectionError("records must be a tuple of ProjectionRecord")
        seen: set[str] = set()
        previous: str | None = None
        for record in self.records:
            if not isinstance(record, ProjectionRecord):
                raise ProjectionError("records must contain ProjectionRecord values")
            if record.page_id in seen:
                raise ProjectionError(f"duplicate projection record: {record.page_id}")
            if record.revision != self.git_revision:
                raise ProjectionError(
                    f"record {record.page_id!r} cites revision "
                    f"{record.revision!r} but the manifest pins "
                    f"{self.git_revision!r}"
                )
            if previous is not None and record.page_id <= previous:
                raise ProjectionError("records must be ordered by page id ascending")
            seen.add(record.page_id)
            previous = record.page_id

    @property
    def guard(self) -> Mapping[str, object]:
        """The non-canonical CLEO-usage guard of every manifest."""
        return NON_CANONICAL_GUARD

    @property
    def note(self) -> str:
        """The non-canonical note of every manifest."""
        return PROJECTION_IS_NOT_CANONICAL

    def to_json(self) -> bytes:
        """Return the canonical byte serialization of this manifest.

        JSON with sorted keys: identical manifests serialize to identical
        bytes.
        """
        payload = {
            "format_version": _PROJECTION_FORMAT_VERSION,
            "git_revision": self.git_revision,
            "cleo_projection": dict(NON_CANONICAL_GUARD),
            "note": PROJECTION_IS_NOT_CANONICAL,
            "records": [_record_payload(record) for record in self.records],
        }
        return json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")

    @classmethod
    def from_json(cls, data: bytes) -> ProjectionManifest:
        """Rebuild a manifest from :meth:`to_json` bytes.

        The format version, non-canonical guard, note, record shapes,
        ordering, and revision consistency are all re-validated, so a
        tampered or hand-edited document is rejected.
        """
        if not isinstance(data, (bytes, bytearray)):
            raise ProjectionError("projection document must be bytes")
        try:
            payload = json.loads(bytes(data).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ProjectionError(
                "projection document must be serialized manifest JSON"
            ) from error
        if not isinstance(payload, dict):
            raise ProjectionError("projection payload must be a JSON object")
        if payload.get("format_version") != _PROJECTION_FORMAT_VERSION:
            raise ProjectionError(
                f"unsupported projection format version: "
                f"{payload.get('format_version')!r}"
            )
        _verify_guard(payload.get("cleo_projection"))
        if payload.get("note") != PROJECTION_IS_NOT_CANONICAL:
            raise ProjectionError(
                "projection payload 'note' does not match the non-canonical note"
            )
        revision = payload.get("git_revision")
        if not isinstance(revision, str) or not revision:
            raise ProjectionError("git_revision must be a non-empty string")
        raw_records = payload.get("records")
        if not isinstance(raw_records, list):
            raise ProjectionError("projection payload 'records' must be a JSON array")
        records = tuple(_record_from_payload(raw) for raw in raw_records)
        return cls(git_revision=revision, records=records)


def build_projection(
    snapshot: ProjectionCorpus | Mapping[str, ProjectionPage],
    overlay: WorkspaceTrustOverlay | None,
    *,
    git_revision: str,
    max_pages: int | None = None,
) -> ProjectionManifest:
    """Build the citation manifest for selected pages of ``snapshot``.

    ``snapshot`` may be a :class:`ProjectionCorpus` (a corpus snapshot) or a
    plain page-id to page mapping.  Records are ordered by page id ascending
    and each carries the Markdown path, ``git_revision``, page version,
    title, source URL, source content hash, global trust tier, effective
    workspace tier, and a bounded excerpt of at most :data:`EXCERPT_CHARS`
    body characters — a citation, never a content copy.

    ``overlay`` only interprets: a ``None`` overlay, and any page absent
    from the overlay, keep their global tier.  ``max_pages`` truncates the
    selection deterministically (first ``max_pages`` ids ascending); ``None``
    keeps every page.  Identical inputs produce byte-identical
    :meth:`ProjectionManifest.to_json` output.
    """
    if not isinstance(git_revision, str) or not git_revision:
        raise ProjectionError("git_revision must be a non-empty string")
    if overlay is not None and not isinstance(overlay, WorkspaceTrustOverlay):
        raise ProjectionError("overlay must be a WorkspaceTrustOverlay or None")
    if max_pages is not None and (
        isinstance(max_pages, bool) or not isinstance(max_pages, int) or max_pages < 0
    ):
        raise ProjectionError("max_pages must be a non-negative integer or None")
    pages = _snapshot_pages(snapshot)
    selected_ids = sorted(pages)
    if max_pages is not None:
        selected_ids = selected_ids[:max_pages]
    records: list[ProjectionRecord] = []
    for page_id in selected_ids:
        metadata, body, path = _page_parts(page_id, pages[page_id])
        decision = overlay.promotion_for(page_id) if overlay is not None else None
        effective = resolve_effective_tier(page_id, metadata.trust_tier, decision)
        records.append(
            ProjectionRecord(
                page_id=page_id,
                path=path,
                revision=git_revision,
                version=metadata.version,
                title=metadata.title,
                source_url=metadata.source_url,
                source_content_hash=metadata.source_content_hash,
                global_tier=metadata.trust_tier,
                effective_tier=effective.tier,
                excerpt=body[:EXCERPT_CHARS],
            )
        )
    return ProjectionManifest(git_revision=git_revision, records=tuple(records))


def rebuild(
    snapshot: ProjectionCorpus | Mapping[str, ProjectionPage],
    overlay: WorkspaceTrustOverlay | None,
    *,
    git_revision: str,
    max_pages: int | None = None,
) -> ProjectionManifest:
    """Regenerate the whole projection manifest from corpus state.

    The projection is optional and rebuildable: this entry point keeps no
    incremental state and consults nothing but the supplied corpus snapshot,
    overlay, and revision, so identical inputs regenerate a byte-identical
    manifest and a workspace can drop its projection and rebuild it at any
    time.
    """
    return build_projection(
        snapshot, overlay, git_revision=git_revision, max_pages=max_pages
    )


def _snapshot_pages(
    snapshot: ProjectionCorpus | Mapping[str, ProjectionPage],
) -> Mapping[str, ProjectionPage]:
    if isinstance(snapshot, Mapping):
        return snapshot
    return snapshot.pages


def _page_parts(page_id: str, page: ProjectionPage) -> tuple[PageMetadata, str, str]:
    metadata = page.metadata
    body = page.body
    path = page.path
    if (
        not isinstance(metadata, PageMetadata)
        or not isinstance(body, str)
        or not isinstance(path, str)
        or not path
    ):
        raise ProjectionError(
            f"corpus page {page_id!r} must expose metadata, body, and path"
        )
    if metadata.page_id != page_id:
        raise ProjectionError(
            f"pages key {page_id!r} does not match page id {metadata.page_id!r}"
        )
    return metadata, body, path


def _verify_guard(raw: object) -> None:
    if not isinstance(raw, dict) or dict(raw) != dict(NON_CANONICAL_GUARD):
        raise ProjectionError(
            "projection guard must record "
            f"{dict(NON_CANONICAL_GUARD)!r}: the projection never uses CLEO "
            "Nexus or Brain and is never canonical"
        )


def _record_payload(record: ProjectionRecord) -> dict[str, object]:
    return {
        "page_id": record.page_id,
        "path": record.path,
        "revision": record.revision,
        "version": record.version,
        "title": record.title,
        "source_url": record.source_url,
        "source_content_hash": record.source_content_hash,
        "global_tier": record.global_tier.value,
        "effective_tier": record.effective_tier.value,
        "excerpt": record.excerpt,
    }


def _record_from_payload(raw: object) -> ProjectionRecord:
    if not isinstance(raw, dict):
        raise ProjectionError("projection records must be JSON objects")
    required = (
        "page_id",
        "path",
        "revision",
        "version",
        "title",
        "source_url",
        "source_content_hash",
        "global_tier",
        "effective_tier",
        "excerpt",
    )
    for key in required:
        if key not in raw:
            raise ProjectionError(f"projection record is missing {key!r}")
    try:
        global_tier = TrustTier(raw["global_tier"])
        effective_tier = TrustTier(raw["effective_tier"])
    except ValueError as error:
        raise ProjectionError(f"unknown trust tier: {error}") from error
    version = raw["version"]
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise ProjectionError("version must be a positive integer")
    excerpt = raw["excerpt"]
    if not isinstance(excerpt, str):
        raise ProjectionError("excerpt must be a string")
    return ProjectionRecord(
        page_id=raw["page_id"],
        path=raw["path"],
        revision=raw["revision"],
        version=version,
        title=raw["title"],
        source_url=raw["source_url"],
        source_content_hash=raw["source_content_hash"],
        global_tier=global_tier,
        effective_tier=effective_tier,
        excerpt=excerpt,
    )


__all__ = [
    "EXCERPT_CHARS",
    "NON_CANONICAL_GUARD",
    "PROJECTION_IS_NOT_CANONICAL",
    "ProjectionCorpus",
    "ProjectionError",
    "ProjectionManifest",
    "ProjectionPage",
    "ProjectionRecord",
    "build_projection",
    "rebuild",
]
