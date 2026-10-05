"""Falsifiable hypothesis artifacts as workspace Markdown documents.

A hypothesis artifact is one Markdown page. Published versions live under
``hypotheses/<slug>/<sha256>.md``; legacy ``hypotheses/<slug>.md`` pages remain
readable within their explicitly selected workspace. Its
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
import os
import re
import tempfile
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

from thesis.persistence.git import GitCoordinator
from thesis.persistence.journal import ArtifactRef, CommandJournal, content_hash
from thesis.research.grammar import IDENTIFIER_PATTERN
from thesis.workspaces.repository import WorkspaceRepositoryProvider

FRONT_MATTER_DELIMITER = "---"
SCHEMA_VERSION = 1
HYPOTHESIS_DIRECTORY = "hypotheses"
_ARTIFACT_MEDIA_TYPE = "text/markdown"

_UNSAFE_SLUG = re.compile(r"[^a-z0-9]+")
_WORKSPACE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")

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
        pattern=IDENTIFIER_PATTERN,
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

    def save(self, document: HypothesisDocument, *, workspace_id: str) -> str:
        """Persist the artifact and return its workspace-relative path."""

    def load(self, hypothesis_id: str, *, workspace_id: str) -> HypothesisDocument:
        """Load one artifact by identity."""

    def list(self, *, workspace_id: str) -> tuple[str, ...]:
        """Return every stored hypothesis identifier in deterministic order."""


class InMemoryHypothesisStore:
    """Explicit, workspace-keyed store for tests."""

    def __init__(
        self,
        documents: Iterable[HypothesisDocument] = (),
        *,
        workspace_id: str | None = None,
    ) -> None:
        self._documents: dict[tuple[str, str], HypothesisDocument] = {}
        for document in documents:
            if workspace_id is None:
                raise HypothesisArtifactError("seed documents require workspace_id")
            self.save(document, workspace_id=workspace_id)

    def save(self, document: HypothesisDocument, *, workspace_id: str) -> str:
        _validate_workspace_id(workspace_id)
        document = _validated_document(document)
        self._documents[(workspace_id, document.hypothesis_id)] = document
        return document.relative_path

    def load(self, hypothesis_id: str, *, workspace_id: str) -> HypothesisDocument:
        _validate_workspace_id(workspace_id)
        _validate_hypothesis_id(hypothesis_id)
        try:
            return self._documents[(workspace_id, hypothesis_id)]
        except KeyError as error:
            raise HypothesisArtifactError(
                f"hypothesis {hypothesis_id!r} was not found"
            ) from error

    def list(self, *, workspace_id: str) -> tuple[str, ...]:
        _validate_workspace_id(workspace_id)
        return tuple(
            sorted(key[1] for key in self._documents if key[0] == workspace_id)
        )


class WorkspaceHypothesisStore:
    """Publish immutable Markdown and a latest-version manifest per workspace.

    All paths come from the verified repository provider. The journal and Git
    fences isolate publication from unrelated worktree/index edits. Legacy
    ``hypotheses/<slug>.md`` is readable only in the selected workspace; no
    provider-root fallback or cross-workspace discovery is performed.
    """

    def __init__(self, workspaces: WorkspaceRepositoryProvider) -> None:
        if not all(
            callable(getattr(workspaces, name, None))
            for name in ("open", "resolve_path", "publish", "repository_uri")
        ):
            raise TypeError("workspaces must implement WorkspaceRepositoryProvider")
        self._workspaces = workspaces

    def path_for(self, hypothesis_id: str, *, workspace_id: str) -> Path:
        """Return the latest immutable artifact, or its scoped legacy path."""
        _validate_workspace_id(workspace_id)
        _validate_hypothesis_id(hypothesis_id)
        manifest = self._manifest(hypothesis_id, workspace_id=workspace_id)
        if manifest is None:
            relative = f"{HYPOTHESIS_DIRECTORY}/{slugify(hypothesis_id)}.md"
        else:
            relative = _version_path(hypothesis_id, manifest["contentSha256"])
        return self._workspaces.resolve_path(workspace_id, relative)

    def save(self, document: HypothesisDocument, *, workspace_id: str) -> str:
        _validate_workspace_id(workspace_id)
        document = _validated_document(document)
        root = self._workspaces.open(workspace_id).root
        manifest_relative = _manifest_path(document.hypothesis_id)
        self._workspaces.resolve_path(workspace_id, manifest_relative)
        coordinator = GitCoordinator(root, manifest_path=manifest_relative)
        transaction = GitCoordinator(root, lock_name="thesis-hypotheses.lock")
        relative = _version_path(document.hypothesis_id, document.content_sha256)
        payload = document.to_bytes()
        with transaction.publication_lock():
            previous = self._manifest(document.hypothesis_id, workspace_id=workspace_id)
            legacy = self._workspaces.resolve_path(workspace_id, document.relative_path)
            if previous is not None or legacy.exists():
                existing = self.load(document.hypothesis_id, workspace_id=workspace_id)
                if previous is not None and existing == document:
                    self._workspaces.publish(workspace_id)
                    return relative
            path = self._workspaces.resolve_path(workspace_id, relative)
            if path.exists() and path.read_bytes() != payload:
                raise HypothesisArtifactError(
                    "immutable hypothesis path has different content"
                )
            journal_path = self._workspaces.resolve_path(
                workspace_id, ".thesis/hypotheses-journal.sqlite3"
            )
            journal = CommandJournal(journal_path)
            state = {
                "workspaceId": workspace_id,
                "hypothesisId": document.hypothesis_id,
                "contentSha256": document.content_sha256,
            }
            expected_head = coordinator.head()
            entry = journal.accept(
                "hypothesis.save",
                {**state, "previous": previous, "headBefore": expected_head},
                state,
                workflow_hash=content_hash("fca:hypothesis-store:v1"),
                model_hash=document.content_sha256,
                artifacts=(
                    ArtifactRef.from_bytes(
                        relative, payload, media_type=document.media_type
                    ),
                ),
            )
            # Install complete bytes atomically before publication; failed pushes
            # retain the accepted local version for an idempotent retry.
            _atomic_write(path, payload)
            coordinator.publish(
                entry, expected_head=expected_head, artifacts={relative: payload}
            )
            self._workspaces.publish(workspace_id)
        return relative

    def _manifest(
        self, hypothesis_id: str, *, workspace_id: str
    ) -> dict[str, str] | None:
        path = self._workspaces.resolve_path(
            workspace_id, _manifest_path(hypothesis_id)
        )
        if not path.exists():
            coordinator = GitCoordinator(self._workspaces.open(workspace_id).root)
            if coordinator.tracked_paths(_manifest_path(hypothesis_id)):
                raise HypothesisArtifactError(
                    "committed hypothesis manifest is missing"
                )
            return None
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
            state = manifest["canonicalState"]
            if (
                state["workspaceId"] != workspace_id
                or state["hypothesisId"] != hypothesis_id
                or not isinstance(state["contentSha256"], str)
                or _DIGEST.fullmatch(state["contentSha256"]) is None
            ):
                raise ValueError("hypothesis manifest identity or hash does not match")
            return {
                "workspaceId": workspace_id,
                "hypothesisId": hypothesis_id,
                "contentSha256": state["contentSha256"],
            }
        except (OSError, UnicodeError, ValueError, KeyError, TypeError) as error:
            raise HypothesisArtifactError(
                "invalid hypothesis publication manifest"
            ) from error

    def load(self, hypothesis_id: str, *, workspace_id: str) -> HypothesisDocument:
        _validate_workspace_id(workspace_id)
        _validate_hypothesis_id(hypothesis_id)
        manifest = self._manifest(hypothesis_id, workspace_id=workspace_id)
        relative = (
            f"{HYPOTHESIS_DIRECTORY}/{slugify(hypothesis_id)}.md"
            if manifest is None
            else _version_path(hypothesis_id, manifest["contentSha256"])
        )
        path = self._workspaces.resolve_path(workspace_id, relative)
        try:
            payload = path.read_bytes()
            document = HypothesisDocument.parse(payload.decode("utf-8"))
        except (OSError, UnicodeError) as error:
            raise HypothesisArtifactError(
                f"hypothesis {hypothesis_id!r} was not found"
            ) from error
        if document.hypothesis_id != hypothesis_id:
            raise HypothesisArtifactError(
                "stored hypothesis identity does not match its path"
            )
        if manifest is not None and (
            document.content_sha256 != manifest["contentSha256"]
            or payload != document.to_bytes()
        ):
            raise HypothesisArtifactError(
                "stored hypothesis content hash does not match"
            )
        if manifest is not None:
            coordinator = GitCoordinator(self._workspaces.open(workspace_id).root)
            head = coordinator.head()
            manifest_relative = _manifest_path(hypothesis_id)
            manifest_payload = self._workspaces.resolve_path(
                workspace_id, manifest_relative
            ).read_bytes()
            if head is None or not (
                coordinator.file_matches(head, relative, payload)
                and coordinator.file_matches(head, manifest_relative, manifest_payload)
            ):
                raise HypothesisArtifactError(
                    "hypothesis publication differs from committed content"
                )
        return document

    def list(self, *, workspace_id: str) -> tuple[str, ...]:
        _validate_workspace_id(workspace_id)
        directory = self._workspaces.resolve_path(workspace_id, HYPOTHESIS_DIRECTORY)
        manifests = self._workspaces.resolve_path(workspace_id, ".thesis/hypotheses")
        coordinator = GitCoordinator(self._workspaces.open(workspace_id).root)
        for relative in coordinator.tracked_paths(".thesis/hypotheses"):
            if not self._workspaces.resolve_path(workspace_id, relative).is_file():
                raise HypothesisArtifactError(
                    "committed hypothesis manifest is missing"
                )
        identifiers: set[str] = set()
        for path in sorted(manifests.glob("*.json")):
            resolved = self._workspaces.resolve_path(
                workspace_id, f".thesis/hypotheses/{path.name}"
            )
            try:
                identity = json.loads(resolved.read_text(encoding="utf-8"))[
                    "canonicalState"
                ]["hypothesisId"]
                _validate_hypothesis_id(identity)
                if _manifest_path(identity) != f".thesis/hypotheses/{path.name}":
                    raise HypothesisArtifactError(
                        "hypothesis manifest path does not match"
                    )
                self.load(identity, workspace_id=workspace_id)
            except (OSError, UnicodeError, ValueError, KeyError, TypeError) as error:
                raise HypothesisArtifactError(
                    "invalid hypothesis publication manifest"
                ) from error
            identifiers.add(identity)
        for path in sorted(directory.glob("*.md")):
            resolved = self._workspaces.resolve_path(
                workspace_id, f"hypotheses/{path.name}"
            )
            document = HypothesisDocument.parse(resolved.read_text(encoding="utf-8"))
            if document.relative_path != f"hypotheses/{path.name}":
                raise HypothesisArtifactError(
                    "stored hypothesis identity does not match its path"
                )
            identifiers.add(document.hypothesis_id)
        return tuple(sorted(identifiers))


def _validate_workspace_id(value: str) -> None:
    if not isinstance(value, str) or _WORKSPACE_ID.fullmatch(value) is None:
        raise HypothesisArtifactError(
            "workspace_id must be a safe workspace identifier"
        )


def _validate_hypothesis_id(value: str) -> None:
    if not isinstance(value, str) or re.fullmatch(IDENTIFIER_PATTERN, value) is None:
        raise HypothesisArtifactError("hypothesis_id must be a safe identifier")


def _validated_document(document: HypothesisDocument) -> HypothesisDocument:
    if not isinstance(document, HypothesisDocument):
        raise TypeError("document must be a HypothesisDocument")
    validated = HypothesisDocument.parse(document.render())
    if validated != document:
        raise HypothesisArtifactError("hypothesis document must be canonical")
    return validated


def _manifest_path(hypothesis_id: str) -> str:
    return f".thesis/hypotheses/{slugify(hypothesis_id)}.json"


def _version_path(hypothesis_id: str, digest: str) -> str:
    return f"{HYPOTHESIS_DIRECTORY}/{slugify(hypothesis_id)}/{digest}.md"


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".hypothesis-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


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
