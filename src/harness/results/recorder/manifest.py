"""Run manifest and per-artifact digest helpers."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from harness.results.recorder.artifacts import artifact_path
from harness.results.recorder.schemas import Artifact


@dataclass(frozen=True, slots=True)
class RunManifest:
    """Immutable summary of one closed recorded run."""

    run_id: str
    directory: Path
    row_counts: Mapping[str, int]
    digests: Mapping[str, str]
    run_digest: str
    metadata: Mapping[str, str]

    @property
    def artifacts(self) -> tuple[str, ...]:
        """Return recorded artifact names in stable recorder order."""
        return tuple(artifact.value for artifact in Artifact)

    @property
    def total_rows(self) -> int:
        """Return the total row count across every artifact."""
        return sum(self.row_counts.values())

    def path(self, artifact: Artifact | str) -> Path:
        """Return the Parquet path for one recorded artifact."""
        return artifact_path(self.directory, artifact)


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _seed_text(seed: str | int | bytes) -> str:
    if isinstance(seed, bytes):
        return seed.hex()
    return str(seed)


def _truth_text(value: bool) -> str:
    return "true" if value else "false"
