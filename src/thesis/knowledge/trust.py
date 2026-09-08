"""Workspace trust overlays over the global knowledge corpus.

The Global Knowledge Library is global and readable by every workspace by
default (:func:`default_read_access`): a trust overlay interprets pages for
one workspace but never filters, hides, or restricts corpus reads.  The
canonical corpus, its Markdown content, and each page's global trust tier
live in the corpus layer and are never modified here.

This module holds the workspace-local overlay that records which global
pages this workspace has promoted to preferred references:

- :class:`WorkspaceTrustOverlay`, the page-id to promotion-decision map,
  serializable to and from a plain JSON document for persistence;
- :func:`resolve_effective_tier`, the explicit trust-precedence function
  backed by :data:`TIER_PRECEDENCE`:
  ``platform_policy > workspace_promoted > curated > untrusted``;
- :class:`EffectiveTier` and :class:`PromotionDecision`, the resolved tier
  and the recorded decision behind it; and
- :class:`TrustDecisionError`, raised for unknown page ids and for
  decisions that platform policy forbids.

A promotion elevates only ``untrusted`` or ``curated`` pages to
``workspace_promoted``, and only for this workspace: it changes
interpretation, never global content or other workspaces' trust, and it can
never grant capabilities or override platform invariants
(:data:`PROMOTION_IS_INTERPRETATION_ONLY`).  A ``platform_policy`` page
always wins: it cannot be promoted, and no workspace decision can push any
page below its global tier.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from types import MappingProxyType
from typing import Final

from thesis.knowledge.metadata import TrustTier

_OVERLAY_FORMAT_VERSION: Final[int] = 1

_SLUG_RE: Final = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

#: The default-read-access guarantee of the Global Knowledge Library: every
#: workspace reads the whole global corpus by default, and a trust overlay
#: only interprets pages; it never filters, hides, or restricts reads.
DEFAULT_READ_ACCESS: Final[str] = (
    "Every workspace has full default read access to the global knowledge "
    "corpus: a workspace trust overlay interprets pages for that workspace "
    "but never filters, hides, or restricts corpus reads."
)

#: What workspace promotion is and is not: an interpretation change for one
#: workspace, bounded below platform policy, never a global-content change,
#: never a capability grant, and never an override of platform invariants.
PROMOTION_IS_INTERPRETATION_ONLY: Final[str] = (
    "Workspace promotion changes interpretation for this workspace only: it "
    "never changes global corpus content or other workspaces' trust, never "
    "grants capabilities, and never overrides platform invariants."
)

#: Explicit precedence of the trust tiers: higher value, higher precedence.
TIER_PRECEDENCE: Final[Mapping[TrustTier, int]] = MappingProxyType(
    {
        TrustTier.UNTRUSTED: 0,
        TrustTier.CURATED: 1,
        TrustTier.WORKSPACE_PROMOTED: 2,
        TrustTier.PLATFORM_POLICY: 3,
    }
)

#: The only global tiers a workspace promotion may elevate.
PROMOTABLE_TIERS: Final[frozenset[TrustTier]] = frozenset(
    {TrustTier.UNTRUSTED, TrustTier.CURATED}
)


class TrustDecisionError(ValueError):
    """Raised when a workspace trust decision is invalid or not permitted."""


def default_read_access() -> str:
    """Return the default-read-access guarantee of the knowledge library."""
    return DEFAULT_READ_ACCESS


@dataclass(frozen=True, slots=True)
class PromotionDecision:
    """One recorded workspace promotion decision for a single page."""

    page_id: str
    note: str
    decided_at: date

    def __post_init__(self) -> None:
        if (
            not isinstance(self.page_id, str)
            or _SLUG_RE.fullmatch(self.page_id) is None
        ):
            raise TrustDecisionError("page_id must be a kebab-case slug")
        if not isinstance(self.note, str) or not self.note.strip():
            raise TrustDecisionError("note must be a non-empty string")
        object.__setattr__(self, "note", self.note.strip())
        if not isinstance(self.decided_at, date):
            raise TrustDecisionError("decided_at must be a datetime.date")


@dataclass(frozen=True, slots=True)
class EffectiveTier:
    """The trust tier a page carries inside one workspace after precedence."""

    page_id: str
    global_tier: TrustTier
    tier: TrustTier
    promotion: PromotionDecision | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.page_id, str) or not self.page_id.strip():
            raise TrustDecisionError("page_id must be a non-empty string")
        if not isinstance(self.global_tier, TrustTier):
            raise TrustDecisionError("global_tier must be a TrustTier")
        if not isinstance(self.tier, TrustTier):
            raise TrustDecisionError("tier must be a TrustTier")
        if self.promotion is not None and not isinstance(
            self.promotion, PromotionDecision
        ):
            raise TrustDecisionError("promotion must be a PromotionDecision or None")

    @property
    def elevated(self) -> bool:
        """Whether workspace promotion raised the tier above the global tier."""
        return (
            self.promotion is not None
            and self.tier is TrustTier.WORKSPACE_PROMOTED
            and self.global_tier is not TrustTier.WORKSPACE_PROMOTED
        )


def resolve_effective_tier(
    page_id: str,
    global_tier: TrustTier,
    promotion: PromotionDecision | None = None,
) -> EffectiveTier:
    """Apply the trust-precedence function for one page.

    ``platform_policy`` always wins and is never overridden.  A workspace
    promotion elevates only :data:`PROMOTABLE_TIERS` pages
    (``untrusted``/``curated``) to ``workspace_promoted``; every other page
    keeps its global tier.
    """
    if not isinstance(page_id, str) or not page_id.strip():
        raise TrustDecisionError("page_id must be a non-empty string")
    if not isinstance(global_tier, TrustTier):
        raise TrustDecisionError("global_tier must be a TrustTier")
    if promotion is not None and not isinstance(promotion, PromotionDecision):
        raise TrustDecisionError("promotion must be a PromotionDecision or None")
    if promotion is not None and global_tier in PROMOTABLE_TIERS:
        return EffectiveTier(
            page_id=page_id,
            global_tier=global_tier,
            tier=TrustTier.WORKSPACE_PROMOTED,
            promotion=promotion,
        )
    return EffectiveTier(
        page_id=page_id, global_tier=global_tier, tier=global_tier, promotion=promotion
    )


class WorkspaceTrustOverlay:
    """Workspace-local promotion decisions over the global corpus tiers.

    The overlay is constructed against the page-id to global-tier mapping of
    the corpus state it annotates.  It stores only this workspace's
    promotion decisions; it never touches corpus files, global metadata, or
    global tiers, and its read path (:meth:`tier_for`) always interprets
    every page the caller supplies.
    """

    def __init__(self, global_tiers: Mapping[str, TrustTier]) -> None:
        if not isinstance(global_tiers, Mapping):
            raise TrustDecisionError(
                "global_tiers must be a mapping of page id to TrustTier"
            )
        tiers: dict[str, TrustTier] = {}
        for page_id, tier in global_tiers.items():
            if not isinstance(page_id, str) or _SLUG_RE.fullmatch(page_id) is None:
                raise TrustDecisionError(
                    f"global tier key {page_id!r} must be a kebab-case slug"
                )
            if not isinstance(tier, TrustTier):
                raise TrustDecisionError(
                    f"global tier for {page_id!r} must be a TrustTier"
                )
            tiers[page_id] = tier
        self._tiers: Mapping[str, TrustTier] = MappingProxyType(tiers)
        self._decisions: dict[str, PromotionDecision] = {}

    @property
    def global_tiers(self) -> Mapping[str, TrustTier]:
        """The global tiers decisions are validated against."""
        return self._tiers

    @property
    def known_page_ids(self) -> tuple[str, ...]:
        """Every page id the overlay knows, sorted ascending."""
        return tuple(sorted(self._tiers))

    @property
    def decisions(self) -> tuple[PromotionDecision, ...]:
        """The recorded promotion decisions, ordered by page id."""
        return tuple(self._decisions[page_id] for page_id in sorted(self._decisions))

    def promotion_for(self, page_id: str) -> PromotionDecision | None:
        """Return the recorded promotion for ``page_id``, or ``None``."""
        if not isinstance(page_id, str):
            raise TrustDecisionError("page_id must be a string")
        return self._decisions.get(page_id)

    def promote(
        self, page_id: str, *, note: str, decided_at: date
    ) -> PromotionDecision:
        """Record this workspace's promotion of ``page_id``.

        Re-promoting a page replaces its decision.  Promoting a
        ``platform_policy`` page or an unknown page id raises
        :class:`TrustDecisionError`; a promotion never grants capabilities
        and never overrides platform invariants.
        """
        self._require_known(page_id)
        if self._tiers[page_id] is TrustTier.PLATFORM_POLICY:
            raise TrustDecisionError(
                f"page {page_id!r} is platform_policy: workspace promotion "
                "can never override platform policy"
            )
        decision = PromotionDecision(page_id=page_id, note=note, decided_at=decided_at)
        self._decisions[page_id] = decision
        return decision

    def demote(self, page_id: str) -> None:
        """Remove this workspace's promotion of ``page_id``.

        Demotion only removes a workspace elevation: the page returns to its
        global tier and can never be pushed below it.
        """
        self._require_known(page_id)
        if page_id not in self._decisions:
            raise TrustDecisionError(
                f"page {page_id!r} has no workspace promotion to demote: "
                "demotion cannot go below the page's global tier"
            )
        del self._decisions[page_id]

    def tier_for(self, page_id: str, global_tier: TrustTier) -> EffectiveTier:
        """Resolve the effective tier of ``page_id`` for this workspace.

        ``global_tier`` is the authoritative global tier of the corpus state
        being read; the caller-supplied value always wins at interpretation
        time, so a stale overlay can never raise a page that has since
        become ``platform_policy``.
        """
        self._require_known(page_id)
        return resolve_effective_tier(
            page_id, global_tier, self._decisions.get(page_id)
        )

    def to_json(self) -> bytes:
        """Return the canonical JSON document for this overlay.

        Deterministic: identical overlay state serializes to identical
        bytes.
        """
        payload = {
            "format_version": _OVERLAY_FORMAT_VERSION,
            "global_tiers": {
                page_id: self._tiers[page_id].value for page_id in sorted(self._tiers)
            },
            "decisions": [_decision_payload(decision) for decision in self.decisions],
        }
        return json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")

    @classmethod
    def from_json(cls, data: bytes) -> WorkspaceTrustOverlay:
        """Rebuild an overlay from :meth:`to_json` bytes.

        Every loaded decision is re-validated: unknown page ids, duplicate
        decisions, malformed notes or dates, and promotions of
        ``platform_policy`` pages are rejected.
        """
        if not isinstance(data, (bytes, bytearray)):
            raise TrustDecisionError("overlay document must be bytes")
        try:
            payload = json.loads(bytes(data).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise TrustDecisionError(
                "overlay document must be serialized overlay JSON"
            ) from error
        if not isinstance(payload, dict):
            raise TrustDecisionError("overlay payload must be a JSON object")
        if payload.get("format_version") != _OVERLAY_FORMAT_VERSION:
            raise TrustDecisionError(
                f"unsupported overlay format version: {payload.get('format_version')!r}"
            )
        return cls._from_payload(payload)

    @classmethod
    def _from_payload(cls, payload: dict[object, object]) -> WorkspaceTrustOverlay:
        raw_tiers = payload.get("global_tiers")
        if not isinstance(raw_tiers, dict):
            raise TrustDecisionError(
                "overlay payload 'global_tiers' must be a JSON object"
            )
        tiers: dict[str, TrustTier] = {}
        for page_id, raw_tier in raw_tiers.items():
            if not isinstance(page_id, str) or _SLUG_RE.fullmatch(page_id) is None:
                raise TrustDecisionError(
                    f"overlay page id {page_id!r} must be a kebab-case slug"
                )
            if not isinstance(raw_tier, str):
                raise TrustDecisionError(
                    f"overlay global tier for {page_id!r} must be a string"
                )
            try:
                tiers[page_id] = TrustTier(raw_tier)
            except ValueError as error:
                raise TrustDecisionError(
                    f"unknown trust tier for {page_id!r}: {raw_tier!r}"
                ) from error
        raw_decisions = payload.get("decisions")
        if not isinstance(raw_decisions, list):
            raise TrustDecisionError("overlay payload 'decisions' must be a JSON array")
        overlay = cls(tiers)
        seen: set[str] = set()
        for raw in raw_decisions:
            if not isinstance(raw, dict):
                raise TrustDecisionError("overlay decisions must be JSON objects")
            page_id = raw.get("page_id")
            note = raw.get("note")
            decided_at = raw.get("decided_at")
            if not isinstance(note, str):
                raise TrustDecisionError("overlay decision 'note' must be a string")
            if not isinstance(decided_at, str):
                raise TrustDecisionError(
                    "overlay decision 'decided_at' must be an ISO date string"
                )
            try:
                day = date.fromisoformat(decided_at)
            except ValueError as error:
                raise TrustDecisionError(
                    "overlay decision 'decided_at' must be an ISO date string"
                ) from error
            if not isinstance(page_id, str) or page_id in seen:
                raise TrustDecisionError(
                    f"duplicate or invalid overlay decision page id: {page_id!r}"
                )
            seen.add(page_id)
            overlay.promote(page_id, note=note, decided_at=day)
        return overlay

    def _require_known(self, page_id: object) -> str:
        if not isinstance(page_id, str) or page_id not in self._tiers:
            raise TrustDecisionError(f"unknown page id: {page_id!r}")
        return page_id

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, WorkspaceTrustOverlay):
            return False
        return self._tiers == other._tiers and self._decisions == other._decisions

    def __repr__(self) -> str:
        return (
            f"WorkspaceTrustOverlay(global_tiers={dict(self._tiers)!r}, "
            f"decisions={self.decisions!r})"
        )


def _decision_payload(decision: PromotionDecision) -> dict[str, object]:
    return {
        "page_id": decision.page_id,
        "note": decision.note,
        "decided_at": decision.decided_at.isoformat(),
    }


__all__ = [
    "DEFAULT_READ_ACCESS",
    "PROMOTABLE_TIERS",
    "PROMOTION_IS_INTERPRETATION_ONLY",
    "TIER_PRECEDENCE",
    "EffectiveTier",
    "PromotionDecision",
    "TrustDecisionError",
    "WorkspaceTrustOverlay",
    "default_read_access",
    "resolve_effective_tier",
]
