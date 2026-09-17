"""Explicit global and workspace-scoped indicator references and storage.

Built-in indicators live in the immutable harness registry. User- or
agent-authored indicators are content-addressed workspace artifacts and never
mutate that registry. Consumers carry an :class:`IndicatorRef`, so resolution
cannot silently shadow a global name or cross a workspace boundary.
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

from harness.indicators import DEFAULT_REGISTRY, IndicatorRegistry, IndicatorSpec

INDICATOR_DIRECTORY = "indicators"
_HASH_DOMAIN = b"fca:thesis-workspace-indicator:v1\0"
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$")
_WORKSPACE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_ENTRYPOINT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


class IndicatorArtifactError(ValueError):
    """A scoped indicator reference or artifact is invalid."""


class IndicatorScope(StrEnum):
    """Where an indicator definition is owned."""

    GLOBAL = "global"
    WORKSPACE = "workspace"


def _workspace_id(value: str) -> str:
    if not isinstance(value, str) or _WORKSPACE_ID.fullmatch(value) is None:
        raise IndicatorArtifactError("workspace_id must be a safe workspace identifier")
    return value


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise IndicatorArtifactError("indicator values must be JSON-shaped") from error


def _frozen_mapping(value: Mapping[str, object], label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise IndicatorArtifactError(f"{label} must be a string-keyed mapping")
    _canonical_bytes(value)
    return MappingProxyType(dict(value))


@dataclass(frozen=True, slots=True)
class IndicatorRef:
    """An explicit reference to a global or workspace-owned indicator."""

    scope: IndicatorScope
    name: str
    workspace_id: str | None = None
    content_sha256: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.scope, IndicatorScope):
            raise IndicatorArtifactError("indicator scope must be global or workspace")
        if not isinstance(self.name, str) or _NAME.fullmatch(self.name) is None:
            raise IndicatorArtifactError("indicator name must match ^[a-z][a-z0-9_]*$")
        if self.scope is IndicatorScope.GLOBAL:
            if self.workspace_id is not None or self.content_sha256 is not None:
                raise IndicatorArtifactError(
                    "global indicator references cannot carry workspace identity"
                )
            return
        if self.workspace_id is None or self.content_sha256 is None:
            raise IndicatorArtifactError(
                "workspace indicator references require workspace_id and content_sha256"
            )
        _workspace_id(self.workspace_id)
        if _DIGEST.fullmatch(self.content_sha256) is None:
            raise IndicatorArtifactError("content_sha256 must be a lowercase SHA-256")

    @classmethod
    def global_indicator(cls, name: str) -> IndicatorRef:
        """Reference one immutable platform indicator."""
        return cls(IndicatorScope.GLOBAL, name)

    @classmethod
    def workspace_indicator(
        cls, workspace_id: str, name: str, content_sha256: str
    ) -> IndicatorRef:
        """Reference one immutable indicator artifact in a workspace."""
        return cls(IndicatorScope.WORKSPACE, name, workspace_id, content_sha256)

    @property
    def key(self) -> str:
        """Return the unambiguous portable reference key."""
        if self.scope is IndicatorScope.GLOBAL:
            return f"global:{self.name}"
        return f"workspace:{self.workspace_id}/{self.name}@{self.content_sha256}"

    def as_dict(self) -> dict[str, object]:
        """Return the portable reference document."""
        return {
            "contentSha256": self.content_sha256,
            "name": self.name,
            "scope": self.scope.value,
            "workspaceId": self.workspace_id,
        }


@dataclass(frozen=True, slots=True)
class WorkspaceIndicator:
    """One content-addressed Python indicator owned on workspace publication.

    The source defines ``calculate(context, parameters)`` by default. It is a
    data transformation only: it receives plain research data and cannot place
    orders. Runtime isolation is owned by the research-model executor.
    """

    name: str
    version: str
    description: str
    source: str
    entrypoint: str = "calculate"
    parameter_schema: Mapping[str, object] = field(default_factory=dict)
    tests: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or _NAME.fullmatch(self.name) is None:
            raise IndicatorArtifactError("indicator name must match ^[a-z][a-z0-9_]*$")
        if (
            not isinstance(self.version, str)
            or _VERSION.fullmatch(self.version) is None
        ):
            raise IndicatorArtifactError(
                "indicator version must use semantic versioning"
            )
        if not isinstance(self.description, str) or not self.description.strip():
            raise IndicatorArtifactError("indicator description must be non-empty")
        if not isinstance(self.source, str) or not self.source.strip():
            raise IndicatorArtifactError("indicator source must be non-empty")
        if len(self.source.encode("utf-8")) > 1_048_576:
            raise IndicatorArtifactError("indicator source exceeds the 1 MiB limit")
        if (
            not isinstance(self.entrypoint, str)
            or _ENTRYPOINT.fullmatch(self.entrypoint) is None
        ):
            raise IndicatorArtifactError(
                "indicator entrypoint must be a Python identifier"
            )
        try:
            tree = ast.parse(self.source, filename=f"<{self.name}>", mode="exec")
        except SyntaxError as error:
            raise IndicatorArtifactError(
                f"indicator source does not parse: {error}"
            ) from None
        if not any(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == self.entrypoint
            for node in tree.body
        ):
            raise IndicatorArtifactError(
                f"indicator source must define {self.entrypoint}(context, parameters)"
            )
        object.__setattr__(self, "description", self.description.strip())
        object.__setattr__(
            self,
            "parameter_schema",
            _frozen_mapping(self.parameter_schema, "parameter_schema"),
        )
        if not isinstance(self.tests, Mapping) or not all(
            isinstance(path, str) and path and isinstance(content, str)
            for path, content in self.tests.items()
        ):
            raise IndicatorArtifactError(
                "indicator tests must be a path-to-text mapping"
            )
        object.__setattr__(self, "tests", MappingProxyType(dict(self.tests)))

    @property
    def canonical_document(self) -> dict[str, object]:
        """Return the location-independent artifact document."""
        return {
            "description": self.description,
            "entrypoint": self.entrypoint,
            "name": self.name,
            "parameterSchema": dict(self.parameter_schema),
            "schema": "fca-thesis-workspace-indicator-v1",
            "source": self.source,
            "tests": dict(self.tests),
            "version": self.version,
        }

    @property
    def content_sha256(self) -> str:
        """Return the domain-separated content identity."""
        return hashlib.sha256(
            _HASH_DOMAIN + _canonical_bytes(self.canonical_document)
        ).hexdigest()

    def reference(self, workspace_id: str) -> IndicatorRef:
        """Bind this location-independent definition to one workspace."""
        return IndicatorRef.workspace_indicator(
            workspace_id, self.name, self.content_sha256
        )


class IndicatorStore(Protocol):
    """Workspace-scoped publication and resolution boundary."""

    def publish(self, workspace_id: str, indicator: WorkspaceIndicator) -> IndicatorRef:
        """Publish an immutable definition in one workspace."""

    def load(self, workspace_id: str, reference: IndicatorRef) -> WorkspaceIndicator:
        """Resolve a workspace reference only inside its owning workspace."""


class InMemoryIndicatorStore:
    """Deterministic workspace-scoped store used by tests."""

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], WorkspaceIndicator] = {}

    def publish(self, workspace_id: str, indicator: WorkspaceIndicator) -> IndicatorRef:
        workspace_id = _workspace_id(workspace_id)
        if not isinstance(indicator, WorkspaceIndicator):
            raise TypeError("indicator must be a WorkspaceIndicator")
        reference = indicator.reference(workspace_id)
        self._items[(workspace_id, indicator.content_sha256)] = indicator
        return reference

    def load(self, workspace_id: str, reference: IndicatorRef) -> WorkspaceIndicator:
        _require_workspace_reference(workspace_id, reference)
        try:
            return self._items[(workspace_id, reference.content_sha256 or "")]
        except KeyError as error:
            raise IndicatorArtifactError(
                f"workspace indicator {reference.key!r} was not found"
            ) from error


class WorkspacePathProvider(Protocol):
    """The confined path resolver supplied by ``thesis.workspaces``."""

    def resolve_path(
        self,
        workspace_id: str,
        relative_path: str | Path = ".",
        *,
        must_exist: bool = False,
    ) -> Path:
        """Resolve a path beneath exactly one selected workspace."""


class WorkspaceIndicatorStore:
    """Filesystem publication under an injected workspace path boundary."""

    def __init__(self, workspaces: WorkspacePathProvider) -> None:
        if not callable(getattr(workspaces, "resolve_path", None)):
            raise TypeError("workspaces must implement WorkspacePathProvider")
        self._workspaces = workspaces

    def publish(self, workspace_id: str, indicator: WorkspaceIndicator) -> IndicatorRef:
        workspace_id = _workspace_id(workspace_id)
        if not isinstance(indicator, WorkspaceIndicator):
            raise TypeError("indicator must be a WorkspaceIndicator")
        reference = indicator.reference(workspace_id)
        relative = _artifact_directory(indicator.name, indicator.content_sha256)
        directory = self._workspaces.resolve_path(workspace_id, relative)
        manifest = _manifest_bytes(workspace_id, indicator)
        if directory.exists():
            existing = self.load(workspace_id, reference)
            if existing.canonical_document != indicator.canonical_document:
                raise IndicatorArtifactError(
                    "immutable indicator path has different content"
                )
            return reference
        directory.mkdir(parents=True, exist_ok=False)
        (directory / "definition.json").write_bytes(manifest)
        (directory / "indicator.py").write_text(indicator.source, encoding="utf-8")
        for path, content in indicator.tests.items():
            target = _confined_test_path(directory / "tests", path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return reference

    def load(self, workspace_id: str, reference: IndicatorRef) -> WorkspaceIndicator:
        _require_workspace_reference(workspace_id, reference)
        relative = _artifact_directory(reference.name, reference.content_sha256 or "")
        path = self._workspaces.resolve_path(
            workspace_id, f"{relative}/definition.json", must_exist=True
        )
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            definition = document["definition"]
            indicator = WorkspaceIndicator(
                name=definition["name"],
                version=definition["version"],
                description=definition["description"],
                source=definition["source"],
                entrypoint=definition["entrypoint"],
                parameter_schema=definition["parameterSchema"],
                tests=definition["tests"],
            )
        except (
            KeyError,
            OSError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as error:
            raise IndicatorArtifactError(
                "workspace indicator artifact is invalid"
            ) from error
        if document.get("workspaceId") != workspace_id:
            raise IndicatorArtifactError(
                "indicator artifact belongs to another workspace"
            )
        if document.get("contentSha256") != indicator.content_sha256:
            raise IndicatorArtifactError(
                "indicator artifact content hash does not match"
            )
        if indicator.content_sha256 != reference.content_sha256:
            raise IndicatorArtifactError(
                "indicator reference content hash does not match"
            )
        return indicator


class ScopedIndicatorResolver:
    """Resolve explicit refs without merging global and workspace namespaces."""

    def __init__(
        self,
        workspace_store: IndicatorStore,
        *,
        global_registry: IndicatorRegistry = DEFAULT_REGISTRY,
    ) -> None:
        if not isinstance(global_registry, IndicatorRegistry):
            raise TypeError("global_registry must be an IndicatorRegistry")
        self._global = global_registry
        self._workspace = workspace_store

    def resolve(
        self, reference: IndicatorRef, *, workspace_id: str
    ) -> IndicatorSpec | WorkspaceIndicator:
        """Resolve one reference in the selected workspace context."""
        if not isinstance(reference, IndicatorRef):
            raise TypeError("reference must be an IndicatorRef")
        if reference.scope is IndicatorScope.GLOBAL:
            return self._global.spec(reference.name)
        return self._workspace.load(workspace_id, reference)


def _require_workspace_reference(workspace_id: str, reference: IndicatorRef) -> None:
    workspace_id = _workspace_id(workspace_id)
    if (
        not isinstance(reference, IndicatorRef)
        or reference.scope is not IndicatorScope.WORKSPACE
    ):
        raise IndicatorArtifactError("a workspace indicator reference is required")
    if reference.workspace_id != workspace_id:
        raise IndicatorArtifactError("indicator reference belongs to another workspace")


def _artifact_directory(name: str, digest: str) -> str:
    return f"{INDICATOR_DIRECTORY}/{name}/{digest}"


def _manifest_bytes(workspace_id: str, indicator: WorkspaceIndicator) -> bytes:
    return (
        _canonical_bytes(
            {
                "contentSha256": indicator.content_sha256,
                "definition": indicator.canonical_document,
                "schema": "fca-thesis-workspace-indicator-publication-v1",
                "workspaceId": workspace_id,
            }
        )
        + b"\n"
    )


def _confined_test_path(root: Path, relative: str) -> Path:
    candidate = (root / relative).resolve(strict=False)
    boundary = root.resolve(strict=False)
    if not candidate.is_relative_to(boundary):
        raise IndicatorArtifactError("indicator test path escapes its artifact")
    return candidate


__all__ = [
    "INDICATOR_DIRECTORY",
    "InMemoryIndicatorStore",
    "IndicatorArtifactError",
    "IndicatorRef",
    "IndicatorScope",
    "IndicatorStore",
    "ScopedIndicatorResolver",
    "WorkspaceIndicator",
    "WorkspaceIndicatorStore",
    "WorkspacePathProvider",
]
