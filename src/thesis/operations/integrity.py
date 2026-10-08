"""All-in-Git repository integrity: size budgets, backup, restore, retention.

The architecture keeps every durable workspace artifact in one Git repository,
so repository growth, backup, restore, and retention are first-class operations.
Measurement and backup are deterministic and local: they never touch the network,
never mutate the source repository, and never delete or externalize existing
evidence. Budget enforcement checks both the total repository ceiling and the
per-artifact ceiling from :class:`~thesis.workspaces.manifest.WorkspaceLimits`;
retention planning keeps protected commits regardless of age.
"""

from __future__ import annotations

import hashlib
import io
import os
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Protocol

from thesis.workspaces.manifest import WorkspaceLimits


class IntegrityLimitError(RuntimeError):
    """Base class for repository integrity limit failures."""


class RepositoryBudgetExceeded(IntegrityLimitError):
    """A measurement exceeded the repository or artifact byte budget."""


class BackupIntegrityError(IntegrityLimitError):
    """A backup or restore failed its integrity check."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class RepositoryMeasurement:
    """A deterministic byte accounting of one repository tree."""

    root: str
    total_bytes: int
    artifact_bytes: int
    file_count: int

    def __post_init__(self) -> None:
        for name in ("total_bytes", "artifact_bytes", "file_count"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise IntegrityLimitError(f"{name} must be a non-negative integer")
        if self.artifact_bytes > self.total_bytes:
            raise IntegrityLimitError("artifact bytes cannot exceed total bytes")

    def as_dict(self) -> dict[str, object]:
        """Return the audit representation of this measurement."""
        return {
            "artifactBytes": self.artifact_bytes,
            "fileCount": self.file_count,
            "root": self.root,
            "totalBytes": self.total_bytes,
        }


@dataclass(frozen=True, slots=True)
class RepositoryBudget:
    """The hard repository and artifact byte ceilings for one workspace."""

    max_repository_bytes: int
    max_artifact_bytes: int

    def __post_init__(self) -> None:
        if self.max_repository_bytes <= 0 or self.max_artifact_bytes <= 0:
            raise IntegrityLimitError("repository budgets must be positive")
        if self.max_artifact_bytes > self.max_repository_bytes:
            raise IntegrityLimitError(
                "artifact budget must not exceed the repository budget"
            )

    @classmethod
    def from_limits(cls, limits: WorkspaceLimits) -> RepositoryBudget:
        """Build a budget from a validated workspace manifest's limits."""
        return cls(
            max_repository_bytes=limits.max_repository_bytes,
            max_artifact_bytes=limits.max_artifact_bytes,
        )

    def as_dict(self) -> dict[str, object]:
        """Return the audit representation of this budget."""
        return {
            "maxArtifactBytes": self.max_artifact_bytes,
            "maxRepositoryBytes": self.max_repository_bytes,
        }


@dataclass(frozen=True, slots=True)
class IntegrityReport:
    """The outcome of enforcing a repository budget against a measurement."""

    measurement: RepositoryMeasurement
    budget: RepositoryBudget
    within_budget: bool
    repository_fraction: float
    artifact_fraction: float
    bottleneck: str | None = None

    def as_dict(self) -> dict[str, object]:
        """Return the audit representation of this report."""
        return {
            "artifactFraction": self.artifact_fraction,
            "bottleneck": self.bottleneck,
            "budget": self.budget.as_dict(),
            "measurement": self.measurement.as_dict(),
            "repositoryFraction": self.repository_fraction,
            "withinBudget": self.within_budget,
        }


@dataclass(frozen=True, slots=True)
class BackupReceipt:
    """Content identity of one deterministic repository backup archive."""

    backup_path: str
    sha256: str
    size_bytes: int
    file_count: int
    created_at: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.sha256, str)
            or len(self.sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.sha256)
        ):
            raise BackupIntegrityError("backup sha256 must be a lowercase digest")

    def as_dict(self) -> dict[str, object]:
        """Return the audit representation of this backup receipt."""
        return {
            "backupPath": self.backup_path,
            "createdAt": self.created_at,
            "fileCount": self.file_count,
            "sha256": self.sha256,
            "sizeBytes": self.size_bytes,
        }


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    """Retention window plus commits that must never be pruned."""

    max_versions: int
    protected: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.max_versions, bool) or self.max_versions < 1:
            raise IntegrityLimitError("max_versions must be a positive integer")
        object.__setattr__(self, "protected", tuple(dict.fromkeys(self.protected)))


@dataclass(frozen=True, slots=True)
class RetentionPlan:
    """The deterministic keep/prune split for a commit history."""

    keep: tuple[str, ...]
    prune: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        """Return the audit representation of this retention plan."""
        return {"keep": list(self.keep), "prune": list(self.prune)}


class RepositorySizePort(Protocol):
    """Injectable repository measurement boundary."""

    def measure(self, root: Path) -> RepositoryMeasurement: ...


#: Workspace subtrees counted against the artifact byte budget.
ARTIFACT_ROOTS: tuple[str, ...] = ("jobs", "uploads", "activations")


class FilesystemRepositorySize:
    """Default deterministic local measurement over a repository tree."""

    def __init__(self, artifact_roots: Sequence[str] = ARTIFACT_ROOTS) -> None:
        self._artifact_roots = tuple(artifact_roots)

    def measure(self, root: Path) -> RepositoryMeasurement:
        root = Path(root)
        if not root.is_dir():
            raise IntegrityLimitError(f"repository path is not a directory: {root}")
        total_bytes = 0
        artifact_bytes = 0
        file_count = 0
        for current, directories, files in os.walk(root):
            directories.sort()
            files.sort()
            current_path = Path(current)
            for name in files:
                path = current_path / name
                if path.is_symlink():
                    continue
                try:
                    size = path.stat().st_size
                except OSError as error:
                    raise IntegrityLimitError(
                        f"unable to measure repository file: {path}"
                    ) from error
                total_bytes += size
                file_count += 1
                relative = path.relative_to(root)
                if relative.parts and relative.parts[0] in self._artifact_roots:
                    artifact_bytes += size
        return RepositoryMeasurement(
            root=str(root),
            total_bytes=total_bytes,
            artifact_bytes=artifact_bytes,
            file_count=file_count,
        )


def measure_repository(
    path: str | Path, *, size_port: RepositorySizePort | None = None
) -> RepositoryMeasurement:
    """Measure a repository tree through the injected size port."""
    port = FilesystemRepositorySize() if size_port is None else size_port
    return port.measure(Path(path))


def enforce_budget(
    measurement: RepositoryMeasurement, budget: RepositoryBudget
) -> IntegrityReport:
    """Enforce both byte ceilings, failing closed with the breach named."""
    repository_fraction = measurement.total_bytes / budget.max_repository_bytes
    artifact_fraction = measurement.artifact_bytes / budget.max_artifact_bytes
    breaches: list[str] = []
    if measurement.total_bytes > budget.max_repository_bytes:
        breaches.append("repository")
    if measurement.artifact_bytes > budget.max_artifact_bytes:
        breaches.append("artifact")
    if breaches:
        raise RepositoryBudgetExceeded(
            "repository budget exceeded for "
            f"{', '.join(breaches)}: repository={measurement.total_bytes}/"
            f"{budget.max_repository_bytes} bytes, artifact="
            f"{measurement.artifact_bytes}/{budget.max_artifact_bytes} bytes"
        )
    bottleneck = (
        "repository" if repository_fraction >= artifact_fraction else "artifact"
    )
    return IntegrityReport(
        measurement=measurement,
        budget=budget,
        within_budget=True,
        repository_fraction=repository_fraction,
        artifact_fraction=artifact_fraction,
        bottleneck=bottleneck,
    )


def plan_retention(commits: Sequence[str], policy: RetentionPolicy) -> RetentionPlan:
    """Keep the newest window plus every protected commit; never prune those."""
    ordered = tuple(commits)
    if len(set(ordered)) != len(ordered):
        raise IntegrityLimitError("commit history must not contain duplicates")
    protected = set(policy.protected)
    keep: list[str] = []
    for index, commit in enumerate(ordered):
        if index < policy.max_versions or commit in protected:
            keep.append(commit)
    kept = set(keep)
    prune = tuple(commit for commit in ordered if commit not in kept)
    return RetentionPlan(keep=tuple(keep), prune=prune)


def _normalized_zip_info(info: zipfile.ZipInfo) -> zipfile.ZipInfo:
    info.date_time = (1980, 1, 1, 0, 0, 0)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 0
    info.external_attr = 0o100644 << 16
    return info


def create_backup(
    source: str | Path,
    destination: str | Path,
    *,
    clock: Callable[[], datetime] = _utc_now,
) -> BackupReceipt:
    """Write a deterministic, content-addressed archive of a repository tree."""
    source_path = Path(source)
    if not source_path.is_dir():
        raise BackupIntegrityError(f"backup source is not a directory: {source_path}")
    destination_path = Path(destination)
    entries = sorted(
        path
        for path in source_path.rglob("*")
        if path.is_file() and not path.is_symlink()
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in entries:
            relative = path.relative_to(source_path).as_posix()
            info = _normalized_zip_info(zipfile.ZipInfo(relative))
            archive.writestr(info, path.read_bytes())
    archive_bytes = buffer.getvalue()
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    destination_path.write_bytes(archive_bytes)
    return BackupReceipt(
        backup_path=str(destination_path),
        sha256=hashlib.sha256(archive_bytes).hexdigest(),
        size_bytes=len(archive_bytes),
        file_count=len(entries),
        created_at=clock().isoformat(),
    )


def restore_backup(
    receipt: BackupReceipt, destination: str | Path
) -> RepositoryMeasurement:
    """Verify a backup receipt, then extract it into a fresh destination tree."""
    archive_path = Path(receipt.backup_path)
    if not archive_path.is_file():
        raise BackupIntegrityError(f"backup archive is missing: {archive_path}")
    archive_bytes = archive_path.read_bytes()
    if (
        len(archive_bytes) != receipt.size_bytes
        or hashlib.sha256(archive_bytes).hexdigest() != receipt.sha256
    ):
        raise BackupIntegrityError("backup archive does not match its receipt")
    destination_path = Path(destination)
    destination_path.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            names = archive.namelist()
            if len(names) != receipt.file_count:
                raise BackupIntegrityError("backup member count does not match receipt")
            for name in names:
                member = PurePosixPath(name)
                if not name or member.is_absolute() or ".." in member.parts:
                    raise BackupIntegrityError("backup contains an unsafe member path")
                target = destination_path.joinpath(*member.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(name))
    except zipfile.BadZipFile as error:
        raise BackupIntegrityError("backup archive is not a valid zip") from error
    return measure_repository(destination_path)


__all__ = [
    "ARTIFACT_ROOTS",
    "BackupIntegrityError",
    "BackupReceipt",
    "FilesystemRepositorySize",
    "IntegrityLimitError",
    "IntegrityReport",
    "RepositoryBudget",
    "RepositoryBudgetExceeded",
    "RepositoryMeasurement",
    "RepositorySizePort",
    "RetentionPlan",
    "RetentionPolicy",
    "create_backup",
    "enforce_budget",
    "measure_repository",
    "plan_retention",
    "restore_backup",
]
