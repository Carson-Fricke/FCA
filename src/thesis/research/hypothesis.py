"""Falsifiable hypothesis artifacts as workspace Markdown documents.

A hypothesis artifact is one Markdown page under ``hypotheses/<slug>.md``.  Its
front matter is a small, strictly validated document rendered in the
deterministic JSON subset of YAML 1.2, exactly like the workspace manifest, so
no additional YAML dependency is required.  The prose body carries the agent's
reasoning while the front matter carries the machine-checkable fields the
workflow needs.

The artifact is deliberately a *projection*, not the source of truth:

* the agent owns the prose fields ``claim``, ``disagreement``, ``risk_factor``,
  ``horizon``, ``universe``, ``falsifiers``, and ``sizing``;
* the workflow and the hash-chained experiment ledger own ``status`` and
  ``links``; and
* ``links.runs`` and ``links.evidence`` are populated from executed backtests,
  never hand-edited.

The module provides the validated model, a deterministic renderer and parser,
and a workspace-backed :class:`WorkspaceHypothesisStore` plus an in-memory
store for tests.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Protocol

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

FRONT_MATTER_DELIMITER = "---"
SCHEMA_VERSION = 1
HYPOTHESIS_DIRECTORY = "hypotheses"
_ARTIFACT_MEDIA_TYPE = "text/markdown"

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$")
_UNSAFE_SLUG = re.compile(r"[^a-z0-9]+")

FalsifierCondition = Literal["gte", "lte", "gt", "lt"]
FALSIFIER_CONDITIONS: frozenset[str] = frozenset({"gte", "lte", "gt", "lt"})


class HypothesisArtifactError(ValueError):
    """Raised when a hypothesis artifact cannot be validated or parsed."""


def _as_tuple(value: object) -> object:
    return tuple(value) if isinstance(value, list) else value


def _finite_threshold(value: object) -> object:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("threshold must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("threshold must be a finite number")
    return number


Identifier = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$",
    ),
]
NonEmptyText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=8_192),
]
ShortText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=4_096),
]


class StrictHypothesisModel(BaseModel):
    """Base model that rejects undeclared fields and stays immutable."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class HypothesisStatus(StrEnum):
    """The projection status owned by the workflow and the ledger."""

    DRAFT = "draft"
    ACTIVE = "active"
    SUPPORTED = "supported"
    REFUTED = "refuted"
    INCONCLUSIVE = "inconclusive"
    ARCHIVED = "archived"


class HypothesisFalsifier(StrictHypothesisModel):
    """One observable condition whose satisfaction falsifies the claim.

    A falsifier is the atom of the falsification-first pipeline: it names the
    ``observable`` the trial measures, the ``condition`` and ``threshold`` that
    would refute the premise, and the ``test`` that produces the observation.
    """

    observable: Identifier
    condition: FalsifierCondition
    threshold: Annotated[float, BeforeValidator(_finite_threshold)]
    test: NonEmptyText
    falsifier_id: Identifier | None = None

    def falsifies(self, observed: float) -> bool:
        """Return whether one observed value satisfies the falsifier."""
        if self.condition == "gte":
            return observed >= self.threshold
        if self.condition == "lte":
            return observed <= self.threshold
        if self.condition == "gt":
            return observed > self.threshold
        return observed < self.threshold

    def matches(self, other: HypothesisFalsifier) -> bool:
        """Return whether another falsifier re-checks the same condition."""
        return (
            self.observable == other.observable
            and self.condition == other.condition
            and self.threshold == other.threshold
        )


class HypothesisLinks(StrictHypothesisModel):
    """Workflow-owned references to executed runs and recorded evidence."""

    runs: Annotated[tuple[str, ...], BeforeValidator(_as_tuple)] = ()
    evidence: Annotated[tuple[str, ...], BeforeValidator(_as_tuple)] = ()


class HypothesisFrontMatter(StrictHypothesisModel):
    """The strictly validated front matter of one hypothesis artifact."""

    schema_version: Literal[1] = SCHEMA_VERSION
    hypothesis_id: Identifier
    status: HypothesisStatus
    claim: NonEmptyText
    disagreement: NonEmptyText
    risk_factor: ShortText
    horizon: ShortText
    universe: Annotated[
        tuple[Identifier, ...], BeforeValidator(_as_tuple), Field(min_length=1)
    ]
    falsifiers: Annotated[
        tuple[HypothesisFalsifier, ...],
        BeforeValidator(_as_tuple),
    ] = ()
    sizing: ShortText | None = None
    links: HypothesisLinks = Field(default_factory=HypothesisLinks)

    @model_validator(mode="after")
    def _identifiers_are_unique(self) -> HypothesisFrontMatter:
        if len(set(self.universe)) != len(self.universe):
            raise ValueError("universe identifiers must be unique")
        declared = [
            falsifier.falsifier_id
            for falsifier in self.falsifiers
            if falsifier.falsifier_id is not None
        ]
        if len(set(declared)) != len(declared):
            raise ValueError("falsifier identifiers must be unique")
        return self


@dataclass(frozen=True, slots=True)
class HypothesisDocument:
    """A parsed hypothesis artifact: validated front matter plus prose."""

    front_matter: HypothesisFrontMatter
    body: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.front_matter, HypothesisFrontMatter):
            raise HypothesisArtifactError("front_matter must be validated front matter")
        if not isinstance(self.body, str):
            raise HypothesisArtifactError("hypothesis body must be text")
        object.__setattr__(self, "body", self.body.strip())

    @property
    def hypothesis_id(self) -> str:
        """Return the stable hypothesis identity."""
        return self.front_matter.hypothesis_id

    @property
    def status(self) -> HypothesisStatus:
        """Return the projection status of the artifact."""
        return self.front_matter.status

    @property
    def slug(self) -> str:
        """Return the safe filename slug for this hypothesis."""
        return slugify(self.hypothesis_id)

    @property
    def relative_path(self) -> str:
        """Return the workspace-relative artifact path."""
        return f"{HYPOTHESIS_DIRECTORY}/{self.slug}.md"

    @property
    def media_type(self) -> str:
        """Return the artifact media type."""
        return _ARTIFACT_MEDIA_TYPE

    def render(self) -> str:
        """Render the deterministic Markdown artifact text."""
        front = json.dumps(
            self.front_matter.model_dump(mode="json"),
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        blocks = [
            FRONT_MATTER_DELIMITER,
            front,
            FRONT_MATTER_DELIMITER,
        ]
        if self.body:
            blocks.extend(("", self.body))
        return "\n".join(blocks) + "\n"

    def to_bytes(self) -> bytes:
        """Return the artifact as UTF-8 bytes for Git publication."""
        return self.render().encode("utf-8")

    @property
    def content_sha256(self) -> str:
        """Return the SHA-256 digest of the rendered artifact."""
        return hashlib.sha256(self.to_bytes()).hexdigest()

    def projected(
        self,
        *,
        status: HypothesisStatus | None = None,
        links: HypothesisLinks | None = None,
    ) -> HypothesisDocument:
        """Return a copy with workflow-owned status/links projected onto it."""
        front = self.front_matter
        if status is not None or links is not None:
            front = front.model_copy(
                update={
                    **({"status": status} if status is not None else {}),
                    **({"links": links} if links is not None else {}),
                }
            )
            front = HypothesisFrontMatter.model_validate(front.model_dump())
        return HypothesisDocument(front_matter=front, body=self.body)

    @classmethod
    def parse(cls, text: str) -> HypothesisDocument:
        """Parse and validate a rendered hypothesis artifact."""
        if not isinstance(text, str):
            raise HypothesisArtifactError("hypothesis artifact must be text")
        lines = text.splitlines()
        if not lines or lines[0].strip() != FRONT_MATTER_DELIMITER:
            raise HypothesisArtifactError(
                "hypothesis artifact must start with YAML front matter"
            )
        try:
            end = lines.index(FRONT_MATTER_DELIMITER, 1)
        except ValueError as error:
            raise HypothesisArtifactError(
                "hypothesis front matter is not terminated"
            ) from error
        front_text = "\n".join(lines[1:end])
        try:
            values = json.loads(front_text)
        except json.JSONDecodeError as error:
            raise HypothesisArtifactError(
                "hypothesis front matter must use the deterministic JSON subset"
            ) from error
        if not isinstance(values, dict):
            raise HypothesisArtifactError("hypothesis front matter must be a mapping")
        try:
            front_matter = HypothesisFrontMatter.model_validate(values)
        except ValueError as error:
            raise HypothesisArtifactError(
                f"invalid hypothesis front matter: {error}"
            ) from error
        body = "\n".join(lines[end + 1 :]).strip()
        return cls(front_matter=front_matter, body=body)


def slugify(value: str) -> str:
    """Return a filesystem-safe slug for a hypothesis identifier."""
    slug = _UNSAFE_SLUG.sub("-", value.strip().lower()).strip("-")
    return slug or "hypothesis"


def render_hypothesis(document: HypothesisDocument) -> str:
    """Render one hypothesis artifact to deterministic Markdown text."""
    return document.render()


def parse_hypothesis(text: str) -> HypothesisDocument:
    """Parse one hypothesis artifact from Markdown text."""
    return HypothesisDocument.parse(text)


class HypothesisStore(Protocol):
    """Workspace-backed persistence boundary for hypothesis artifacts."""

    def save(self, document: HypothesisDocument) -> str:
        """Persist the artifact and return its workspace-relative path."""

    def load(self, hypothesis_id: str) -> HypothesisDocument:
        """Load one artifact by identity."""

    def list(self) -> tuple[str, ...]:
        """Return every stored hypothesis identifier in deterministic order."""


class InMemoryHypothesisStore:
    """Deterministic store used by tests and ephemeral runtimes."""

    def __init__(self, documents: Iterable[HypothesisDocument] = ()) -> None:
        self._documents: dict[str, HypothesisDocument] = {}
        for document in documents:
            self._documents[document.hypothesis_id] = document

    def save(self, document: HypothesisDocument) -> str:
        self._documents[document.hypothesis_id] = document
        return document.relative_path

    def load(self, hypothesis_id: str) -> HypothesisDocument:
        try:
            return self._documents[hypothesis_id]
        except KeyError as error:
            raise HypothesisArtifactError(
                f"hypothesis {hypothesis_id!r} was not found"
            ) from error

    def list(self) -> tuple[str, ...]:
        return tuple(sorted(self._documents))


class WorkspaceHypothesisStore:
    """Filesystem store rooted at a workspace repository.

    The store owns only ``hypotheses/<slug>.md`` under the injected workspace
    root.  Paths are resolved beneath the root and traversal is rejected, so a
    caller cannot escape the selected workspace.
    """

    def __init__(self, workspace_root: str | Path) -> None:
        root = Path(workspace_root).expanduser().resolve(strict=False)
        root.mkdir(parents=True, exist_ok=True)
        self._root = root
        self._directory = root / HYPOTHESIS_DIRECTORY
        self._directory.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        """Return the workspace root this store writes beneath."""
        return self._root

    def path_for(self, hypothesis_id: str) -> Path:
        """Return the validated absolute path for one hypothesis artifact."""
        relative = f"{HYPOTHESIS_DIRECTORY}/{slugify(hypothesis_id)}.md"
        candidate = (self._root / relative).resolve(strict=False)
        if not candidate.is_relative_to(self._root):
            raise HypothesisArtifactError("hypothesis path escapes the workspace")
        return candidate

    def save(self, document: HypothesisDocument) -> str:
        path = self.path_for(document.hypothesis_id)
        path.write_bytes(document.to_bytes())
        return document.relative_path

    def load(self, hypothesis_id: str) -> HypothesisDocument:
        path = self.path_for(hypothesis_id)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise HypothesisArtifactError(
                f"hypothesis {hypothesis_id!r} was not found"
            ) from error
        document = HypothesisDocument.parse(text)
        if document.hypothesis_id != hypothesis_id:
            raise HypothesisArtifactError(
                "stored hypothesis identity does not match its path"
            )
        return document

    def list(self) -> tuple[str, ...]:
        identifiers: list[str] = []
        for path in sorted(self._directory.glob("*.md")):
            try:
                document = HypothesisDocument.parse(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, HypothesisArtifactError):
                continue
            identifiers.append(document.hypothesis_id)
        return tuple(identifiers)


__all__ = [
    "FALSIFIER_CONDITIONS",
    "FRONT_MATTER_DELIMITER",
    "HYPOTHESIS_DIRECTORY",
    "HypothesisArtifactError",
    "HypothesisDocument",
    "HypothesisFalsifier",
    "HypothesisFrontMatter",
    "HypothesisLinks",
    "HypothesisStatus",
    "HypothesisStore",
    "InMemoryHypothesisStore",
    "SCHEMA_VERSION",
    "WorkspaceHypothesisStore",
    "parse_hypothesis",
    "render_hypothesis",
    "slugify",
]
