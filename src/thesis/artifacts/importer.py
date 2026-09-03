"""Hash-verified, Git-backed import of harness backtest artifacts."""

from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol
from urllib.parse import urlsplit

from harness.configuration import canonical_configuration_bytes

_DIGEST = "0123456789abcdef"
_VERSION_CHARS = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ.-+"


class ArtifactImportError(RuntimeError):
    """Base class for safe artifact import failures."""


class ArtifactHashMismatchError(ArtifactImportError):
    """A harness artifact did not match its manifest digest or size."""


class ArtifactProvenanceError(ArtifactImportError):
    """Artifact provenance is incomplete or inconsistent."""


def _digest(value: str, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _DIGEST for character in value)
    ):
        raise ArtifactImportError(f"{label} must be a lowercase SHA-256 digest")
    return value


def _identifier(value: str, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or any(
            character
            not in "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz._:@-"
            for character in value
        )
    ):
        raise ArtifactProvenanceError(f"{label} must be a safe identifier")
    return value


def _version(value: str, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 128
        or not value[:1].isdigit()
        or value.count(".") < 2
        or any(character not in _VERSION_CHARS for character in value)
    ):
        raise ArtifactProvenanceError(f"{label} must be semantic version text")
    return value


def _artifact_name(value: str) -> str:
    normalized = value.replace("\\", "/")
    parts = normalized.split("/")
    if (
        not normalized
        or normalized.startswith("/")
        or any(part in {"", ".", ".."} for part in parts)
        or ":" in parts[0]
    ):
        raise ArtifactImportError("artifact names must be confined relative paths")
    return "/".join(parts)


def _relative_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise ArtifactImportError("import paths must be confined relative paths")
    return path


@dataclass(frozen=True, slots=True)
class HarnessArtifact:
    """One artifact entry emitted by a harness reproducibility manifest."""

    name: str
    uri: str
    sha256: str
    size_bytes: int
    media_type: str = "application/octet-stream"
    reproducibility_critical: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _artifact_name(self.name))
        if not isinstance(self.uri, str) or not self.uri:
            raise ArtifactImportError("artifact uri must be non-empty text")
        _digest(self.sha256, "artifact sha256")
        if isinstance(self.size_bytes, bool) or self.size_bytes < 0:
            raise ArtifactImportError("artifact size_bytes must be non-negative")
        if not isinstance(self.media_type, str) or not self.media_type:
            raise ArtifactImportError("artifact media_type must be non-empty text")
        if not isinstance(self.reproducibility_critical, bool):
            raise ArtifactImportError("reproducibility_critical must be boolean")

    @property
    def path(self) -> str:
        """Alias for the harness result URI."""
        return self.uri

    def as_dict(self) -> dict[str, object]:
        """Return the stable manifest representation."""
        return {
            "mediaType": self.media_type,
            "name": self.name,
            "reproducibilityCritical": self.reproducibility_critical,
            "sha256": self.sha256,
            "sizeBytes": self.size_bytes,
            "uri": self.uri,
        }


@dataclass(frozen=True, slots=True)
class HarnessManifest:
    """Portable harness manifest containing every importable artifact digest."""

    manifest_id: str
    artifacts: tuple[HarnessArtifact, ...] | Sequence[HarnessArtifact]
    strategy_version: str | None = None
    experiment_id: str | None = None
    workflow_execution_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "manifest_id", _identifier(self.manifest_id, "manifest_id")
        )
        artifacts = tuple(self.artifacts)
        if not all(isinstance(item, HarnessArtifact) for item in artifacts):
            raise ArtifactImportError(
                "manifest artifacts must contain HarnessArtifact values"
            )
        artifacts = tuple(sorted(artifacts, key=lambda item: item.name))
        if len({item.name for item in artifacts}) != len(artifacts):
            raise ArtifactImportError("manifest artifact names must be unique")
        object.__setattr__(self, "artifacts", artifacts)
        for name in ("strategy_version", "experiment_id", "workflow_execution_id"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(
                    self,
                    name,
                    _version(value, name)
                    if name == "strategy_version"
                    else _identifier(value, name),
                )

    @property
    def canonical_document(self) -> dict[str, object]:
        """Return the location-independent manifest document."""
        return {
            "artifacts": [artifact.as_dict() for artifact in self.artifacts],
            "experimentId": self.experiment_id,
            "manifestId": self.manifest_id,
            "schema": "fca-harness-reproducibility-manifest-v1",
            "strategyVersion": self.strategy_version,
            "workflowExecutionId": self.workflow_execution_id,
        }

    @property
    def canonical_bytes(self) -> bytes:
        """Return deterministic manifest bytes."""
        return canonical_configuration_bytes(self.canonical_document)

    @property
    def sha256(self) -> str:
        """Return the manifest content digest used by provenance."""
        return hashlib.sha256(self.canonical_bytes).hexdigest()

    @property
    def manifest_sha256(self) -> str:
        """Alias for the manifest digest."""
        return self.sha256

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> HarnessManifest:
        """Parse the camel-case harness manifest wire shape without network access."""
        raw_artifacts = value.get("artifacts")
        if not isinstance(raw_artifacts, list):
            raise ArtifactImportError("harness manifest artifacts must be an array")
        artifacts: list[HarnessArtifact] = []
        for raw in raw_artifacts:
            if not isinstance(raw, Mapping):
                raise ArtifactImportError("harness manifest artifact must be an object")
            try:
                critical = raw.get("reproducibilityCritical", True)
                size_bytes = raw["sizeBytes"]
                if (
                    not isinstance(critical, bool)
                    or isinstance(size_bytes, bool)
                    or not isinstance(size_bytes, int)
                ):
                    raise TypeError
                artifacts.append(
                    HarnessArtifact(
                        name=str(raw["name"]),
                        uri=str(raw.get("uri", raw.get("path", ""))),
                        sha256=str(raw["sha256"]),
                        size_bytes=size_bytes,
                        media_type=str(
                            raw.get("mediaType", "application/octet-stream")
                        ),
                        reproducibility_critical=critical,
                    )
                )
            except (KeyError, TypeError, ValueError) as error:
                raise ArtifactImportError(
                    "invalid harness manifest artifact"
                ) from error
        return cls(
            manifest_id=str(value.get("manifestId", value.get("manifest_id", ""))),
            artifacts=tuple(artifacts),
            strategy_version=_optional_text(
                value, "strategyVersion", "strategy_version"
            ),
            experiment_id=_optional_text(value, "experimentId", "experiment_id"),
            workflow_execution_id=_optional_text(
                value, "workflowExecutionId", "workflow_execution_id"
            ),
        )


def _optional_text(value: Mapping[str, object], *names: str) -> str | None:
    for name in names:
        candidate = value.get(name)
        if candidate is not None:
            return str(candidate)
    return None


@dataclass(frozen=True, slots=True)
class ImportedArtifactReference:
    """An immutable workspace artifact link with complete execution lineage."""

    name: str
    uri: str
    sha256: str
    size_bytes: int
    media_type: str
    strategy_version: str
    experiment_id: str
    workflow_execution_id: str
    harness_manifest_id: str
    harness_manifest_sha256: str
    git_commit: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _artifact_name(self.name))
        object.__setattr__(self, "uri", _relative_path(self.uri).as_posix())
        _digest(self.sha256, "artifact sha256")
        if isinstance(self.size_bytes, bool) or self.size_bytes < 0:
            raise ArtifactProvenanceError("artifact size_bytes must be non-negative")
        if not self.media_type:
            raise ArtifactProvenanceError("artifact media_type must be non-empty")
        object.__setattr__(
            self,
            "strategy_version",
            _version(self.strategy_version, "strategy_version"),
        )
        object.__setattr__(
            self, "experiment_id", _identifier(self.experiment_id, "experiment_id")
        )
        object.__setattr__(
            self,
            "workflow_execution_id",
            _identifier(self.workflow_execution_id, "workflow_execution_id"),
        )
        object.__setattr__(
            self,
            "harness_manifest_id",
            _identifier(self.harness_manifest_id, "harness_manifest_id"),
        )
        _digest(self.harness_manifest_sha256, "harness manifest sha256")
        if (
            not isinstance(self.git_commit, str)
            or len(self.git_commit) not in {40, 64}
            or any(character not in _DIGEST for character in self.git_commit)
        ):
            raise ArtifactProvenanceError("git_commit must be a Git object ID")

    @property
    def artifact_id(self) -> str:
        """Return the content address of the imported artifact."""
        return f"sha256:{self.sha256}"

    @property
    def harness_manifest_reference(self) -> str:
        """Return the stable manifest ID and digest link."""
        return f"{self.harness_manifest_id}@sha256:{self.harness_manifest_sha256}"

    def as_dict(self) -> dict[str, object]:
        """Return the provenance reference representation."""
        return {
            "artifactId": self.artifact_id,
            "gitCommit": self.git_commit,
            "harnessManifest": {
                "id": self.harness_manifest_id,
                "sha256": self.harness_manifest_sha256,
            },
            "mediaType": self.media_type,
            "name": self.name,
            "sha256": self.sha256,
            "sizeBytes": self.size_bytes,
            "strategyVersion": self.strategy_version,
            "uri": self.uri,
            "workflowExecutionId": self.workflow_execution_id,
            "experimentId": self.experiment_id,
        }


@dataclass(frozen=True, slots=True)
class ArtifactImportReceipt:
    """Result of one atomic artifact import publication."""

    git_commit: str
    harness_manifest_id: str
    harness_manifest_sha256: str
    strategy_version: str
    experiment_id: str
    workflow_execution_id: str
    artifacts: tuple[ImportedArtifactReference, ...]
    provenance_uri: str

    @property
    def references(self) -> tuple[ImportedArtifactReference, ...]:
        """Return imported artifact references."""
        return self.artifacts


class Filesystem(Protocol):
    """Injected byte filesystem boundary used by the importer."""

    def exists(self, path: Path) -> bool:
        """Return whether a regular file exists."""

    def read_bytes(self, path: Path) -> bytes:
        """Read bytes from a local artifact or workspace path."""

    def mkdir(self, path: Path) -> None:
        """Create a workspace directory and parents."""

    def write_bytes(self, path: Path, content: bytes) -> None:
        """Write one immutable workspace file."""


class GitBoundary(Protocol):
    """Injected local Git publication boundary."""

    def head(self) -> str | None:
        """Return the current local repository commit."""

    def commit(
        self,
        paths: Sequence[str],
        message: str,
        *,
        expected_head: str | None,
    ) -> str:
        """Commit only the supplied paths under an expected-head fence."""


class LocalFilesystem:
    """Default local filesystem adapter; it has no network behavior."""

    def exists(self, path: Path) -> bool:
        return path.is_file()

    def read_bytes(self, path: Path) -> bytes:
        return path.read_bytes()

    def mkdir(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)

    def write_bytes(self, path: Path, content: bytes) -> None:
        path.write_bytes(content)


class NativeGitBoundary:
    """Native Git adapter that publishes selected paths without network calls."""

    def __init__(self, repository: str | Path) -> None:
        self.repository = Path(repository).resolve()

    def head(self) -> str | None:
        result = self._run("rev-parse", "--verify", "HEAD", check=False)
        return result.stdout.strip() if result.returncode == 0 else None

    def commit(
        self,
        paths: Sequence[str],
        message: str,
        *,
        expected_head: str | None,
    ) -> str:
        actual = self.head()
        if actual != expected_head:
            raise ArtifactImportError(
                f"Git HEAD moved from {expected_head or '<unborn>'} "
                f"to {actual or '<unborn>'}"
            )
        relative_paths = tuple(_relative_path(path).as_posix() for path in paths)
        with self._temporary_index() as environment:
            if expected_head is None:
                self._run("read-tree", "--empty", environment=environment)
            else:
                self._run("read-tree", expected_head, environment=environment)
            self._run("add", "--", *relative_paths, environment=environment)
            tree = self._run("write-tree", environment=environment).stdout.strip()
            arguments = ["commit-tree", tree]
            if expected_head is not None:
                arguments.extend(("-p", expected_head))
            arguments.extend(("-m", message))
            commit = self._run(*arguments).stdout.strip()
            old = expected_head or self._zero_object_id()
            advanced = self._run("update-ref", "HEAD", commit, old, check=False)
            if advanced.returncode != 0:
                raise ArtifactImportError("Git HEAD fence was rejected")
        self._run("add", "--", *relative_paths)
        return commit

    def _zero_object_id(self) -> str:
        object_format = self._run("rev-parse", "--show-object-format").stdout.strip()
        return "0" * {"sha1": 40, "sha256": 64}[object_format]

    def _run(
        self,
        *arguments: str,
        check: bool = True,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                ("git", *arguments),
                cwd=self.repository,
                capture_output=True,
                check=False,
                encoding="utf-8",
                stdin=subprocess.DEVNULL,
                env=environment,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ArtifactImportError(f"unable to run Git: {error}") from error
        if check and result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise ArtifactImportError(f"Git command failed: {detail}")
        return result

    @contextmanager
    def _temporary_index(self):
        descriptor, name = tempfile.mkstemp(
            prefix="thesis-artifact-index-", dir=self.repository / ".git"
        )
        os.close(descriptor)
        path = Path(name)
        path.unlink()
        environment = os.environ.copy()
        environment["GIT_INDEX_FILE"] = str(path)
        try:
            yield environment
        finally:
            path.unlink(missing_ok=True)


class ArtifactImporter:
    """Import manifest-declared harness artifacts into a workspace repository."""

    def __init__(
        self,
        workspace: str | Path,
        *,
        filesystem: Filesystem | None = None,
        git: GitBoundary | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.filesystem = LocalFilesystem() if filesystem is None else filesystem
        self.git = NativeGitBoundary(self.workspace) if git is None else git

    def import_manifest(
        self,
        manifest: HarnessManifest,
        *,
        strategy_version: str,
        experiment_id: str,
        workflow_execution_id: str,
        source_root: str | Path | None = None,
    ) -> ArtifactImportReceipt:
        """Verify and publish all manifest artifacts with complete provenance."""
        if not isinstance(manifest, HarnessManifest):
            raise TypeError("manifest must be a HarnessManifest")
        strategy_version = _version(strategy_version, "strategy_version")
        experiment_id = _identifier(experiment_id, "experiment_id")
        workflow_execution_id = _identifier(
            workflow_execution_id, "workflow_execution_id"
        )
        for name, supplied, declared in (
            ("strategy_version", strategy_version, manifest.strategy_version),
            ("experiment_id", experiment_id, manifest.experiment_id),
            (
                "workflow_execution_id",
                workflow_execution_id,
                manifest.workflow_execution_id,
            ),
        ):
            if declared is not None and declared != supplied:
                raise ArtifactProvenanceError(
                    f"manifest {name} does not match import provenance"
                )
        root = (self.workspace if source_root is None else Path(source_root)).resolve(
            strict=False
        )
        verified: list[tuple[HarnessArtifact, bytes]] = []
        for artifact in manifest.artifacts:
            if not artifact.reproducibility_critical:
                continue
            payload = self._read_source(root, artifact.uri)
            actual_hash = hashlib.sha256(payload).hexdigest()
            if len(payload) != artifact.size_bytes or actual_hash != artifact.sha256:
                raise ArtifactHashMismatchError(
                    f"artifact {artifact.name} does not match manifest hash or size"
                )
            verified.append((artifact, payload))
        if not verified:
            raise ArtifactImportError(
                "manifest has no reproducibility-critical artifacts to import"
            )

        artifact_root = PurePosixPath("backtests", experiment_id, "artifacts")
        artifact_locations: list[tuple[HarnessArtifact, str]] = []
        writes: list[tuple[str, Path, bytes]] = []
        for artifact, payload in verified:
            relative = artifact_root / artifact.sha256 / artifact.name
            relative_text = relative.as_posix()
            destination = self._workspace_path(relative_text)
            self._write_immutable(destination, payload, relative_text)
            writes.append((relative_text, destination, payload))
            artifact_locations.append((artifact, relative_text))
        expected_head = self.git.head()
        artifact_commit = self.git.commit(
            tuple(relative for relative, _, _ in writes),
            f"thesis: import backtest artifacts {manifest.manifest_id}",
            expected_head=expected_head,
        )
        artifact_references = tuple(
            ImportedArtifactReference(
                name=artifact.name,
                uri=relative,
                sha256=artifact.sha256,
                size_bytes=artifact.size_bytes,
                media_type=artifact.media_type,
                strategy_version=strategy_version,
                experiment_id=experiment_id,
                workflow_execution_id=workflow_execution_id,
                harness_manifest_id=manifest.manifest_id,
                harness_manifest_sha256=manifest.sha256,
                git_commit=artifact_commit,
            )
            for artifact, relative in artifact_locations
        )
        provenance_relative = PurePosixPath(
            "backtests",
            experiment_id,
            "imports",
            f"{workflow_execution_id}-{manifest.sha256}.json",
        ).as_posix()
        provenance_path = self._workspace_path(provenance_relative)
        provenance = {
            "artifacts": [reference.as_dict() for reference in artifact_references],
            "gitCommit": artifact_commit,
            "harnessManifest": {
                "id": manifest.manifest_id,
                "sha256": manifest.sha256,
            },
            "schema": "fca-thesis-artifact-import-v1",
            "experimentId": experiment_id,
            "strategyVersion": strategy_version,
            "workflowExecutionId": workflow_execution_id,
        }
        provenance_bytes = canonical_configuration_bytes(provenance) + b"\n"
        self._write_immutable(provenance_path, provenance_bytes, provenance_relative)
        provenance_commit = self.git.commit(
            (provenance_relative,),
            f"thesis: record artifact provenance {manifest.manifest_id}",
            expected_head=artifact_commit,
        )
        finalized = tuple(
            _replace_git_commit(reference, provenance_commit)
            for reference in artifact_references
        )
        return ArtifactImportReceipt(
            git_commit=provenance_commit,
            harness_manifest_id=manifest.manifest_id,
            harness_manifest_sha256=manifest.sha256,
            strategy_version=strategy_version,
            experiment_id=experiment_id,
            workflow_execution_id=workflow_execution_id,
            artifacts=finalized,
            provenance_uri=provenance_relative,
        )

    def import_artifacts(
        self,
        manifest: HarnessManifest,
        *,
        strategy_version: str,
        experiment_id: str,
        workflow_execution_id: str,
        source_root: str | Path | None = None,
    ) -> ArtifactImportReceipt:
        """Alias emphasizing that the manifest controls the imported artifact set."""
        return self.import_manifest(
            manifest,
            strategy_version=strategy_version,
            experiment_id=experiment_id,
            workflow_execution_id=workflow_execution_id,
            source_root=source_root,
        )

    def _read_source(self, source_root: Path, uri: str) -> bytes:
        split = urlsplit(uri)
        if split.scheme or "://" in uri or "\\" in uri:
            raise ArtifactImportError("artifact imports do not support network URIs")
        source = Path(uri)
        if not source.is_absolute() and any(
            part in {"", ".", ".."} for part in uri.split("/")
        ):
            raise ArtifactImportError("artifact source paths must be confined")
        source = (
            source.resolve(strict=False)
            if source.is_absolute()
            else (source_root / PurePosixPath(uri)).resolve(strict=False)
        )
        if not source.is_relative_to(source_root):
            raise ArtifactImportError("artifact source path escapes its result root")
        try:
            return self.filesystem.read_bytes(source)
        except (KeyError, OSError, ValueError) as error:
            raise ArtifactImportError(
                f"unable to read harness artifact {uri!r}"
            ) from error

    def _workspace_path(self, relative: str) -> Path:
        _relative_path(relative)
        path = self.workspace.joinpath(*PurePosixPath(relative).parts)
        try:
            resolved = path.resolve(strict=False)
        except OSError as error:
            raise ArtifactImportError("workspace path cannot be resolved") from error
        if not resolved.is_relative_to(self.workspace):
            raise ArtifactImportError("artifact path escapes workspace")
        return resolved

    def _write_immutable(self, path: Path, content: bytes, relative: str) -> None:
        if self.filesystem.exists(path):
            try:
                existing = self.filesystem.read_bytes(path)
            except OSError as error:
                raise ArtifactImportError(
                    f"unable to read existing {relative}"
                ) from error
            if existing != content:
                raise ArtifactImportError(
                    f"immutable workspace path already differs: {relative}"
                )
            return
        self.filesystem.mkdir(path.parent)
        self.filesystem.write_bytes(path, content)


def _replace_git_commit(
    reference: ImportedArtifactReference, commit: str
) -> ImportedArtifactReference:
    return ImportedArtifactReference(
        name=reference.name,
        uri=reference.uri,
        sha256=reference.sha256,
        size_bytes=reference.size_bytes,
        media_type=reference.media_type,
        strategy_version=reference.strategy_version,
        experiment_id=reference.experiment_id,
        workflow_execution_id=reference.workflow_execution_id,
        harness_manifest_id=reference.harness_manifest_id,
        harness_manifest_sha256=reference.harness_manifest_sha256,
        git_commit=commit,
    )


ArtifactManifest = HarnessManifest
HarnessArtifactReference = HarnessArtifact
ArtifactReference = ImportedArtifactReference


__all__ = [
    "ArtifactHashMismatchError",
    "ArtifactImportError",
    "ArtifactImportReceipt",
    "ArtifactImporter",
    "ArtifactManifest",
    "ArtifactProvenanceError",
    "ArtifactReference",
    "Filesystem",
    "GitBoundary",
    "HarnessArtifact",
    "HarnessArtifactReference",
    "HarnessManifest",
    "ImportedArtifactReference",
    "LocalFilesystem",
    "NativeGitBoundary",
]
