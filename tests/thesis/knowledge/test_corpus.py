from __future__ import annotations

import subprocess
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from thesis.knowledge import corpus as corpus_module
from thesis.knowledge.corpus import (
    CorpusError,
    CorpusPage,
    CorpusSnapshot,
    KnowledgeCorpus,
)
from thesis.knowledge.links import (
    LinkError,
    LinkGraph,
    PageNode,
    relationship_type,
)
from thesis.knowledge.metadata import (
    MetadataError,
    PageMetadata,
    RelationshipType,
    TrustState,
    TrustTier,
    parse_front_matter,
    render_front_matter,
)

_HASH = "0123456789abcdef" * 4
_REVISION = "f" * 40


def _page_text(
    page_id: str = "alpha-momentum",
    *,
    title: str = "Alpha Momentum",
    version: int | str = 1,
    created: str = "2026-01-05",
    retrieved: str = "2026-01-06",
    effective_start: str | None = None,
    effective_end: str | None = None,
    url: str = "https://example.com/alpha",
    publisher: str | None = None,
    content_hash: str = _HASH,
    tags: Sequence[str] = ("momentum",),
    links: Sequence[str] = (),
    relationships: Sequence[tuple[str, str]] = (),
    tier: str = "curated",
    state: str = "published",
    drop_field: str | None = None,
    source_extra: Sequence[str] = (),
    extra_lines: Sequence[str] = (),
    body: str = "Alpha body text.",
) -> str:
    lines = [
        "---",
        f"id: {page_id}",
        f"title: {title}",
        f"version: {version}",
        f"created: {created}",
        f"retrieved: {retrieved}",
    ]
    if effective_start is not None:
        lines.append(f"effective_start: {effective_start}")
    if effective_end is not None:
        lines.append(f"effective_end: {effective_end}")
    lines.append("source:")
    lines.append(f"  url: {url}")
    if publisher is not None:
        lines.append(f"  publisher: {publisher}")
    lines.append(f"  content_hash: {content_hash}")
    lines.extend(source_extra)
    for name, values in (
        ("tags", tags),
        ("links", links),
        ("relationships", relationships),
    ):
        if not values:
            lines.append(f"{name}: []")
            continue
        lines.append(f"{name}:")
        for value in values:
            if isinstance(value, tuple):
                lines.append(f"  - type: {value[0]}")
                lines.append(f"    target: {value[1]}")
            else:
                lines.append(f"  - {value}")
    lines.append("trust:")
    lines.append(f"  tier: {tier}")
    lines.append(f"  state: {state}")
    if drop_field is not None:
        kept: list[str] = ["---"]
        skipping = False
        for line in lines[1:]:
            if line.startswith(f"{drop_field}:"):
                skipping = True
                continue
            if skipping and line.startswith(" "):
                continue
            skipping = False
            kept.append(line)
        lines = kept
    lines.extend(extra_lines)
    lines.append("---")
    return "\n".join([*lines, body])


def _metadata(**kwargs: Any) -> PageMetadata:
    metadata, _ = parse_front_matter(_page_text(**kwargs))
    return metadata


def _node(page_id: str, path: str, **kwargs: Any) -> PageNode:
    return PageNode(path=path, metadata=_metadata(page_id=page_id, **kwargs))


class _FakeGit:
    def __init__(self) -> None:
        self.revision = _REVISION
        self.exit_code = 0
        self.stderr = ""
        self.calls: list[tuple[list[str], str | None]] = []

    def __call__(
        self, command: list[str], cwd: object | None = None, **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(command), str(cwd) if cwd is not None else None))
        if self.exit_code != 0:
            return subprocess.CompletedProcess(
                command, self.exit_code, stdout="", stderr=self.stderr
            )
        return subprocess.CompletedProcess(
            command, 0, stdout=self.revision + "\n", stderr=""
        )


@pytest.fixture
def fake_git(monkeypatch: pytest.MonkeyPatch) -> _FakeGit:
    fake = _FakeGit()
    monkeypatch.setattr(corpus_module.subprocess, "run", fake)
    return fake


@pytest.fixture
def corpus_root(tmp_path: Path) -> Path:
    root = tmp_path / "knowledge"
    root.mkdir()

    def write(relative: str, text: str) -> None:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    write(
        "alpha-momentum.md",
        _page_text(
            "alpha-momentum",
            title="Alpha Momentum",
            tags=("momentum",),
            links=("beta-mean-reversion",),
            tier="curated",
            state="published",
            body="Alpha body text.\n",
        ),
    )
    write(
        "beta-mean-reversion.md",
        _page_text(
            "beta-mean-reversion",
            title="Beta Mean Reversion",
            relationships=(("supersedes", "alpha-momentum"),),
            tier="untrusted",
            state="published",
            body="Beta body text.\n",
        ),
    )
    write(
        "gamma-contrarian.md",
        _page_text(
            "gamma-contrarian",
            title="Gamma Contrarian",
            links=("sub/delta-notes.md",),
            relationships=(("contradicts", "beta-mean-reversion"),),
            tier="curated",
            state="proposed",
            body="Gamma body text.\n",
        ),
    )
    write(
        "sub/delta-notes.md",
        _page_text(
            "delta-notes",
            title="Delta Notes",
            relationships=(
                ("supersedes", "beta-mean-reversion"),
                ("relates", "gamma-contrarian"),
            ),
            tier="curated",
            state="published",
            body="Delta body text.\n",
        ),
    )
    write("notes.md", "Plain markdown without any front matter.\n")
    return root


# Metadata parsing and rendering ----------------------------------------------


def test_valid_page_round_trips_through_render_and_parse() -> None:
    text = _page_text(
        title="Alpha Momentum Review",
        version=2,
        effective_start="2026-01-01",
        effective_end="2026-06-30",
        publisher="Example Press",
        tags=("momentum", "equities"),
        links=("beta-mean-reversion",),
        relationships=(("supersedes", "old-alpha"), ("relates", "beta-mean-reversion")),
        tier="curated",
        state="published",
        body="First paragraph.\n\nSecond paragraph.\n",
    )
    parsed, body = parse_front_matter(text)
    assert parsed.page_id == "alpha-momentum"
    assert parsed.title == "Alpha Momentum Review"
    assert parsed.version == 2
    assert parsed.created == date(2026, 1, 5)
    assert parsed.retrieved == date(2026, 1, 6)
    assert parsed.effective_start == date(2026, 1, 1)
    assert parsed.effective_end == date(2026, 6, 30)
    assert parsed.source_publisher == "Example Press"
    assert parsed.tags == ("momentum", "equities")
    assert parsed.links == ("beta-mean-reversion",)
    assert [r.type for r in parsed.relationships] == [
        RelationshipType.SUPERSEDES,
        RelationshipType.RELATES,
    ]
    assert [r.target for r in parsed.relationships] == [
        "old-alpha",
        "beta-mean-reversion",
    ]
    assert parsed.trust_tier is TrustTier.CURATED
    assert parsed.trust_state is TrustState.PUBLISHED
    assert body == "First paragraph.\n\nSecond paragraph.\n"
    rendered = render_front_matter(parsed)
    reparsed, rendered_body = parse_front_matter(rendered)
    assert reparsed == parsed
    assert rendered_body == ""
    assert render_front_matter(reparsed) == rendered


def test_render_is_canonical_and_deterministic() -> None:
    text = _page_text(tags=("momentum",), links=("beta-mean-reversion",))
    parsed, _ = parse_front_matter(text)
    first = render_front_matter(parsed)
    assert first == render_front_matter(parsed)
    assert first == "\n".join(
        [
            "---",
            "id: alpha-momentum",
            "title: Alpha Momentum",
            "version: 1",
            "created: 2026-01-05",
            "retrieved: 2026-01-06",
            "source:",
            "  url: https://example.com/alpha",
            f"  content_hash: {_HASH}",
            "tags:",
            "  - momentum",
            "links:",
            "  - beta-mean-reversion",
            "relationships: []",
            "trust:",
            "  tier: curated",
            "  state: published",
            "---",
            "",
        ]
    )


def test_body_is_preserved_verbatim_after_front_matter() -> None:
    body = "Intro paragraph.\n\n---\n\nAfter a horizontal rule.\n"
    parsed_text = _page_text(body=body)
    _, parsed_body = parse_front_matter(parsed_text)
    assert parsed_body == body


def test_render_quotes_scalars_that_require_quoting() -> None:
    parsed = _metadata(title="Note: revised")
    rendered = render_front_matter(parsed)
    assert 'title: "Note: revised"' in rendered
    assert "url: https://example.com/alpha" in rendered
    reparsed, _ = parse_front_matter(rendered)
    assert reparsed == parsed
    text = _page_text(tags=()).replace("tags: []", 'tags:\n  - "size: large"')
    tagged, _ = parse_front_matter(text)
    assert tagged.tags == ("size: large",)
    tagged_rendered = render_front_matter(tagged)
    assert '  - "size: large"' in tagged_rendered
    tagged_reparsed, _ = parse_front_matter(tagged_rendered)
    assert tagged_reparsed == tagged


def test_missing_front_matter_raises() -> None:
    with pytest.raises(MetadataError, match="no front matter"):
        parse_front_matter("Just some prose, no delimiters.\n")


def test_unclosed_front_matter_raises() -> None:
    lines = _page_text().split("\n")
    closing = len(lines) - 1 - lines[::-1].index("---")
    del lines[closing]
    with pytest.raises(MetadataError, match="not closed"):
        parse_front_matter("\n".join(lines))


def test_unknown_field_raises() -> None:
    text = _page_text(extra_lines=("bogus: yes",))
    with pytest.raises(MetadataError, match="unknown front matter field: 'bogus'"):
        parse_front_matter(text)


@pytest.mark.parametrize(
    "field", ["title", "created", "retrieved", "source", "tags", "links", "trust"]
)
def test_missing_required_field_raises(field: str) -> None:
    text = _page_text(drop_field=field)
    with pytest.raises(
        MetadataError, match="missing required front matter field"
    ) as excinfo:
        parse_front_matter(text)
    assert field in str(excinfo.value)


def test_relationship_field_is_required() -> None:
    text = _page_text(drop_field="relationships")
    with pytest.raises(
        MetadataError, match="missing required front matter field: 'relationships'"
    ):
        parse_front_matter(text)


def test_bad_version_raises() -> None:
    with pytest.raises(MetadataError, match="'version' must be a positive integer"):
        parse_front_matter(_page_text(version="abc"))
    with pytest.raises(MetadataError, match="'version' must be a positive integer"):
        parse_front_matter(_page_text(version=0))


def test_non_list_tags_raises() -> None:
    text = _page_text(drop_field="tags", extra_lines=("tags: momentum",))
    with pytest.raises(MetadataError, match="'tags' must be a list of strings"):
        parse_front_matter(text)


def test_non_mapping_source_raises() -> None:
    text = _page_text(drop_field="source", extra_lines=("source: https://x.example",))
    with pytest.raises(MetadataError, match="'source' must be a mapping"):
        parse_front_matter(text)


def test_non_mapping_trust_raises() -> None:
    text = _page_text(drop_field="trust", extra_lines=("trust: published",))
    with pytest.raises(MetadataError, match="'trust' must be a mapping"):
        parse_front_matter(text)


def test_relationship_item_missing_target_raises() -> None:
    text = _page_text(
        drop_field="relationships",
        extra_lines=("relationships:", "  - type: supersedes"),
    )
    with pytest.raises(MetadataError, match="relationship is missing 'target'"):
        parse_front_matter(text)


def test_relationship_item_unknown_field_raises() -> None:
    text = _page_text(
        drop_field="relationships",
        extra_lines=(
            "relationships:",
            "  - type: supersedes",
            "    target: old-alpha",
            "    note: why",
        ),
    )
    with pytest.raises(MetadataError, match="unknown front matter relationship"):
        parse_front_matter(text)


@pytest.mark.parametrize(
    ("tier", "state", "message"),
    [
        ("canonical", "published", "unknown trust tier: 'canonical'"),
        ("curated", "archived", "unknown trust state: 'archived'"),
    ],
)
def test_unknown_trust_vocabulary_raises(tier: str, state: str, message: str) -> None:
    with pytest.raises(MetadataError, match=message):
        parse_front_matter(_page_text(tier=tier, state=state))


def test_unknown_relationship_type_raises() -> None:
    text = _page_text(relationships=(("cites", "old-alpha"),))
    with pytest.raises(MetadataError, match="unknown relationship type: 'cites'"):
        parse_front_matter(text)


def test_duplicate_top_level_key_raises() -> None:
    text = _page_text(extra_lines=("id: other-page",))
    with pytest.raises(MetadataError, match="duplicate front matter key: 'id'"):
        parse_front_matter(text)


def test_duplicate_nested_source_key_raises() -> None:
    text = _page_text(source_extra=("  url: https://example.com/other",))
    with pytest.raises(MetadataError, match="duplicate front matter key: source.url"):
        parse_front_matter(text)


@pytest.mark.parametrize("value", ["January 5, 2026", "2026-13-40", "20260105"])
def test_bad_iso_date_raises(value: str) -> None:
    with pytest.raises(MetadataError, match="'created' must be an ISO date"):
        parse_front_matter(_page_text(created=value))


@pytest.mark.parametrize("value", ["deadbeef", _HASH.upper()])
def test_bad_content_hash_raises(value: str) -> None:
    with pytest.raises(MetadataError, match="64-character hex digest"):
        parse_front_matter(_page_text(content_hash=value))


@pytest.mark.parametrize("value", ["ftp://example.com/x", "example.com/no-scheme"])
def test_non_http_source_url_raises(value: str) -> None:
    with pytest.raises(MetadataError, match="http or https URL"):
        parse_front_matter(_page_text(url=value))


def test_bad_page_id_slug_raises() -> None:
    with pytest.raises(MetadataError, match="'id' must be a kebab-case slug"):
        parse_front_matter(_page_text(page_id="Alpha Momentum"))


def test_empty_list_item_raises() -> None:
    with pytest.raises(MetadataError, match="'tags' list item is empty"):
        parse_front_matter(_page_text(tags=(" ",)))


def test_bad_effective_window_raises() -> None:
    with pytest.raises(MetadataError, match="effective_start must not be after"):
        parse_front_matter(
            _page_text(effective_start="2026-06-30", effective_end="2026-01-01")
        )


# Link graph ------------------------------------------------------------------


def test_dangling_link_raises_link_error() -> None:
    graph_nodes = [_node("alpha-momentum", "alpha.md", links=("ghost-page",))]
    with pytest.raises(LinkError, match="dangling-link"):
        LinkGraph(graph_nodes)


def test_dangling_relationship_target_raises_link_error() -> None:
    graph_nodes = [
        _node("alpha-momentum", "alpha.md", relationships=(("supersedes", "ghost"),))
    ]
    with pytest.raises(LinkError, match="dangling-relationship"):
        LinkGraph(graph_nodes)


def test_duplicate_relationship_entry_raises_link_error() -> None:
    graph_nodes = [
        _node(
            "alpha-momentum",
            "alpha.md",
            relationships=(("relates", "beta-notes"), ("relates", "beta-notes")),
        ),
        _node("beta-notes", "beta.md"),
    ]
    with pytest.raises(LinkError, match="duplicate-relationship"):
        LinkGraph(graph_nodes)


def test_unknown_relationship_type_string_raises() -> None:
    with pytest.raises(LinkError, match="unknown relationship type: 'cites'"):
        relationship_type("cites")
    assert relationship_type("supersedes") is RelationshipType.SUPERSEDES


def test_backlinks_superseded_by_and_contradictions_resolve() -> None:
    graph = LinkGraph(
        [
            _node("alpha-momentum", "alpha.md", links=("beta-mean-reversion",)),
            _node(
                "beta-mean-reversion",
                "beta.md",
                relationships=(
                    ("supersedes", "alpha-momentum"),
                    ("contradicts", "gamma-notes"),
                ),
            ),
            _node("gamma-notes", "gamma.md"),
        ]
    )
    assert graph.backlinks("beta-mean-reversion") == ("alpha-momentum",)
    assert graph.backlinks("alpha-momentum") == ()
    assert graph.superseded_by("alpha-momentum") == ("beta-mean-reversion",)
    assert graph.superseded_by("beta-mean-reversion") == ()
    assert graph.contradictions("gamma-notes") == ("beta-mean-reversion",)
    assert graph.contradictions("alpha-momentum") == ()
    assert graph.page_ids == ("alpha-momentum", "beta-mean-reversion", "gamma-notes")


def test_links_resolve_by_path_and_id() -> None:
    graph = LinkGraph(
        [
            _node("delta-notes", "sub/delta.md", links=()),
            _node("gamma-notes", "gamma.md", links=("sub/delta.md",)),
        ]
    )
    assert graph.resolve("delta-notes") == "delta-notes"
    assert graph.resolve("sub/delta.md") == "delta-notes"
    assert graph.resolve("./sub/delta.md") == "delta-notes"
    assert graph.resolve("missing-page") is None
    assert graph.backlinks("delta-notes") == ("gamma-notes",)


def test_lenient_graph_collects_problems_and_keeps_resolved_edges() -> None:
    graph = LinkGraph(
        [
            _node(
                "alpha-momentum",
                "alpha.md",
                links=("ghost-page",),
                relationships=(("relates", "beta-notes"), ("relates", "beta-notes")),
            ),
            _node("beta-notes", "beta.md"),
        ],
        strict=False,
    )
    assert any("dangling-link" in problem for problem in graph.problems)
    assert any("duplicate-relationship" in problem for problem in graph.problems)
    assert graph.superseded_by("beta-notes") == ()
    resolved = LinkGraph(
        [
            _node("alpha-momentum", "alpha.md", links=("beta-notes",)),
            _node("beta-notes", "beta.md"),
        ]
    )
    assert resolved.backlinks("beta-notes") == ("alpha-momentum",)


def test_accessor_unknown_page_raises() -> None:
    graph = LinkGraph([_node("alpha-momentum", "alpha.md")])
    with pytest.raises(LinkError, match="unknown page id"):
        graph.backlinks("ghost-page")
    with pytest.raises(LinkError, match="unknown page id"):
        graph.superseded_by("ghost-page")
    with pytest.raises(LinkError, match="unknown page id"):
        graph.contradictions("ghost-page")


# Corpus loading and validation -----------------------------------------------


def test_load_builds_snapshot_with_pages_graph_and_revision(
    corpus_root: Path, fake_git: _FakeGit
) -> None:
    snapshot = KnowledgeCorpus(corpus_root).load()
    assert snapshot.revision == _REVISION
    assert sorted(snapshot.pages) == [
        "alpha-momentum",
        "beta-mean-reversion",
        "delta-notes",
        "gamma-contrarian",
    ]
    assert snapshot.is_unique_ids is True
    assert fake_git.calls == [(["git", "rev-parse", "HEAD"], str(corpus_root))]
    alpha = snapshot.page("alpha-momentum")
    assert isinstance(alpha, CorpusPage)
    assert alpha.path == "alpha-momentum.md"
    assert alpha.body == "Alpha body text.\n"
    assert alpha.metadata.title == "Alpha Momentum"
    assert snapshot.page("sub/delta-notes.md").page_id == "delta-notes"
    assert snapshot.graph.superseded_by("alpha-momentum") == ("beta-mean-reversion",)
    assert snapshot.graph.superseded_by("beta-mean-reversion") == ("delta-notes",)
    assert snapshot.graph.contradictions("beta-mean-reversion") == ("gamma-contrarian",)
    assert snapshot.graph.backlinks("beta-mean-reversion") == ("alpha-momentum",)
    assert snapshot.graph.backlinks("delta-notes") == ("gamma-contrarian",)
    assert {page.path for page in snapshot.pages.values()}.isdisjoint({"notes.md"})


def test_validate_is_clean_for_well_formed_corpus(
    corpus_root: Path, fake_git: _FakeGit
) -> None:
    snapshot = KnowledgeCorpus(corpus_root).load()
    assert isinstance(snapshot, CorpusSnapshot)
    assert snapshot.validate() == []


def test_load_reports_duplicate_ids(corpus_root: Path, fake_git: _FakeGit) -> None:
    duplicate = corpus_root / "copy-alpha-momentum.md"
    duplicate.write_text(
        _page_text("alpha-momentum", title="Alpha Again", body="Duplicated.\n"),
        encoding="utf-8",
    )
    snapshot = KnowledgeCorpus(corpus_root).load()
    assert snapshot.is_unique_ids is False
    assert snapshot.duplicate_ids == (
        ("alpha-momentum", "alpha-momentum.md", "copy-alpha-momentum.md"),
    )
    assert snapshot.page("alpha-momentum").path == "alpha-momentum.md"
    problems = snapshot.validate()
    assert problems == sorted(problems)
    assert any(
        problem.startswith("duplicate-id:") and "copy-alpha-momentum.md" in problem
        for problem in problems
    )


def test_validate_lists_dangling_links_and_trust_mismatches(
    tmp_path: Path, fake_git: _FakeGit
) -> None:
    root = tmp_path / "knowledge"
    root.mkdir()
    (root / "policy-draft.md").write_text(
        _page_text("policy-draft", tier="platform_policy", state="draft"),
        encoding="utf-8",
    )
    (root / "promoted.md").write_text(
        _page_text("promoted", tier="workspace_promoted", state="draft"),
        encoding="utf-8",
    )
    (root / "lonely.md").write_text(
        _page_text("lonely", links=("ghost-page",)), encoding="utf-8"
    )
    snapshot = KnowledgeCorpus(root).load()
    problems = snapshot.validate()
    assert problems == sorted(problems)
    assert any(
        problem.startswith("trust-mismatch:")
        and "policy-draft" in problem
        and "published" in problem
        for problem in problems
    )
    assert any(
        problem.startswith("trust-mismatch:") and "promoted" in problem
        for problem in problems
    )
    assert any(
        problem.startswith("dangling-link:") and "ghost-page" in problem
        for problem in problems
    )


def test_load_raises_when_git_fails(corpus_root: Path, fake_git: _FakeGit) -> None:
    fake_git.exit_code = 1
    fake_git.stderr = "fatal: not a git repository"
    with pytest.raises(CorpusError, match="git rev-parse HEAD failed"):
        KnowledgeCorpus(corpus_root).load()


def test_load_propagates_metadata_errors_for_broken_pages(
    corpus_root: Path, fake_git: _FakeGit
) -> None:
    (corpus_root / "broken.md").write_text(
        _page_text("broken", drop_field="title"), encoding="utf-8"
    )
    with pytest.raises(MetadataError, match="missing required front matter field"):
        KnowledgeCorpus(corpus_root).load()


def test_corpus_requires_directory_and_path_type(tmp_path: Path) -> None:
    with pytest.raises(CorpusError, match="root must be a pathlib.Path"):
        KnowledgeCorpus("knowledge")  # ty: ignore[invalid-argument-type]
    with pytest.raises(CorpusError, match="not a directory"):
        KnowledgeCorpus(tmp_path / "missing").load()


def test_page_lookup_unknown_raises(tmp_path: Path, fake_git: _FakeGit) -> None:
    (tmp_path / "alpha.md").write_text(_page_text(), encoding="utf-8")
    snapshot = KnowledgeCorpus(tmp_path).load()
    with pytest.raises(CorpusError, match="unknown page id or path"):
        snapshot.page("ghost-page")
    with pytest.raises(CorpusError, match="non-empty string"):
        snapshot.page("")
