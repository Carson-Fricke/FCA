"""Loader for the repository's non-executable YAML configuration subset.

The retired ``harness.configuration`` module carried the Python loader; its
Rust replacement is the ``config`` crate in the harness workspace. Thesis
needs only the read-only subset for its runtime documents: nested mappings,
scalars, inline sequences, and quoted strings — the same subset the Rust
loader accepts, with no anchors, aliases, or executable tags.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

#: Keys that must carry ``${ENV_REF}`` references, never resolved secret values.
_SECRET_KEYS: Final[frozenset[str]] = frozenset(
    {"password", "secret", "token", "api_key", "api_secret"}
)


class ConfigurationError(ValueError):
    """Raised when a configuration document is malformed or unsafe."""


def load_yaml_mapping(path: Path | str) -> dict[str, object]:
    """Load one configuration document into a plain mapping."""
    text = Path(path).read_text(encoding="utf-8")
    return parse_yaml_mapping(text)


def parse_yaml_mapping(text: str) -> dict[str, object]:
    """Parse the supported YAML subset into a mapping and reject secrets."""
    root: dict[str, object] = {}
    section: dict[str, object] | None = None
    for number, raw in enumerate(text.splitlines(), start=1):
        stripped = raw.split("#", 1)[0].rstrip()
        if not stripped.strip():
            continue
        if stripped != stripped.lstrip():
            if section is None:
                raise ConfigurationError(
                    f"configuration line {number} is indented without a section"
                )
            section[_mapping_entry(stripped.strip(), number)[0]] = _mapping_entry(
                stripped.strip(), number
            )[1]
            continue
        key, _, value = stripped.partition(":")
        key = key.strip()
        value = value.strip()
        if not key or key != _safe_key(key):
            raise ConfigurationError(f"configuration line {number} has an invalid key")
        if value:
            root[key] = _scalar(value, number)
            section = None
            continue
        nested: dict[str, object] = {}
        root[key] = nested
        section = nested
    _reject_secret_values(root, "")
    return root


def _mapping_entry(text: str, number: int) -> tuple[str, object]:
    key, _, value = text.partition(":")
    key = key.strip()
    if not key or key != _safe_key(key):
        raise ConfigurationError(f"configuration line {number} has an invalid key")
    return key, _scalar(value.strip(), number)


def _safe_key(key: str) -> bool:
    return key.replace("_", "a").replace("-", "a").isalnum() and key[0].isalpha()


def _scalar(value: str, number: int) -> object:
    if value in {"null", "~", "Null", "NULL"}:
        return None
    if value in {"true", "True", "TRUE"}:
        return True
    if value in {"false", "False", "FALSE"}:
        return False
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_scalar(item.strip(), number) for item in inner.split(",")]
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def _reject_secret_values(node: object, path: str) -> None:
    """Fail when a secret-keyed field carries a resolved value, not a reference."""
    if not isinstance(node, dict):
        return
    for key, value in node.items():
        location = f"{path}.{key}" if path else str(key)
        if key.lower() in _SECRET_KEYS and isinstance(value, str):
            if not (value.startswith("${") and value.endswith("}")):
                raise ConfigurationError(
                    f"configuration field {location!r} carries a resolved secret; "
                    "use a ${ENV_REF} reference"
                )
        _reject_secret_values(value, location)


__all__ = ["ConfigurationError", "load_yaml_mapping", "parse_yaml_mapping"]
