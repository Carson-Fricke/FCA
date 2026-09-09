"""Workspace artifact persistence for screener definitions and studies.

Screener definitions and saved studies persist as workspace artifacts with
stable identity (content hash over the canonical JSON v1 document) and
provenance (universe revision, data window, definition hash). The Git
revision is supplied by the caller — this module performs no subprocess
work and no I/O of its own.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, Final

from thesis.screening.engine import (
    ScreenerDefinition,
    ScreenerError,
    ScreenerHit,
    ScreenerResult,
)

STUDY_DOMAIN: Final = b"fca:thesis-screening-study:v1\0"


class ArtifactError(ScreenerError):
    """A screener artifact failed validation on save or load."""


@dataclass(frozen=True, slots=True)
class SavedStudy:
    """One persisted screening study with full provenance."""

    name: str
    definition: ScreenerDefinition
    result: ScreenerResult
    git_revision: str
    saved_at: date

    def __post_init__(self) -> None:
        if not self.name:
            raise ScreenerError("study name must not be empty")
        if not isinstance(self.definition, ScreenerDefinition):
            raise ScreenerError("study definition must be a ScreenerDefinition")
        if not isinstance(self.result, ScreenerResult):
            raise ScreenerError("study result must be a ScreenerResult")
        if not self.git_revision:
            raise ScreenerError("git revision must not be empty")
        if self.definition.definition_hash != self.result.definition_hash:
            raise ScreenerError(
                "study definition and result carry different definition hashes"
            )

    @property
    def canonical_document(self) -> dict[str, object]:
        """Return the canonical JSON-ready study document."""
        return {
            "schema_version": 1,
            "name": self.name,
            "saved_at": self.saved_at.isoformat(),
            "git_revision": self.git_revision,
            "definition": self.definition.canonical_document,
            "definition_hash": self.definition.definition_hash,
            "result": self.result.canonical_document(),
        }

    @property
    def content_sha256(self) -> str:
        """Return the stable content hash of the canonical study document."""
        payload = json.dumps(
            self.canonical_document, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(STUDY_DOMAIN + payload).hexdigest()

    def canonical_json(self) -> str:
        """Return the deterministic JSON serialization with the content hash."""
        document = dict(self.canonical_document)
        document["content_sha256"] = self.content_sha256
        return json.dumps(document, sort_keys=True, separators=(",", ":"))

    def artifact_path(self) -> str:
        """Return the workspace-relative artifact path for this study."""
        return f"screeners/studies/{self.name}-{self.content_sha256[:12]}.json"

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> SavedStudy:
        """Rebuild a study from its canonical document."""
        if not isinstance(document, Mapping):
            raise ScreenerError("study payload must be a mapping")
        if document.get("schema_version") != 1:
            raise ScreenerError("unsupported study schema version")
        try:
            raw_definition = document["definition"]
            definition = ScreenerDefinition(
                name=str(raw_definition["name"]),
                universe_revision=str(raw_definition["universe_revision"]),
                filters=tuple(raw_definition["filters"]),
                lookback_days=int(raw_definition["lookback_days"]),
            )
            raw_result = document["result"]
            result = ScreenerResult(
                definition_name=definition.name,
                universe_revision=definition.universe_revision,
                as_of=date.fromisoformat(str(raw_result["as_of"])),
                data_window=(
                    date.fromisoformat(str(raw_result["data_window_start"])),
                    date.fromisoformat(str(raw_result["data_window_end"])),
                ),
                definition_hash=definition.definition_hash,
                hits=tuple(_hit_from_row(row) for row in raw_result["hits"]),
            )
            return cls(
                name=str(document["name"]),
                definition=definition,
                result=result,
                git_revision=str(document["git_revision"]),
                saved_at=date.fromisoformat(str(document["saved_at"])),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ScreenerError(f"study payload is malformed: {error}") from None


def save_study(study: SavedStudy) -> bytes:
    """Return the canonical artifact bytes for a study."""
    return study.canonical_json().encode("utf-8")


def load_study(payload: bytes) -> SavedStudy:
    """Rebuild a study from artifact bytes, verifying content identity."""
    try:
        document = json.loads(payload.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ScreenerError(f"study payload does not parse: {error}") from None
    if not isinstance(document, Mapping):
        raise ScreenerError("study payload must be a JSON object")
    recorded = document.get("content_sha256")
    study = SavedStudy.from_document(document)
    if recorded is not None and recorded != study.content_sha256:
        raise ScreenerError("recorded content hash does not match the study")
    if save_study(study) != payload:
        raise ArtifactError(
            "artifact bytes do not reproduce the canonical serialization"
        )
    return study


def _hit_from_row(row: Mapping[str, Any]) -> ScreenerHit:
    return ScreenerHit(
        symbol=str(row["symbol"]),
        rank=int(row["rank"]),
        metrics={str(key): str(value) for key, value in dict(row["metrics"]).items()},
    )
