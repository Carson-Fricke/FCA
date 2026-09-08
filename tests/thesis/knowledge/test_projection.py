from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import pytest

from thesis.knowledge.cleo_projection import (
    EXCERPT_CHARS,
    NON_CANONICAL_GUARD,
    PROJECTION_IS_NOT_CANONICAL,
    ProjectionError,
    ProjectionManifest,
    ProjectionRecord,
    build_projection,
    rebuild,
)
from thesis.knowledge.corpus import CorpusPage, CorpusSnapshot
from thesis.knowledge.links import LinkGraph, PageNode
from thesis.knowledge.metadata import TrustTier, parse_front_matter
from thesis.knowledge.trust import (
    DEFAULT_READ_ACCESS,
    PROMOTION_IS_INTERPRETATION_ONLY,
    TIER_PRECEDENCE,
    PromotionDecision,
    TrustDecisionError,
    WorkspaceTrustOverlay,
    default_read_access,
    resolve_effective_tier,
)

_REVISION = "a" * 40
_LONG_BODY = "Beta evidence record. " + "cited observation " * 30
_DAY_ONE = date(2026, 9, 1)
_DAY_TWO = date(2026, 9, 2)


def _page_text(
    page_id: str,
    *,
    title: str | None = None,
    tier: str = "curated",
    state: str = "published",
    body: str = "Body text.\n",
) -> str:
    resolved_title = title if title is not None else page_id.replace("-", " ").title()
    lines = [
        "---",
        f"id: {page_id}",
        f"title: {resolved_title}",
        "version: 1",
        "created: 2026-01-05",
        "retrieved: 2026-01-06",
        "source:",
        f"  url: https://example.com/{page_id}",
        f"  content_hash: {hashlib.sha256(page_id.encode()).hexdigest()}",
        "tags: []",
        "links: []",
        "relationships: []",
        "trust:",
        f"  tier: {tier}",
        f"  state: {state}",
        "---",
    ]
    return "\n".join([*lines, body])


def _page(page_id: str, path: str, **kwargs: str) -> CorpusPage:
    metadata, body = parse_front_matter(_page_text(page_id, **kwargs))
    return CorpusPage(metadata=metadata, body=body, path=path)


def _pages() -> dict[str, CorpusPage]:
    return {
        "alpha-basics": _page("alpha-basics", "alpha-basics.md", tier="curated"),
        "beta-notes": _page(
            "beta-notes", "beta-notes.md", tier="untrusted", body=_LONG_BODY
        ),
        "gamma-prepromoted": _page(
            "gamma-prepromoted",
            "gamma/gamma-prepromoted.md",
            tier="workspace_promoted",
            state="proposed",
        ),
        "policy-guard": _page(
            "policy-guard", "policy/policy-guard.md", tier="platform_policy"
        ),
    }


def _snapshot(pages: dict[str, CorpusPage]) -> CorpusSnapshot:
    nodes = [
        PageNode(path=page.path, metadata=page.metadata) for page in pages.values()
    ]
    return CorpusSnapshot(
        root=Path("knowledge"),
        revision=_REVISION,
        pages=pages,
        graph=LinkGraph(nodes, strict=False),
    )


def _overlay(pages: dict[str, CorpusPage]) -> WorkspaceTrustOverlay:
    return WorkspaceTrustOverlay(
        {page_id: page.metadata.trust_tier for page_id, page in pages.items()}
    )


def _dumps(payload: object) -> bytes:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


# Trust overlay ---------------------------------------------------------------


def test_promote_demote_round_trip_serializes_to_json() -> None:
    overlay = _overlay(_pages())
    first = overlay.promote(
        "alpha-basics", note=" Preferred reference ", decided_at=_DAY_ONE
    )
    assert first.note == "Preferred reference"
    overlay.promote("beta-notes", note="Cited evidence source", decided_at=_DAY_TWO)
    updated = overlay.promote(
        "alpha-basics", note="Updated promotion note", decided_at=_DAY_TWO
    )
    assert overlay.promotion_for("alpha-basics") == updated
    assert [decision.page_id for decision in overlay.decisions] == [
        "alpha-basics",
        "beta-notes",
    ]

    restored = WorkspaceTrustOverlay.from_json(overlay.to_json())
    assert restored == overlay
    assert restored.promotion_for("alpha-basics") == updated
    assert restored.promotion_for("beta-notes") == overlay.promotion_for("beta-notes")

    restored.demote("alpha-basics")
    assert restored.promotion_for("alpha-basics") is None
    assert WorkspaceTrustOverlay.from_json(restored.to_json()) == restored
    assert WorkspaceTrustOverlay.from_json(restored.to_json()) != overlay


def test_trust_precedence_orders_platform_over_workspace_over_curated_over_untrusted() -> (
    None
):
    assert (
        TIER_PRECEDENCE[TrustTier.UNTRUSTED]
        < TIER_PRECEDENCE[TrustTier.CURATED]
        < TIER_PRECEDENCE[TrustTier.WORKSPACE_PROMOTED]
        < TIER_PRECEDENCE[TrustTier.PLATFORM_POLICY]
    )
    decision = PromotionDecision(
        page_id="alpha-basics", note="Preferred", decided_at=_DAY_ONE
    )
    assert (
        resolve_effective_tier("alpha-basics", TrustTier.CURATED, decision).tier
        is TrustTier.WORKSPACE_PROMOTED
    )
    assert (
        resolve_effective_tier("alpha-basics", TrustTier.UNTRUSTED, decision).tier
        is TrustTier.WORKSPACE_PROMOTED
    )
    assert (
        resolve_effective_tier("alpha-basics", TrustTier.CURATED, None).tier
        is TrustTier.CURATED
    )
    assert (
        resolve_effective_tier("alpha-basics", TrustTier.UNTRUSTED, None).tier
        is TrustTier.UNTRUSTED
    )


def test_platform_policy_beats_any_workspace_promotion() -> None:
    pages = _pages()
    overlay = _overlay(pages)
    with pytest.raises(TrustDecisionError, match="platform_policy"):
        overlay.promote("policy-guard", note="Attempted promotion", decided_at=_DAY_ONE)
    assert overlay.promotion_for("policy-guard") is None
    effective = overlay.tier_for("policy-guard", TrustTier.PLATFORM_POLICY)
    assert effective.tier is TrustTier.PLATFORM_POLICY
    assert effective.elevated is False
    assert effective.promotion is None

    attempted = PromotionDecision(
        page_id="policy-guard", note="Attempted", decided_at=_DAY_ONE
    )
    overridden = resolve_effective_tier(
        "policy-guard", TrustTier.PLATFORM_POLICY, attempted
    )
    assert overridden.tier is TrustTier.PLATFORM_POLICY
    assert overridden.elevated is False
    assert pages["policy-guard"].metadata.trust_tier is TrustTier.PLATFORM_POLICY


def test_promotion_elevates_only_untrusted_and_curated_pages() -> None:
    pages = _pages()
    overlay = _overlay(pages)
    decision = overlay.promote(
        "alpha-basics", note="Preferred reference", decided_at=_DAY_ONE
    )
    overlay.promote("beta-notes", note="Cited source", decided_at=_DAY_TWO)

    curated = overlay.tier_for("alpha-basics", TrustTier.CURATED)
    assert curated.tier is TrustTier.WORKSPACE_PROMOTED
    assert curated.global_tier is TrustTier.CURATED
    assert curated.elevated is True
    assert curated.promotion == decision

    untrusted = overlay.tier_for("beta-notes", TrustTier.UNTRUSTED)
    assert untrusted.tier is TrustTier.WORKSPACE_PROMOTED
    assert untrusted.global_tier is TrustTier.UNTRUSTED
    assert untrusted.elevated is True

    overlay.promote("gamma-prepromoted", note="Already elevated", decided_at=_DAY_TWO)
    already = overlay.tier_for("gamma-prepromoted", TrustTier.WORKSPACE_PROMOTED)
    assert already.tier is TrustTier.WORKSPACE_PROMOTED
    assert already.elevated is False

    assert pages["alpha-basics"].metadata.trust_tier is TrustTier.CURATED


def test_demote_cannot_go_below_global_tier() -> None:
    overlay = _overlay(_pages())
    with pytest.raises(TrustDecisionError, match="global tier"):
        overlay.demote("alpha-basics")
    overlay.promote("alpha-basics", note="Preferred reference", decided_at=_DAY_ONE)
    overlay.demote("alpha-basics")
    assert overlay.tier_for("alpha-basics", TrustTier.CURATED).tier is (
        TrustTier.CURATED
    )
    with pytest.raises(TrustDecisionError, match="global tier"):
        overlay.demote("alpha-basics")
    with pytest.raises(TrustDecisionError):
        overlay.demote("policy-guard")


def test_unknown_page_ids_are_rejected() -> None:
    overlay = _overlay(_pages())
    with pytest.raises(TrustDecisionError, match="unknown page id"):
        overlay.promote("missing-page", note="x", decided_at=_DAY_ONE)
    with pytest.raises(TrustDecisionError, match="unknown page id"):
        overlay.demote("missing-page")
    with pytest.raises(TrustDecisionError, match="unknown page id"):
        overlay.tier_for("missing-page", TrustTier.CURATED)


def test_promotion_decision_inputs_are_validated() -> None:
    overlay = _overlay(_pages())
    with pytest.raises(TrustDecisionError, match="note"):
        overlay.promote("alpha-basics", note="   ", decided_at=_DAY_ONE)
    with pytest.raises(TrustDecisionError, match="decided_at"):
        overlay.promote("alpha-basics", note="note", decided_at="2026-09-01")  # ty: ignore[invalid-argument-type]
    with pytest.raises(TrustDecisionError, match="global_tier"):
        overlay.tier_for("alpha-basics", "curated")  # ty: ignore[invalid-argument-type]


def test_resolve_effective_tier_validates_inputs() -> None:
    with pytest.raises(TrustDecisionError):
        resolve_effective_tier("", TrustTier.CURATED, None)
    with pytest.raises(TrustDecisionError):
        resolve_effective_tier("alpha-basics", "curated", None)  # ty: ignore[invalid-argument-type]
    with pytest.raises(TrustDecisionError):
        resolve_effective_tier("alpha-basics", TrustTier.CURATED, "decision")  # ty: ignore[invalid-argument-type]


def test_overlay_json_rejects_tampered_documents() -> None:
    overlay = _overlay(_pages())
    with pytest.raises(TrustDecisionError):
        WorkspaceTrustOverlay.from_json(b"{not json")
    with pytest.raises(TrustDecisionError):
        WorkspaceTrustOverlay.from_json(b"[]")
    with pytest.raises(TrustDecisionError):
        WorkspaceTrustOverlay.from_json(b"bytes-not-json-root")

    payload = json.loads(overlay.to_json())
    payload["format_version"] = 2
    with pytest.raises(TrustDecisionError, match="format version"):
        WorkspaceTrustOverlay.from_json(_dumps(payload))

    payload = json.loads(overlay.to_json())
    payload["decisions"] = [
        {"page_id": "policy-guard", "note": "injected", "decided_at": "2026-09-01"}
    ]
    with pytest.raises(TrustDecisionError, match="platform_policy"):
        WorkspaceTrustOverlay.from_json(_dumps(payload))

    payload = json.loads(overlay.to_json())
    payload["decisions"] = [
        {"page_id": "alpha-basics", "note": "one", "decided_at": "2026-09-01"},
        {"page_id": "alpha-basics", "note": "two", "decided_at": "2026-09-02"},
    ]
    with pytest.raises(TrustDecisionError, match="duplicate"):
        WorkspaceTrustOverlay.from_json(_dumps(payload))

    payload = json.loads(overlay.to_json())
    payload["decisions"] = [
        {"page_id": "missing-page", "note": "x", "decided_at": "2026-09-01"}
    ]
    with pytest.raises(TrustDecisionError, match="unknown page id"):
        WorkspaceTrustOverlay.from_json(_dumps(payload))


def test_default_read_access_guarantee_is_documented() -> None:
    assert default_read_access() == DEFAULT_READ_ACCESS
    assert "never filters" in DEFAULT_READ_ACCESS
    assert PROMOTION_IS_INTERPRETATION_ONLY


# Projection ------------------------------------------------------------------


def test_projection_is_deterministic_byte_identical() -> None:
    pages = _pages()
    overlay = _overlay(pages)
    overlay.promote("beta-notes", note="Preferred", decided_at=_DAY_ONE)
    first = build_projection(_snapshot(pages), overlay, git_revision=_REVISION)
    second = build_projection(_snapshot(pages), overlay, git_revision=_REVISION)
    assert first == second
    assert first.to_json() == second.to_json()

    bare_first = build_projection(_snapshot(pages), None, git_revision=_REVISION)
    bare_second = build_projection(_snapshot(pages), None, git_revision=_REVISION)
    assert bare_first.to_json() == bare_second.to_json()
    assert bare_first.to_json() != first.to_json()


def test_projection_json_round_trip_is_byte_identical() -> None:
    pages = _pages()
    overlay = _overlay(pages)
    overlay.promote("alpha-basics", note="Preferred", decided_at=_DAY_ONE)
    manifest = build_projection(_snapshot(pages), overlay, git_revision=_REVISION)
    payload = manifest.to_json()
    restored = ProjectionManifest.from_json(payload)
    assert restored == manifest
    assert restored.to_json() == payload


def test_projection_records_carry_citation_fields_in_id_order() -> None:
    pages = _pages()
    overlay = _overlay(pages)
    overlay.promote("alpha-basics", note="Preferred", decided_at=_DAY_ONE)
    manifest = build_projection(_snapshot(pages), overlay, git_revision=_REVISION)
    assert [record.page_id for record in manifest.records] == [
        "alpha-basics",
        "beta-notes",
        "gamma-prepromoted",
        "policy-guard",
    ]
    record = manifest.records[0]
    assert record.path == "alpha-basics.md"
    assert record.revision == _REVISION == manifest.git_revision
    assert record.version == 1
    assert record.title == "Alpha Basics"
    assert record.source_url == "https://example.com/alpha-basics"
    assert record.source_content_hash == hashlib.sha256(b"alpha-basics").hexdigest()
    assert record.global_tier is TrustTier.CURATED
    assert record.effective_tier is TrustTier.WORKSPACE_PROMOTED
    assert record.excerpt == "Body text.\n"
    assert manifest.records[1].global_tier is TrustTier.UNTRUSTED
    assert manifest.records[1].effective_tier is TrustTier.UNTRUSTED
    assert manifest.records[2].effective_tier is TrustTier.WORKSPACE_PROMOTED
    assert manifest.records[3].effective_tier is TrustTier.PLATFORM_POLICY
    assert manifest.records[3].path == "policy/policy-guard.md"


def test_projection_excerpt_is_bounded() -> None:
    pages = _pages()
    manifest = build_projection(_snapshot(pages), None, git_revision=_REVISION)
    beta = manifest.records[1]
    assert beta.excerpt == _LONG_BODY[:EXCERPT_CHARS]
    assert len(beta.excerpt) == EXCERPT_CHARS
    for record in manifest.records:
        assert len(record.excerpt) <= EXCERPT_CHARS
        assert (
            record.excerpt != pages[record.page_id].body
            or len(pages[record.page_id].body) <= EXCERPT_CHARS
        )


def test_max_pages_bounds_selection_deterministically() -> None:
    pages = _pages()
    full = build_projection(_snapshot(pages), None, git_revision=_REVISION)
    bounded = build_projection(
        _snapshot(pages), None, git_revision=_REVISION, max_pages=2
    )
    assert [record.page_id for record in bounded.records] == [
        "alpha-basics",
        "beta-notes",
    ]
    assert bounded.to_json() != full.to_json()
    assert (
        build_projection(
            _snapshot(pages), None, git_revision=_REVISION, max_pages=99
        ).to_json()
        == full.to_json()
    )
    empty = build_projection(
        _snapshot(pages), None, git_revision=_REVISION, max_pages=0
    )
    assert empty.records == ()
    with pytest.raises(ProjectionError):
        build_projection(_snapshot(pages), None, git_revision=_REVISION, max_pages=-1)
    with pytest.raises(ProjectionError):
        build_projection(_snapshot(pages), None, git_revision=_REVISION, max_pages=True)
    with pytest.raises(ProjectionError):
        build_projection(
            _snapshot(pages),
            None,
            git_revision=_REVISION,
            max_pages="2",  # ty: ignore[invalid-argument-type]
        )


def test_rebuild_regenerates_identical_manifest_from_corpus_state() -> None:
    pages = _pages()
    snapshot = _snapshot(pages)
    overlay = _overlay(pages)
    overlay.promote("beta-notes", note="Preferred", decided_at=_DAY_ONE)
    first = build_projection(snapshot, overlay, git_revision=_REVISION)
    assert rebuild(snapshot, overlay, git_revision=_REVISION).to_json() == (
        first.to_json()
    )
    overlay.promote("alpha-basics", note="Also preferred", decided_at=_DAY_TWO)
    grown = rebuild(snapshot, overlay, git_revision=_REVISION)
    assert [record.effective_tier for record in grown.records] == [
        TrustTier.WORKSPACE_PROMOTED,
        TrustTier.WORKSPACE_PROMOTED,
        TrustTier.WORKSPACE_PROMOTED,
        TrustTier.PLATFORM_POLICY,
    ]
    assert grown.to_json() != first.to_json()


def test_manifest_guard_forbids_cleo_nexus_and_brain() -> None:
    pages = _pages()
    manifest = build_projection(_snapshot(pages), None, git_revision=_REVISION)
    payload = json.loads(manifest.to_json())
    assert payload["cleo_projection"] == {
        "nexus": "not-used",
        "brain": "not-used",
        "canonical": False,
    }
    assert payload["cleo_projection"] == dict(manifest.guard)
    assert payload["note"] == PROJECTION_IS_NOT_CANONICAL == manifest.note
    assert "never a second canonical copy" in manifest.note

    tampered = dict(payload)
    tampered["cleo_projection"] = {
        "nexus": "not-used",
        "brain": "not-used",
        "canonical": True,
    }
    with pytest.raises(ProjectionError, match="canonical"):
        ProjectionManifest.from_json(_dumps(tampered))

    tampered = dict(payload)
    tampered["note"] = "canonical knowledge"
    with pytest.raises(ProjectionError, match="note"):
        ProjectionManifest.from_json(_dumps(tampered))


def test_manifest_from_json_rejects_tampered_records() -> None:
    pages = _pages()
    manifest = build_projection(_snapshot(pages), None, git_revision=_REVISION)

    payload = json.loads(manifest.to_json())
    payload["format_version"] = 2
    with pytest.raises(ProjectionError, match="format version"):
        ProjectionManifest.from_json(_dumps(payload))

    payload = json.loads(manifest.to_json())
    payload["records"][0]["excerpt"] = "x" * (EXCERPT_CHARS + 1)
    with pytest.raises(ProjectionError, match="excerpt"):
        ProjectionManifest.from_json(_dumps(payload))

    payload = json.loads(manifest.to_json())
    payload["records"].reverse()
    with pytest.raises(ProjectionError, match="ordered"):
        ProjectionManifest.from_json(_dumps(payload))

    payload = json.loads(manifest.to_json())
    payload["records"][0]["global_tier"] = "mystery"
    with pytest.raises(ProjectionError, match="tier"):
        ProjectionManifest.from_json(_dumps(payload))

    payload = json.loads(manifest.to_json())
    payload["git_revision"] = "b" * 40
    with pytest.raises(ProjectionError, match="revision"):
        ProjectionManifest.from_json(_dumps(payload))

    with pytest.raises(ProjectionError):
        ProjectionManifest.from_json(b"{not json")


def test_projection_record_rejects_tier_demotion_and_long_excerpt() -> None:
    with pytest.raises(ProjectionError, match="below"):
        ProjectionRecord(
            page_id="alpha-basics",
            path="alpha-basics.md",
            revision=_REVISION,
            version=1,
            title="Alpha Basics",
            source_url="https://example.com/alpha-basics",
            source_content_hash=hashlib.sha256(b"alpha-basics").hexdigest(),
            global_tier=TrustTier.CURATED,
            effective_tier=TrustTier.UNTRUSTED,
            excerpt="Alpha body text.\n",
        )
    with pytest.raises(ProjectionError, match="excerpt"):
        ProjectionRecord(
            page_id="alpha-basics",
            path="alpha-basics.md",
            revision=_REVISION,
            version=1,
            title="Alpha Basics",
            source_url="https://example.com/alpha-basics",
            source_content_hash=hashlib.sha256(b"alpha-basics").hexdigest(),
            global_tier=TrustTier.CURATED,
            effective_tier=TrustTier.WORKSPACE_PROMOTED,
            excerpt="x" * (EXCERPT_CHARS + 1),
        )


def test_projection_validates_build_inputs() -> None:
    pages = _pages()
    with pytest.raises(ProjectionError, match="git_revision"):
        build_projection(_snapshot(pages), None, git_revision="")
    with pytest.raises(ProjectionError, match="overlay"):
        build_projection(
            _snapshot(pages),
            "not-an-overlay",  # ty: ignore[invalid-argument-type]
            git_revision=_REVISION,
        )
    assert NON_CANONICAL_GUARD["nexus"] == "not-used"
    assert NON_CANONICAL_GUARD["brain"] == "not-used"
    assert NON_CANONICAL_GUARD["canonical"] is False


def test_default_read_access_keeps_every_page_visible_with_empty_overlay() -> None:
    pages = _pages()
    overlay = _overlay(pages)
    assert overlay.decisions == ()
    for page_id, page in pages.items():
        effective = overlay.tier_for(page_id, page.metadata.trust_tier)
        assert effective.tier is page.metadata.trust_tier
        assert effective.elevated is False
        assert effective.promotion is None
    manifest = build_projection(_snapshot(pages), overlay, git_revision=_REVISION)
    assert len(manifest.records) == len(pages)
    assert [record.page_id for record in manifest.records] == sorted(pages)
    assert all(
        record.effective_tier is record.global_tier for record in manifest.records
    )
    assert default_read_access() == DEFAULT_READ_ACCESS
