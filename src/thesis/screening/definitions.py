"""Immutable, content-addressed screener definitions authored as Rust source.

Thesis owns screener definitions as workspace artifacts; the harness stays
workspace-agnostic and executes the source with the per-run configuration the
client materializes.  A definition is self-identifying: its canonical JSON
document is domain-separated and hashed, so two equivalent definitions always
produce the same id and the same run request.  The authored source carries its
own digest (line endings normalized to LF) because the harness JIT verifies the
``bundle_hash`` it receives against the source bytes it compiles.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

_HASH_DOMAIN = b"fca:thesis-screener-definition:v1\0"

#: The bar intervals the harness accepts for a screening run.
BAR_INTERVALS: tuple[str, ...] = ("1min", "1d")


class ScreenerDefinitionError(ValueError):
    """Raised when a screener definition is not a valid deterministic artifact."""


def _canonical_json_bytes(payload: Mapping[str, object]) -> bytes:
    """Return deterministic JSON bytes for a configuration mapping."""
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ScreenerDefinitionError(f"{label} must be a non-empty string")
    return value.strip()


def _freeze(value: object) -> object:
    """Freeze configuration-shaped values before retaining them."""
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ScreenerDefinitionError("parameter mapping keys must be strings")
        return MappingProxyType({key: _freeze(value[key]) for key in sorted(value)})
    if isinstance(value, list | tuple):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set | frozenset):
        return tuple(sorted((_freeze(item) for item in value), key=repr))
    return value


def _canonical_value(value: object) -> Any:
    """Convert frozen values back to the JSON-shaped values the hash uses."""
    if isinstance(value, Mapping):
        return {str(key): _canonical_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_canonical_value(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class ScreenerDefinition:
    """A workspace screener definition whose identity derives from its bytes.

    ``source`` is the authored Rust program compiled by the harness JIT.
    ``parameters`` are optional per-definition JSON-shaped values, frozen at
    construction and canonicalized (sorted keys) before hashing.
    """

    name: str
    lookback_days: int
    bar_interval: str
    rank_metric: str
    universe_revision: str | None = None
    source: str = ""
    parameters: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _name(self.name))
        lookback = self.lookback_days
        if isinstance(lookback, bool) or not isinstance(lookback, int):
            raise ScreenerDefinitionError("lookback_days must be a positive integer")
        if lookback <= 0:
            raise ScreenerDefinitionError("lookback_days must be a positive integer")
        if self.bar_interval not in BAR_INTERVALS:
            allowed = ", ".join(repr(item) for item in BAR_INTERVALS)
            raise ScreenerDefinitionError(f"bar_interval must be one of {allowed}")
        object.__setattr__(self, "rank_metric", _text(self.rank_metric, "rank_metric"))
        if self.universe_revision is not None:
            object.__setattr__(
                self,
                "universe_revision",
                _text(self.universe_revision, "universe_revision"),
            )
        source = self.source
        if not isinstance(source, str):
            raise ScreenerDefinitionError("source must be Rust source text")
        source = source.replace("\r\n", "\n").replace("\r", "\n")
        if not source.strip():
            raise ScreenerDefinitionError("source must not be empty")
        object.__setattr__(self, "source", source)
        parameters = self.parameters
        if parameters is None:
            parameters = {}
        if not isinstance(parameters, Mapping):
            raise ScreenerDefinitionError("parameters must be a mapping when supplied")
        object.__setattr__(self, "parameters", _freeze(parameters))

    @property
    def source_bytes(self) -> bytes:
        """Return the canonical authored-source bytes (LF line endings)."""
        return self.source.encode("utf-8")

    @property
    def source_sha256(self) -> str:
        """Return the digest the harness verifies as ``bundle_hash``."""
        return hashlib.sha256(self.source_bytes).hexdigest()

    @property
    def canonical_document(self) -> dict[str, object]:
        """Return the complete content document without derived location fields."""
        return {
            "barInterval": self.bar_interval,
            "lookbackDays": self.lookback_days,
            "name": self.name,
            "parameters": _canonical_value(self.parameters),
            "rankMetric": self.rank_metric,
            "schema": "fca-thesis-screener-definition-v1",
            "source": self.source,
            "sourceSha256": self.source_sha256,
            "universeRevision": self.universe_revision,
        }

    @property
    def canonical_bytes(self) -> bytes:
        """Return the deterministic UTF-8 definition representation."""
        return _canonical_json_bytes(self.canonical_document)

    @property
    def definition_sha256(self) -> str:
        """Return the content address of this definition."""
        return hashlib.sha256(_HASH_DOMAIN + self.canonical_bytes).hexdigest()

    @property
    def definition_hash(self) -> str:
        """Alias for the domain-separated definition digest."""
        return self.definition_sha256

    @property
    def definition_id(self) -> str:
        """Return the self-describing immutable workspace artifact identity."""
        return f"sha256:{self.definition_sha256}"

    def materialize_config(self, *, as_of: str) -> dict[str, object]:
        """Return the per-run configuration sent to ``run_screener``.

        ``bundle_hash`` is the authored-source digest the harness verifies
        while JIT compiling; the definition id and hash travel alongside it as
        provenance, and ``as_of`` binds the run to one evaluation date.
        """
        moment = _text(as_of, "as_of")
        return {
            "as_of": moment,
            "bar_interval": self.bar_interval,
            "bundle_hash": self.source_sha256,
            "definition_hash": self.definition_sha256,
            "definition_id": self.definition_id,
            "lookback_days": self.lookback_days,
            "name": self.name,
            "parameters": _canonical_value(self.parameters),
            "rank_metric": self.rank_metric,
            "schema_version": 1,
            "shell": "screen",
            "source": self.source,
            "source_sha256": self.source_sha256,
            "universe_revision": self.universe_revision,
        }

    def document(self) -> dict[str, object]:
        """Return the portable JSON document the HTTP surface serves."""
        document = dict(self.canonical_document)
        document["definitionId"] = self.definition_id
        document["definitionSha256"] = self.definition_sha256
        return document


def _name(value: object) -> str:
    name = _text(value, "screener name")
    if len(name) > 256:
        raise ScreenerDefinitionError("screener name must be at most 256 characters")
    return name


__all__ = [
    "BAR_INTERVALS",
    "ScreenerDefinition",
    "ScreenerDefinitionError",
]
