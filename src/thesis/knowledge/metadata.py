"""Canonical page metadata for the Global Knowledge Library.

Every corpus page is a Git-versioned ``*.md`` file whose body is free
Markdown and whose structured metadata lives in a ``---``-delimited YAML
front-matter block.  This module is the strict, dependency-free contract for
that block:

- :class:`PageMetadata`, the validated typed form of the pinned schema
  (dates as :class:`datetime.date`, trust and relationship vocabularies as
  validated enums);
- :func:`parse_front_matter`, which parses a page into
  ``(metadata, body)`` using a minimal YAML-subset parser (flat mappings,
  string lists, one nested mapping level, list-of-mappings relationships,
  quoted or unquoted scalars, and ISO dates); and
- :func:`render_front_matter`, which renders metadata back to canonical
  deterministic front matter so that
  ``parse(render(parse(text)))`` is semantically identical to
  ``parse(text)``.

The parser accepts exactly the pinned schema.  Missing or ill-formed front
matter, unknown fields, missing required fields, wrong value types, unknown
enum values, and duplicate keys all raise :class:`MetadataError`.  No YAML
library is used or permitted here.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Final
from urllib.parse import urlparse

_DELIMITER = "---"

_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):(.*)$")
_MAPPING_ITEM_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*:(\s|$)")
_UNESCAPE_RE = re.compile(r"\\([\\\"])")


class MetadataError(ValueError):
    """Raised when page front matter is missing, ill-formed, or invalid."""


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


class RelationshipType(StrEnum):
    """The typed relationships a knowledge page may declare."""

    SUPERSEDES = "supersedes"
    CONTRADICTS = "contradicts"
    RELATES = "relates"


_REQUIRED_FIELDS: Final = (
    "id",
    "title",
    "version",
    "created",
    "retrieved",
    "source",
    "tags",
    "links",
    "relationships",
    "trust",
)
_OPTIONAL_FIELDS: Final = ("effective_start", "effective_end")
_KNOWN_FIELDS: Final = frozenset((*_REQUIRED_FIELDS, *_OPTIONAL_FIELDS))
_SOURCE_FIELDS: Final = frozenset(("url", "publisher", "content_hash"))
_REQUIRED_SOURCE_FIELDS: Final = frozenset(("url", "content_hash"))
_TRUST_FIELDS: Final = frozenset(("tier", "state"))
_RELATIONSHIP_FIELDS: Final = frozenset(("type", "target"))


@dataclass(frozen=True, slots=True)
class PageRelationship:
    """One typed relationship from a page to another corpus identity."""

    type: RelationshipType
    target: str

    def __post_init__(self) -> None:
        if not isinstance(self.type, RelationshipType):
            raise MetadataError("relationship type must be a RelationshipType")
        if not isinstance(self.target, str) or not self.target.strip():
            raise MetadataError("relationship target must be a non-empty string")
        object.__setattr__(self, "target", self.target.strip())


@dataclass(frozen=True, slots=True)
class PageMetadata:
    """The validated typed form of the pinned front-matter schema.

    ``tags``, ``links``, and ``relationships`` are required in front matter
    but may be empty; ``effective_start``, ``effective_end``, and
    ``source_publisher`` are optional.  ``links`` entries are page ids or
    repository-relative corpus paths and are not resolved here: link-graph
    resolution is the separate link layer's job.  Duplicate relationship
    entries are preserved so the link layer can flag them.
    """

    page_id: str
    title: str
    version: int
    created: date
    retrieved: date
    source_url: str
    source_content_hash: str
    tags: tuple[str, ...] = ()
    links: tuple[str, ...] = ()
    relationships: tuple[PageRelationship, ...] = ()
    trust_tier: TrustTier = TrustTier.UNTRUSTED
    trust_state: TrustState = TrustState.DRAFT
    source_publisher: str | None = None
    effective_start: date | None = None
    effective_end: date | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.page_id, str)
            or _SLUG_RE.fullmatch(self.page_id) is None
        ):
            raise MetadataError("page_id must be a kebab-case slug")
        if not isinstance(self.title, str) or not self.title.strip():
            raise MetadataError("title must be a non-empty string")
        object.__setattr__(self, "title", self.title.strip())
        if isinstance(self.version, bool) or not isinstance(self.version, int):
            raise MetadataError("version must be an integer")
        if self.version < 1:
            raise MetadataError("version must be a positive integer")
        if not isinstance(self.created, date):
            raise MetadataError("created must be a datetime.date")
        if not isinstance(self.retrieved, date):
            raise MetadataError("retrieved must be a datetime.date")
        if not self._is_http_url(self.source_url):
            raise MetadataError("source_url must be an http or https URL")
        if (
            not isinstance(self.source_content_hash, str)
            or _HEX64_RE.fullmatch(self.source_content_hash) is None
        ):
            raise MetadataError(
                "source_content_hash must be a lowercase 64-character hex digest"
            )
        if self.source_publisher is not None and (
            not isinstance(self.source_publisher, str)
            or not self.source_publisher.strip()
        ):
            raise MetadataError("source_publisher must be a non-empty string or None")
        if self.effective_start is not None and not isinstance(
            self.effective_start, date
        ):
            raise MetadataError("effective_start must be a datetime.date or None")
        if self.effective_end is not None and not isinstance(self.effective_end, date):
            raise MetadataError("effective_end must be a datetime.date or None")
        if (
            self.effective_start is not None
            and self.effective_end is not None
            and self.effective_start > self.effective_end
        ):
            raise MetadataError("effective_start must not be after effective_end")
        object.__setattr__(self, "tags", _clean_strings(self.tags, "tags"))
        object.__setattr__(self, "links", _clean_strings(self.links, "links"))
        if isinstance(self.relationships, (PageRelationship, str)) or not isinstance(
            self.relationships, (tuple, list)
        ):
            raise MetadataError("relationships must be a tuple of PageRelationship")
        for relationship in self.relationships:
            if not isinstance(relationship, PageRelationship):
                raise MetadataError(
                    "relationships must contain PageRelationship values"
                )
        object.__setattr__(self, "relationships", tuple(self.relationships))
        if not isinstance(self.trust_tier, TrustTier):
            raise MetadataError("trust_tier must be a TrustTier")
        if not isinstance(self.trust_state, TrustState):
            raise MetadataError("trust_state must be a TrustState")

    @staticmethod
    def _is_http_url(value: object) -> bool:
        if not isinstance(value, str) or not value.strip():
            return False
        parsed = urlparse(value.strip())
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _clean_strings(values: Sequence[object], label: str) -> tuple[str, ...]:
    if isinstance(values, str) or not isinstance(values, (tuple, list)):
        raise MetadataError(f"{label} must be a tuple of strings")
    cleaned: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value.strip():
            raise MetadataError(f"{label} must contain non-empty strings")
        item = value.strip()
        if item not in cleaned:
            cleaned.append(item)
    return tuple(cleaned)


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _scalar(raw: str, label: str) -> str:
    value = raw.strip()
    if value.startswith('"'):
        if len(value) < 2 or not value.endswith('"'):
            raise MetadataError(
                f"front matter {label} has an unterminated quoted scalar"
            )
        return _UNESCAPE_RE.sub(r"\1", value[1:-1])
    if value.startswith("'"):
        if len(value) < 2 or not value.endswith("'"):
            raise MetadataError(
                f"front matter {label} has an unterminated quoted scalar"
            )
        return value[1:-1]
    return value


def _split_key(line: str) -> tuple[str, bool, str]:
    match = _KEY_RE.match(line.strip())
    if match is None:
        return line.strip(), False, ""
    return match.group(1), True, match.group(2)


def _parse_sub_mapping(block: Sequence[str], parent: str) -> dict[str, str]:
    result: dict[str, str] = {}
    seen: set[str] = set()
    for line in block:
        if not line.strip():
            continue
        if line.strip().startswith("-"):
            raise MetadataError(
                f"front matter {parent!r} must be a mapping, not a list"
            )
        key, has_value, inline = _split_key(line)
        if not has_value or not inline.strip():
            raise MetadataError(
                f"front matter {parent!r} mapping value for {key!r} is malformed"
            )
        if key in seen:
            raise MetadataError(f"duplicate front matter key: {parent}.{key}")
        seen.add(key)
        result[key] = _scalar(inline, f"{parent}.{key}")
    return result


def _parse_list_mapping(
    block: Sequence[str], start: int, base: int, parent: str
) -> tuple[dict[str, str], int]:
    content = block[start].strip()[1:].strip()
    first_key, has_value, inline = _split_key(content)
    if not has_value or not inline.strip():
        raise MetadataError(
            f"front matter {parent!r} list mapping item {first_key!r} is malformed"
        )
    item: dict[str, str] = {first_key: _scalar(inline, f"{parent}.{first_key}")}
    seen: set[str] = {first_key}
    position = start + 1
    while position < len(block) and _indent_of(block[position]) > base:
        continuation = block[position]
        key, has_continuation, value = _split_key(continuation)
        if not has_continuation or not value.strip():
            raise MetadataError(
                f"front matter {parent!r} list mapping item {key!r} is malformed"
            )
        if key in seen:
            raise MetadataError(
                f"duplicate front matter key: {parent} list item {key!r}"
            )
        seen.add(key)
        item[key] = _scalar(value, f"{parent}.{key}")
        position += 1
    return item, position


def _parse_list(block: Sequence[str], parent: str) -> list[object]:
    items: list[object] = []
    base = _indent_of(block[0])
    position = 0
    while position < len(block):
        line = block[position]
        if not line.strip():
            position += 1
            continue
        if _indent_of(line) != base or not line.strip().startswith("-"):
            raise MetadataError(f"front matter {parent!r} list is malformed")
        content = line.strip()[1:].strip()
        if not content:
            raise MetadataError(f"front matter {parent!r} list item is empty")
        if _MAPPING_ITEM_RE.match(content) is not None:
            item, position = _parse_list_mapping(block, position, base, parent)
            items.append(item)
        else:
            items.append(_scalar(content, parent))
            position += 1
    return items


def _parse_mapping(lines: Sequence[str]) -> dict[str, object]:
    result: dict[str, object] = {}
    seen: set[str] = set()
    position = 0
    while position < len(lines):
        line = lines[position]
        if not line.strip():
            position += 1
            continue
        if _indent_of(line) != 0:
            raise MetadataError(
                f"front matter has unexpected indentation: {line.strip()!r}"
            )
        key, has_value, inline = _split_key(line)
        if not has_value:
            raise MetadataError(
                f"front matter line is not a key-value pair: {line.strip()!r}"
            )
        if key in seen:
            raise MetadataError(f"duplicate front matter key: {key!r}")
        seen.add(key)
        if inline.strip():
            result[key] = [] if inline.strip() == "[]" else _scalar(inline, key)
            position += 1
            continue
        position += 1
        block: list[str] = []
        while position < len(lines):
            following = lines[position]
            if not following.strip():
                position += 1
                continue
            if _indent_of(following) <= 0:
                break
            block.append(following)
            position += 1
        if not block:
            result[key] = None
        elif block[0].strip().startswith("-"):
            result[key] = _parse_list(block, key)
        else:
            result[key] = _parse_sub_mapping(block, key)
    return result


def parse_front_matter(text: str) -> tuple[PageMetadata, str]:
    """Parse ``text`` into ``(metadata, body)``.

    The body is everything after the closing ``---`` delimiter line,
    byte-for-byte.  Raises :class:`MetadataError` when the front matter is
    missing, unclosed, ill-formed, or violates the pinned schema.
    """
    if not isinstance(text, str):
        raise MetadataError("page text must be a string")
    lines = text.split("\n")
    if not lines or lines[0].strip() != _DELIMITER:
        raise MetadataError("page has no front matter: expected opening --- delimiter")
    closing: int | None = None
    for position in range(1, len(lines)):
        if lines[position].strip() == _DELIMITER:
            closing = position
            break
    if closing is None:
        raise MetadataError("front matter is not closed by a second --- delimiter")
    metadata = _metadata_from_raw(_parse_mapping(lines[1:closing]))
    return metadata, "\n".join(lines[closing + 1 :])


def _require_str(raw: Mapping[str, object], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise MetadataError(f"front matter {key!r} must be a non-empty string")
    return value.strip()


def _require_date(raw: Mapping[str, object], key: str) -> date:
    value = raw.get(key)
    if not isinstance(value, str) or _DATE_RE.fullmatch(value.strip()) is None:
        raise MetadataError(f"front matter {key!r} must be an ISO date (YYYY-MM-DD)")
    try:
        return date.fromisoformat(value.strip())
    except ValueError as error:
        raise MetadataError(
            f"front matter {key!r} must be an ISO date (YYYY-MM-DD)"
        ) from error


def _optional_date(raw: Mapping[str, object], key: str) -> date | None:
    if raw.get(key) is None:
        return None
    return _require_date(raw, key)


def _require_version(raw: Mapping[str, object]) -> int:
    value = raw.get("version")
    if isinstance(value, bool):
        raise MetadataError("front matter 'version' must be a positive integer")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
    else:
        raise MetadataError("front matter 'version' must be a positive integer")
    if parsed < 1:
        raise MetadataError("front matter 'version' must be a positive integer")
    return parsed


def _string_list(raw: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = raw.get(key)
    if not isinstance(value, list) or isinstance(value, str):
        raise MetadataError(f"front matter {key!r} must be a list of strings")
    return _clean_strings(value, f"front matter {key!r}")


def _relationships(raw: Mapping[str, object]) -> tuple[PageRelationship, ...]:
    value = raw.get("relationships")
    if not isinstance(value, list) or isinstance(value, str):
        raise MetadataError("front matter 'relationships' must be a list")
    parsed: list[PageRelationship] = []
    for entry in value:
        if not isinstance(entry, Mapping):
            raise MetadataError(
                "front matter 'relationships' must contain type/target mappings"
            )
        unknown = sorted(set(entry) - _RELATIONSHIP_FIELDS)
        if unknown:
            raise MetadataError(
                f"unknown front matter relationship field: {unknown[0]!r}"
            )
        missing = sorted(_RELATIONSHIP_FIELDS - set(entry))
        if missing:
            raise MetadataError(f"front matter relationship is missing {missing[0]!r}")
        kind_raw = entry["type"]
        target_raw = entry["target"]
        if not isinstance(kind_raw, str) or not isinstance(target_raw, str):
            raise MetadataError(
                "front matter relationships need string type and target"
            )
        try:
            kind = RelationshipType(kind_raw.strip())
        except ValueError as error:
            raise MetadataError(f"unknown relationship type: {kind_raw!r}") from error
        parsed.append(PageRelationship(type=kind, target=target_raw))
    return tuple(parsed)


def _trust(raw: Mapping[str, object]) -> tuple[TrustTier, TrustState]:
    value = raw.get("trust")
    if not isinstance(value, Mapping):
        raise MetadataError("front matter 'trust' must be a mapping")
    unknown = sorted(set(value) - _TRUST_FIELDS)
    if unknown:
        raise MetadataError(f"unknown front matter trust field: {unknown[0]!r}")
    missing = sorted(_TRUST_FIELDS - set(value))
    if missing:
        raise MetadataError(f"front matter trust is missing {missing[0]!r}")
    tier_raw = value["tier"]
    state_raw = value["state"]
    if not isinstance(tier_raw, str) or not isinstance(state_raw, str):
        raise MetadataError("front matter trust needs string tier and state")
    try:
        tier = TrustTier(tier_raw.strip())
    except ValueError as error:
        raise MetadataError(f"unknown trust tier: {tier_raw!r}") from error
    try:
        state = TrustState(state_raw.strip())
    except ValueError as error:
        raise MetadataError(f"unknown trust state: {state_raw!r}") from error
    return tier, state


def _source(raw: Mapping[str, object]) -> tuple[str, str | None, str]:
    value = raw.get("source")
    if not isinstance(value, Mapping):
        raise MetadataError("front matter 'source' must be a mapping")
    unknown = sorted(set(value) - _SOURCE_FIELDS)
    if unknown:
        raise MetadataError(f"unknown front matter source field: {unknown[0]!r}")
    missing = sorted(_REQUIRED_SOURCE_FIELDS - set(value))
    if missing:
        raise MetadataError(f"front matter source is missing {missing[0]!r}")
    url = value["url"]
    if not isinstance(url, str) or not url.strip():
        raise MetadataError("front matter 'source.url' must be a non-empty string")
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise MetadataError("front matter 'source.url' must be an http or https URL")
    publisher = value.get("publisher")
    if publisher is not None and (
        not isinstance(publisher, str) or not publisher.strip()
    ):
        raise MetadataError(
            "front matter 'source.publisher' must be a non-empty string"
        )
    content_hash = value.get("content_hash")
    if (
        not isinstance(content_hash, str)
        or _HEX64_RE.fullmatch(content_hash.strip()) is None
    ):
        raise MetadataError(
            "front matter 'source.content_hash' must be a lowercase "
            "64-character hex digest"
        )
    return url, None if publisher is None else publisher.strip(), content_hash.strip()


def _metadata_from_raw(raw: Mapping[str, object]) -> PageMetadata:
    unknown = sorted(set(raw) - _KNOWN_FIELDS)
    if unknown:
        raise MetadataError(f"unknown front matter field: {unknown[0]!r}")
    missing = [name for name in _REQUIRED_FIELDS if name not in raw]
    if missing:
        raise MetadataError(f"missing required front matter field: {missing[0]!r}")
    source_url, source_publisher, source_content_hash = _source(raw)
    trust_tier, trust_state = _trust(raw)
    page_id = _require_str(raw, "id")
    if _SLUG_RE.fullmatch(page_id) is None:
        raise MetadataError("front matter 'id' must be a kebab-case slug")
    return PageMetadata(
        page_id=page_id,
        title=_require_str(raw, "title"),
        version=_require_version(raw),
        created=_require_date(raw, "created"),
        retrieved=_require_date(raw, "retrieved"),
        effective_start=_optional_date(raw, "effective_start"),
        effective_end=_optional_date(raw, "effective_end"),
        source_url=source_url,
        source_publisher=source_publisher,
        source_content_hash=source_content_hash,
        tags=_string_list(raw, "tags"),
        links=_string_list(raw, "links"),
        relationships=_relationships(raw),
        trust_tier=trust_tier,
        trust_state=trust_state,
    )


def _format_scalar(value: str) -> str:
    needs_quoting = (
        value == ""
        or value != value.strip()
        or ": " in value
        or value.endswith(":")
        or any(c in value for c in "'\"#")
    )
    if needs_quoting:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def render_front_matter(metadata: PageMetadata) -> str:
    """Render ``metadata`` as canonical deterministic front matter.

    The output is the full ``---``-delimited block with a trailing newline
    and no body: ``parse(render(parse(text)))[0] == parse(text)[0]`` always
    holds, and the rendered text composes with a body as
    ``render(metadata) + body``.  Field order, indentation, and scalar
    quoting are fixed so the same metadata always renders byte-identically.
    """
    if not isinstance(metadata, PageMetadata):
        raise MetadataError("render_front_matter requires a PageMetadata")
    lines = [
        _DELIMITER,
        f"id: {_format_scalar(metadata.page_id)}",
        f"title: {_format_scalar(metadata.title)}",
        f"version: {metadata.version}",
        f"created: {metadata.created.isoformat()}",
        f"retrieved: {metadata.retrieved.isoformat()}",
    ]
    if metadata.effective_start is not None:
        lines.append(f"effective_start: {metadata.effective_start.isoformat()}")
    if metadata.effective_end is not None:
        lines.append(f"effective_end: {metadata.effective_end.isoformat()}")
    lines.append("source:")
    lines.append(f"  url: {_format_scalar(metadata.source_url)}")
    if metadata.source_publisher is not None:
        lines.append(f"  publisher: {_format_scalar(metadata.source_publisher)}")
    lines.append(f"  content_hash: {metadata.source_content_hash}")
    if metadata.tags:
        lines.append("tags:")
        lines.extend(f"  - {_format_scalar(tag)}" for tag in metadata.tags)
    else:
        lines.append("tags: []")
    if metadata.links:
        lines.append("links:")
        lines.extend(f"  - {_format_scalar(link)}" for link in metadata.links)
    else:
        lines.append("links: []")
    if metadata.relationships:
        lines.append("relationships:")
        for relationship in metadata.relationships:
            lines.append(f"  - type: {relationship.type.value}")
            lines.append(f"    target: {_format_scalar(relationship.target)}")
    else:
        lines.append("relationships: []")
    lines.append("trust:")
    lines.append(f"  tier: {metadata.trust_tier.value}")
    lines.append(f"  state: {metadata.trust_state.value}")
    lines.append(_DELIMITER)
    return "\n".join([*lines, ""])


__all__ = [
    "MetadataError",
    "PageMetadata",
    "PageRelationship",
    "RelationshipType",
    "TrustState",
    "TrustTier",
    "parse_front_matter",
    "render_front_matter",
]
