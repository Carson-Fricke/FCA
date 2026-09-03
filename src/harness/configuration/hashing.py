"""Canonical, cross-machine hashing for validated harness configuration."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel, SecretStr

_HASH_DOMAIN = b"fca:harness-configuration:v1\0"


def _decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("configuration decimals must be finite")
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def _canonical_value(value: object) -> Any:
    if isinstance(value, SecretStr):
        raise TypeError(
            "resolved secrets must never be included in configuration hashes"
        )
    if isinstance(value, BaseModel):
        return _canonical_value(value.model_dump(mode="python"))
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        raise TypeError("configuration hashes reject binary floating-point values")
    if isinstance(value, Decimal):
        return {"$decimal": _decimal_text(value)}
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("configuration datetimes must include a UTC offset")
        text = value.astimezone(UTC).isoformat(timespec="microseconds")
        return {"$datetime": text.replace("+00:00", "Z")}
    if isinstance(value, date):
        return {"$date": value.isoformat()}
    if isinstance(value, Enum):
        return _canonical_value(value.value)
    if isinstance(value, Mapping):
        canonical: dict[str, Any] = {}
        if not all(isinstance(key, str) for key in value):
            raise TypeError("configuration mapping keys must be strings")
        for key in sorted(value):
            canonical[key] = _canonical_value(value[key])
        return canonical
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        return [_canonical_value(item) for item in value]
    raise TypeError(f"unsupported configuration value: {type(value).__name__}")


def canonical_configuration_json(configuration: object) -> str:
    """Serialize configuration with stable keys and scalar representations."""
    return json.dumps(
        _canonical_value(configuration),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def canonical_configuration_bytes(configuration: object) -> bytes:
    """Return the canonical UTF-8 representation used by the hash function."""
    return canonical_configuration_json(configuration).encode("utf-8")


def configuration_hash(configuration: object) -> str:
    """Return a domain-separated SHA-256 hash of canonical configuration."""
    return hashlib.sha256(
        _HASH_DOMAIN + canonical_configuration_bytes(configuration)
    ).hexdigest()


canonical_hash = configuration_hash

__all__ = [
    "canonical_configuration_bytes",
    "canonical_configuration_json",
    "canonical_hash",
    "configuration_hash",
]
