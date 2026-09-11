"""Canonical artifact and manifest support for the determinism oracles.

This module is the engine-free slice of the former determinism harness: it
turns structured output and event collections into canonical byte payloads and
builds reproducible manifests over them. The Rust and indicator oracles compare
their native output against these canonical bytes, so the encoding is the
shared contract rather than an engine concern.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum

from harness.configuration.hashing import (
    canonical_configuration_bytes,
    configuration_hash,
)
from harness.core.types import Event, ParquetCodec


def _normalized(value: object, path: str = "value") -> object:
    """Convert records to configuration-shaped values without hiding order."""
    if value is None or isinstance(value, str | bool | int | Decimal | datetime):
        return value
    if isinstance(value, float):
        raise TypeError(f"{path} must not contain binary floating point")
    if isinstance(value, Enum):
        return _normalized(value.value, path)
    if isinstance(value, Mapping):
        return {key: _normalized(item, f"{path}.{key}") for key, item in value.items()}
    if isinstance(value, set | frozenset):
        raise TypeError(f"{path} contains an unordered set")
    if isinstance(value, bytes | bytearray):
        raise TypeError(f"{path} contains binary data; provide bytes as an artifact")
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _normalized(getattr(value, field.name), f"{path}.{field.name}")
            for field in fields(value)
        }
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [
            _normalized(item, f"{path}[{index}]") for index, item in enumerate(value)
        ]
    raise TypeError(f"{path} contains unsupported {type(value).__name__}")


def _class_name(value: object) -> str:
    value_type = value if isinstance(value, type) else type(value)
    return f"{value_type.__module__}.{value_type.__qualname__}"


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _validate_artifact_name(name: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("artifact name must be a non-empty string")
    normalized = name.strip()
    if (
        normalized.startswith("/")
        or "\\" in normalized
        or any(part in {"", ".", ".."} for part in normalized.split("/"))
    ):
        raise ValueError("artifact name must be a relative slash-separated path")
    return normalized


@dataclass(frozen=True, slots=True)
class DeterminismArtifact:
    """One named canonical byte payload and its content digest."""

    name: str
    payload: bytes

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _validate_artifact_name(self.name))
        if not isinstance(self.payload, bytes):
            raise TypeError("artifact payload must be bytes")

    @property
    def sha256(self) -> str:
        return _sha256(self.payload)

    @property
    def byte_length(self) -> int:
        return len(self.payload)


@dataclass(frozen=True, slots=True)
class ArtifactDigest:
    """Portable manifest entry for one artifact."""

    name: str
    sha256: str
    byte_length: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _validate_artifact_name(self.name))
        if len(self.sha256) != 64 or any(
            character not in "0123456789abcdef" for character in self.sha256
        ):
            raise ValueError("sha256 must be a lowercase SHA-256 digest")
        if isinstance(self.byte_length, bool) or self.byte_length < 0:
            raise ValueError("byte_length must be non-negative")


class ArtifactCanonicalizer:
    """Canonicalize structured test output and homogeneous event collections."""

    @staticmethod
    def json_bytes(value: object) -> bytes:
        """Encode configuration-shaped data as compact canonical UTF-8 JSON."""
        return canonical_configuration_bytes(_normalized(value))

    @staticmethod
    def event_bytes(events: Iterable[Event]) -> bytes:
        """Encode one homogeneous event collection using the Parquet codec."""
        values = tuple(events)
        if not values:
            raise ValueError("event_bytes requires at least one event")
        if not all(isinstance(event, Event) for event in values):
            raise TypeError("events must contain Event values")
        record_type = type(values[0])
        if any(type(event) is not record_type for event in values):
            raise TypeError("event_bytes requires one exact event type")
        return ParquetCodec.encode(values, record_type)

    @classmethod
    def event_artifacts(cls, prefix: str, events: Iterable[Event]) -> dict[str, bytes]:
        """Return stable per-event-type Parquet artifacts for an event stream."""
        normalized_prefix = _validate_artifact_name(prefix).rstrip("/")
        values = tuple(events)
        groups: dict[type[Event], list[Event]] = {}
        for event in values:
            if not isinstance(event, Event):
                raise TypeError("events must contain Event values")
            groups.setdefault(type(event), []).append(event)
        if not groups:
            return {f"{normalized_prefix}/empty.json": cls.json_bytes({"events": []})}
        return {
            f"{normalized_prefix}/{_class_name(record_type)}.parquet": cls.event_bytes(
                records
            )
            for record_type, records in sorted(
                groups.items(), key=lambda item: _class_name(item[0])
            )
        }

    @classmethod
    def artifacts(cls, values: Mapping[str, object]) -> tuple[DeterminismArtifact, ...]:
        """Canonicalize named bytes, event streams, or structured values."""
        artifacts: list[DeterminismArtifact] = []
        for name, value in values.items():
            normalized_name = _validate_artifact_name(name)
            if isinstance(value, bytes):
                payload = value
            elif isinstance(value, bytearray):
                payload = bytes(value)
            elif isinstance(value, Event):
                payload = cls.event_bytes((value,))
            else:
                payload = cls.json_bytes(value)
            artifacts.append(DeterminismArtifact(normalized_name, payload))
        return tuple(sorted(artifacts, key=lambda artifact: artifact.name))

    @staticmethod
    def manifest(
        configuration: object, artifacts: Iterable[DeterminismArtifact]
    ) -> DeterminismManifest:
        """Build a manifest from canonical artifact bytes and config inputs."""
        values = tuple(artifacts)
        if not all(isinstance(artifact, DeterminismArtifact) for artifact in values):
            raise TypeError("artifacts must contain DeterminismArtifact values")
        return DeterminismManifest(
            configuration_hash=configuration_hash(_normalized(configuration)),
            artifacts=tuple(
                ArtifactDigest(artifact.name, artifact.sha256, artifact.byte_length)
                for artifact in sorted(values, key=lambda item: item.name)
            ),
        )


@dataclass(frozen=True, slots=True)
class DeterminismManifest:
    """Canonical manifest containing configuration and artifact digests."""

    configuration_hash: str
    artifacts: tuple[ArtifactDigest, ...] | Sequence[ArtifactDigest]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.configuration_hash, str)
            or len(self.configuration_hash) != 64
            or any(
                character not in "0123456789abcdef"
                for character in self.configuration_hash
            )
        ):
            raise ValueError("configuration_hash must be a lowercase SHA-256 digest")
        artifacts = tuple(self.artifacts)
        if not all(isinstance(artifact, ArtifactDigest) for artifact in artifacts):
            raise TypeError("artifacts must contain ArtifactDigest values")
        if tuple(sorted(artifact.name for artifact in artifacts)) != tuple(
            artifact.name for artifact in artifacts
        ):
            raise ValueError("manifest artifacts must be sorted by name")
        if len({artifact.name for artifact in artifacts}) != len(artifacts):
            raise ValueError("manifest artifact names must be unique")
        object.__setattr__(self, "artifacts", artifacts)

    @property
    def canonical_bytes(self) -> bytes:
        return canonical_configuration_bytes(
            {
                "schema": "fca-determinism-manifest-v1",
                "configuration_hash": self.configuration_hash,
                "artifacts": [
                    {
                        "name": artifact.name,
                        "sha256": artifact.sha256,
                        "byte_length": artifact.byte_length,
                    }
                    for artifact in self.artifacts
                ],
            }
        )

    @property
    def sha256(self) -> str:
        return _sha256(self.canonical_bytes)

    @property
    def artifact_hashes(self) -> tuple[tuple[str, str], ...]:
        return tuple((artifact.name, artifact.sha256) for artifact in self.artifacts)


class DeterminismError(AssertionError):
    """Raised when two determinism runs do not have identical canonical output."""


@dataclass(frozen=True, slots=True)
class ManifestComparison:
    """Detailed result of comparing two manifests."""

    differences: tuple[str, ...]

    @property
    def equal(self) -> bool:
        return not self.differences

    @property
    def matches(self) -> bool:
        return self.equal


def compare_manifests(
    left: DeterminismManifest,
    right: DeterminismManifest,
) -> ManifestComparison:
    """Compare config and artifact hashes without relying on filesystem paths."""
    differences: list[str] = []
    if left.configuration_hash != right.configuration_hash:
        differences.append("configuration_hash")
    left_artifacts = {artifact.name: artifact for artifact in left.artifacts}
    right_artifacts = {artifact.name: artifact for artifact in right.artifacts}
    for name in sorted(set(left_artifacts) | set(right_artifacts)):
        left_artifact = left_artifacts.get(name)
        right_artifact = right_artifacts.get(name)
        if left_artifact is None:
            differences.append(f"artifact:{name}:missing-left")
        elif right_artifact is None:
            differences.append(f"artifact:{name}:missing-right")
        elif left_artifact.sha256 != right_artifact.sha256:
            differences.append(f"artifact:{name}:sha256")
        elif left_artifact.byte_length != right_artifact.byte_length:
            differences.append(f"artifact:{name}:byte_length")
    return ManifestComparison(tuple(differences))


def assert_manifest_equal(
    left: DeterminismManifest,
    right: DeterminismManifest,
) -> None:
    """Raise ``DeterminismError`` when manifest hashes or entries differ."""
    comparison = compare_manifests(left, right)
    if not comparison.equal:
        raise DeterminismError(
            "determinism manifests differ: " + ", ".join(comparison.differences)
        )


__all__ = [
    "ArtifactCanonicalizer",
    "ArtifactDigest",
    "DeterminismArtifact",
    "DeterminismError",
    "DeterminismManifest",
    "ManifestComparison",
    "assert_manifest_equal",
    "compare_manifests",
]
