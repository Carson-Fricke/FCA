"""Immutable, content-addressed authored-program bundles."""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, cast

_HASH_DOMAIN = b"fca:thesis-strategy-bundle:v1\0"


def _canonical_json_bytes(payload: Mapping[str, object]) -> bytes:
    """Return canonical JSON bytes for a configuration mapping."""
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]{0,127}$")
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.-]+)?$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class StrategyBundleError(ValueError):
    """Raised when a strategy bundle cannot be made deterministic and safe."""


def _identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise StrategyBundleError(f"{label} must be a safe identifier")
    return value


def _version(value: str) -> str:
    if not isinstance(value, str) or _VERSION.fullmatch(value) is None:
        raise StrategyBundleError("strategy version must be semantic version text")
    return value


def _relative_name(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise StrategyBundleError(f"{label} must be a string")
    normalized = value.replace("\\", "/")
    parts = normalized.split("/")
    if (
        not normalized
        or normalized.startswith("/")
        or any(part in {"", ".", ".."} for part in parts)
        or ":" in parts[0]
    ):
        raise StrategyBundleError(f"{label} must be a confined relative path")
    return "/".join(parts)


def _freeze(value: object) -> object:
    """Freeze configuration-shaped values before retaining them in a bundle."""
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise StrategyBundleError("bundle mapping keys must be strings")
        return MappingProxyType({key: _freeze(value[key]) for key in sorted(value)})
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set | frozenset):
        return tuple(sorted((_freeze(item) for item in value), key=repr))
    if isinstance(value, bytes | bytearray):
        return {"$bytes": bytes(value).hex()}
    return value


def _canonical_value(value: object) -> Any:
    """Convert frozen values to the JSON-shaped values used by the hash."""
    if isinstance(value, Mapping):
        return {str(key): _canonical_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_canonical_value(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class StrategyFile:
    """One source or test file retained by an immutable strategy bundle."""

    path: str
    content: bytes

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "path", _relative_name(self.path, "strategy file path")
        )
        content = cast(bytes | str, self.content)
        if isinstance(content, str):
            content = content.encode("utf-8")
        elif not isinstance(content, bytes):
            raise StrategyBundleError("strategy file content must be bytes or text")
        object.__setattr__(
            self,
            "content",
            content.replace(b"\r\n", b"\n").replace(b"\r", b"\n"),
        )

    @property
    def sha256(self) -> str:
        """Return the digest of the canonical file bytes."""
        return hashlib.sha256(self.content).hexdigest()

    def as_dict(self) -> dict[str, str]:
        """Return the portable representation used in the bundle manifest."""
        return {
            "content": base64.b64encode(self.content).decode("ascii"),
            "path": self.path,
            "sha256": self.sha256,
        }


type StrategyFilesInput = Mapping[str, bytes | str] | Sequence[StrategyFile]


def _files(values: StrategyFilesInput, label: str) -> tuple[StrategyFile, ...]:
    if isinstance(values, Mapping):
        files = tuple(
            StrategyFile(path, cast(bytes, content)) for path, content in values.items()
        )
    else:
        files = tuple(values)
        if not all(isinstance(item, StrategyFile) for item in files):
            raise StrategyBundleError(f"{label} must contain StrategyFile values")
    ordered = tuple(sorted(files, key=lambda item: item.path))
    if len({item.path for item in ordered}) != len(ordered):
        raise StrategyBundleError(f"{label} paths must be unique")
    if not ordered:
        raise StrategyBundleError(f"{label} must not be empty")
    return ordered


def _dependency_values(value: object) -> object:
    if isinstance(value, Mapping):
        return _freeze(value)
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise StrategyBundleError("dependencies must be a mapping or sequence")
    if not all(isinstance(item, str) and item.strip() for item in value):
        raise StrategyBundleError("dependencies must contain non-empty strings")
    dependencies = tuple(sorted(value))
    return dependencies


@dataclass(frozen=True, slots=True)
class StrategyBundle:
    """A complete strategy submission whose identity is derived from its bytes.

    The retained values are frozen at construction.  The bundle hash includes the
    source and tests themselves, rather than only their file hashes, so the
    canonical bytes are also a portable integrity record.
    """

    strategy_id: str
    version: str
    source: tuple[StrategyFile, ...]
    tests: tuple[StrategyFile, ...]
    dependencies: object = field(default_factory=dict)
    platform_versions: Mapping[str, object] = field(default_factory=dict)
    harness_configuration: object = field(default_factory=dict)
    risk_declarations: object = field(default_factory=dict)
    capability_declarations: object = field(default_factory=dict)
    data_manifest: object = field(default_factory=dict)
    backtest_manifest: object = field(default_factory=dict)
    evidence_summary: object = field(default_factory=dict)
    limitations: tuple[str, ...] = ()
    harness_manifest_reference: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "strategy_id", _identifier(self.strategy_id, "strategy_id")
        )
        object.__setattr__(self, "version", _version(self.version))
        object.__setattr__(
            self, "source", _files(cast(StrategyFilesInput, self.source), "source")
        )
        object.__setattr__(
            self, "tests", _files(cast(StrategyFilesInput, self.tests), "tests")
        )
        object.__setattr__(self, "dependencies", _dependency_values(self.dependencies))
        for name in (
            "platform_versions",
            "harness_configuration",
            "risk_declarations",
            "capability_declarations",
            "data_manifest",
            "backtest_manifest",
            "evidence_summary",
        ):
            object.__setattr__(self, name, _freeze(getattr(self, name)))
        limitations = tuple(self.limitations)
        if not all(isinstance(item, str) and item.strip() for item in limitations):
            raise StrategyBundleError("limitations must contain non-empty strings")
        object.__setattr__(self, "limitations", tuple(sorted(limitations)))
        if self.harness_manifest_reference is not None:
            object.__setattr__(
                self,
                "harness_manifest_reference",
                _identifier(
                    self.harness_manifest_reference, "harness manifest reference"
                ),
            )

    @property
    def strategy_version(self) -> str:
        """Return the version name used by artifact provenance."""
        return self.version

    @property
    def harness_contract(self) -> str:
        """Return the authored program boundary implemented by the bundle."""
        return "harness-strategy-sdk"

    @property
    def canonical_document(self) -> dict[str, object]:
        """Return the complete content document without location-dependent fields."""
        return {
            "backtestManifest": _canonical_value(self.backtest_manifest),
            "capabilityDeclarations": _canonical_value(self.capability_declarations),
            "dataManifest": _canonical_value(self.data_manifest),
            "dependencies": _canonical_value(self.dependencies),
            "evidenceSummary": _canonical_value(self.evidence_summary),
            "harnessConfiguration": _canonical_value(self.harness_configuration),
            "harnessContract": {
                "kinds": ("strategy", "study"),
                "name": self.harness_contract,
            },
            "harnessManifestReference": self.harness_manifest_reference,
            "limitations": list(self.limitations),
            "platformVersions": _canonical_value(self.platform_versions),
            "riskDeclarations": _canonical_value(self.risk_declarations),
            "schema": "fca-thesis-strategy-bundle-v1",
            "source": [file.as_dict() for file in self.source],
            "strategyId": self.strategy_id,
            "tests": [file.as_dict() for file in self.tests],
            "version": self.version,
        }

    @property
    def canonical_bytes(self) -> bytes:
        """Return the deterministic UTF-8 bundle representation."""
        return _canonical_json_bytes(self.canonical_document)

    @property
    def bundle_sha256(self) -> str:
        """Return the content address of this bundle."""
        return hashlib.sha256(_HASH_DOMAIN + self.canonical_bytes).hexdigest()

    @property
    def content_hash(self) -> str:
        """Alias for the raw SHA-256 bundle identity."""
        return self.bundle_sha256

    @property
    def bundle_id(self) -> str:
        """Return a self-describing immutable bundle identity."""
        return f"sha256:{self.bundle_sha256}"

    @property
    def immutable_id(self) -> str:
        """Return the content-addressed identity used by external references."""
        return self.bundle_id

    def manifest(self) -> dict[str, object]:
        """Return a digest-oriented manifest suitable for a handoff record."""
        return {
            "bundleId": self.bundle_id,
            "bundleSha256": self.bundle_sha256,
            "canonicalBytesSha256": hashlib.sha256(self.canonical_bytes).hexdigest(),
            "strategyId": self.strategy_id,
            "version": self.version,
        }

    @classmethod
    def create(
        cls,
        *,
        strategy_id: str,
        version: str,
        source: Mapping[str, bytes | str] | Sequence[StrategyFile],
        tests: Mapping[str, bytes | str] | Sequence[StrategyFile],
        dependencies: Mapping[str, object] | Sequence[str] = (),
        platform_versions: Mapping[str, object] | None = None,
        harness_configuration: object = None,
        risk_declarations: object = None,
        capability_declarations: object = None,
        data_manifest: object = None,
        backtest_manifest: object = None,
        evidence_summary: object = None,
        limitations: Sequence[str] = (),
        harness_manifest_reference: str | None = None,
    ) -> StrategyBundle:
        """Build a bundle while making all equivalent inputs canonical."""
        return cls(
            strategy_id=strategy_id,
            version=version,
            source=_files(source, "source"),
            tests=_files(tests, "tests"),
            dependencies=dependencies,
            platform_versions={} if platform_versions is None else platform_versions,
            harness_configuration=(
                {} if harness_configuration is None else harness_configuration
            ),
            risk_declarations={} if risk_declarations is None else risk_declarations,
            capability_declarations=(
                {} if capability_declarations is None else capability_declarations
            ),
            data_manifest={} if data_manifest is None else data_manifest,
            backtest_manifest={} if backtest_manifest is None else backtest_manifest,
            evidence_summary={} if evidence_summary is None else evidence_summary,
            limitations=tuple(limitations),
            harness_manifest_reference=harness_manifest_reference,
        )


build_strategy_bundle = StrategyBundle.create


__all__ = [
    "StrategyBundle",
    "StrategyBundleError",
    "StrategyFile",
    "build_strategy_bundle",
]
