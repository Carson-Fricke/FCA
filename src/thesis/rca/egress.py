"""Deterministic advice egress from a protected RCA session to the main agent.

The main Thesis agent never receives the full aliased run.  Advice returns
only through the release's filtered-advice path plus the additional egress
filters of this module: no verbatim quotation from a released artifact
longer than a small structural threshold, and no web-query or URL-shaped
linkage material that could re-link a released identity.  Raw aliases and
raw real identifiers remain delegated to the release layer's
``assert_no_raw_identifiers`` check, which fails closed.

The emitted :class:`AdviceDocument` is a separate, schema-limited document:
the advice categories, provenance references to released artifacts by
digest (never content), and the release's recorded risk assessment.  It
never carries the response text verbatim beyond the filtered summaries,
the transformation manifest, or any real identifier.
"""

from __future__ import annotations

import difflib
import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from thesis.protected.release import ProtectedRcaRelease
from thesis.rca.session import (
    RcaHandoffError,
    RcaResponseField,
    RcaResponseVerdict,
    _canonical_json,
    _timestamp,
)

ADVICE_SCHEMA = "fca-thesis-rca-advice-egress-v1"

#: The largest verbatim run from a released artifact an advice item may
#: carry; anything longer is treated as a quotation and fails the egress.
MAX_VERBATIM_RUN = 24

_WEB_QUERY = re.compile(
    r"(?i)(?:https?://|www\.|site:|file://"
    r"|[?&](?:q|query|search|url)="
    r"|\b(?:google|bing|duckduckgo|search[- ]engine|web[- ]query|web[- ]search)\b)"
)


class AdviceEgressError(RcaHandoffError):
    """Advice cannot be emitted without carrying excluded material."""


def _longest_verbatim_run(text: str, artifact: str) -> int:
    matcher = difflib.SequenceMatcher(None, text, artifact, autojunk=False)
    return matcher.find_longest_match(0, len(text), 0, len(artifact)).size


@dataclass(frozen=True, slots=True)
class AdviceDocument:
    """The schema-limited advice document for the main Thesis agent."""

    release_id: str
    rca_session_id: str
    categories: Mapping[str, tuple[str, ...]]
    provenance: Mapping[str, str]
    risk: Mapping[str, object]
    recorded_at: str | None = None
    schema: str = ADVICE_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.release_id, str) or not self.release_id:
            raise AdviceEgressError("release_id must be a non-empty string")
        if not isinstance(self.rca_session_id, str) or not self.rca_session_id:
            raise AdviceEgressError("rca_session_id must be a non-empty string")
        if not isinstance(self.categories, Mapping):
            raise AdviceEgressError("categories must map response fields to advice")
        object.__setattr__(
            self,
            "categories",
            MappingProxyType(
                {field: tuple(items) for field, items in self.categories.items()}
            ),
        )
        if not isinstance(self.provenance, Mapping):
            raise AdviceEgressError("provenance must map references to digests")
        object.__setattr__(self, "provenance", MappingProxyType(dict(self.provenance)))
        if not isinstance(self.risk, Mapping):
            raise AdviceEgressError("risk must be a JSON-shaped mapping")
        object.__setattr__(self, "risk", MappingProxyType(dict(self.risk)))
        if self.recorded_at is not None:
            object.__setattr__(
                self, "recorded_at", _timestamp(self.recorded_at, "recorded_at")
            )

    @property
    def canonical_document(self) -> dict[str, object]:
        """Return the canonical JSON document for this advice document."""
        document: dict[str, object] = {
            "categories": {
                field: list(items) for field, items in sorted(self.categories.items())
            },
            "provenance": dict(self.provenance),
            "releaseId": self.release_id,
            "rcaSessionId": self.rca_session_id,
            "risk": dict(self.risk),
            "schema": self.schema,
        }
        if self.recorded_at is not None:
            document["recordedAt"] = self.recorded_at
        return document

    @property
    def content_sha256(self) -> str:
        """Return the content digest of the canonical advice document."""
        return hashlib.sha256(
            _canonical_json(self.canonical_document).encode("utf-8")
        ).hexdigest()


class AdviceEgress:
    """The deterministic egress service for one release's RCA advice."""

    @staticmethod
    def emit(
        release: ProtectedRcaRelease,
        accepted: RcaResponseVerdict,
        *,
        recorded_at: str | None = None,
    ) -> AdviceDocument:
        """Emit schema-limited advice for one accepted RCA response.

        Every category item runs through the release's filtered-advice
        path, then through the extra egress filters: no verbatim run from a
        released artifact beyond :data:`MAX_VERBATIM_RUN` and no web-query
        or URL-shaped linkage material.  A rejected response, a response
        recorded against a different release, or any item that cannot be
        filtered safely fails the egress closed.
        """
        if not isinstance(release, ProtectedRcaRelease):
            raise AdviceEgressError("release must be a ProtectedRcaRelease")
        if not isinstance(accepted, RcaResponseVerdict):
            raise AdviceEgressError("accepted must be an RcaResponseVerdict")
        if not accepted.accepted or accepted.response is None:
            raise AdviceEgressError(
                "a rejected RCA response cannot be emitted as advice"
            )
        if (
            accepted.release_id != release.release_id
            or accepted.rca_session_id != release.rca_session_id
        ):
            raise AdviceEgressError(
                "advice egress is bound to the release's RCA session"
            )
        normalized_at: str | None
        if recorded_at is None:
            normalized_at = None
        else:
            try:
                normalized_at = _timestamp(recorded_at, "recorded_at")
            except RcaHandoffError as error:
                raise AdviceEgressError(str(error)) from error
        categories: dict[str, tuple[str, ...]] = {}
        for field in RcaResponseField:
            items = accepted.response.get(field.value) or ()
            categories[field.value] = tuple(
                AdviceEgress._filtered_item(release, field.value, index, item)
                for index, item in enumerate(items)
            )
        manifest_sha256 = hashlib.sha256(
            _canonical_json(release.manifest.canonical_document).encode("utf-8")
        ).hexdigest()
        provenance: dict[str, str] = {
            "releaseContentSha256": release.content_sha256,
            "transformationManifestSha256": manifest_sha256,
        }
        for name, digest in sorted(release.manifest.artifact_sha256.items()):
            provenance[f"artifact/{name}"] = digest
        return AdviceDocument(
            release_id=release.release_id,
            rca_session_id=release.rca_session_id,
            categories=MappingProxyType(categories),
            provenance=MappingProxyType(provenance),
            risk=MappingProxyType(dict(release.risk.canonical_document)),
            recorded_at=normalized_at,
        )

    @staticmethod
    def _filtered_item(
        release: ProtectedRcaRelease, category: str, index: int, item: str
    ) -> str:
        filtered = release.filter_advice(item, from_session=release.rca_session_id)
        for name in sorted(release.artifacts):
            run = _longest_verbatim_run(filtered, release.artifacts[name])
            if run > MAX_VERBATIM_RUN:
                raise AdviceEgressError(
                    f"advice item {category}[{index}] quotes {run} characters "
                    f"verbatim from released artifact {name!r}"
                )
        if _WEB_QUERY.search(filtered) is not None:
            raise AdviceEgressError(
                f"advice item {category}[{index}] contains web-query linkage material"
            )
        return filtered


__all__ = [
    "ADVICE_SCHEMA",
    "MAX_VERBATIM_RUN",
    "AdviceDocument",
    "AdviceEgress",
    "AdviceEgressError",
]
