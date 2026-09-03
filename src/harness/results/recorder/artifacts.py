"""Path construction and read access for recorded run artifacts."""

from __future__ import annotations

from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from harness.results.recorder.schemas import ARTIFACT_FILES, RECORDER_SCHEMAS, Artifact


def artifact_path(directory: str | Path, artifact: Artifact | str) -> Path:
    """Return the Parquet path for one artifact inside a run directory."""
    return Path(directory) / ARTIFACT_FILES[Artifact(artifact)]


def read_artifact(path: str | Path, artifact: Artifact | str) -> pa.Table:
    """Read one artifact, rejecting a schema that drifted from this recorder."""
    schema = RECORDER_SCHEMAS[Artifact(artifact)]
    table = pq.read_table(Path(path))
    if not table.schema.equals(schema, check_metadata=True):
        raise ValueError(
            f"{Artifact(artifact).value} schema does not match the recorder schema"
        )
    return table


def read_rows(
    path: str | Path, artifact: Artifact | str
) -> tuple[dict[str, object], ...]:
    """Read one artifact as immutable row mappings in recorded order."""
    table = read_artifact(path, artifact)
    return tuple(dict(row) for row in table.to_pylist())
