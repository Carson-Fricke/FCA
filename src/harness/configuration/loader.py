"""Safe YAML-subset loading and explicit runtime secret resolution."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

from pydantic import SecretStr, TypeAdapter

from harness.configuration.models import HarnessConfig


@dataclass(frozen=True, slots=True)
class _YamlLine:
    number: int
    indent: int
    text: str


class ConfigurationSecretError(ValueError):
    """Raised when committed configuration contains a secret instead of a ref."""


_INTEGER = re.compile(r"[-+]?\d+")
_DECIMAL = re.compile(r"[-+]?(?:\d+\.\d*|\.\d+)(?:[eE][-+]?\d+)?")
_ENVIRONMENT_REFERENCE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")
_SECRET_KEY = re.compile(
    r"(?:^|_)(?:access[_-]?key|api[_-]?key|api[_-]?secret|bearer|"
    r"client[_-]?secret|connection[_-]?string|credential|password|"
    r"private[_-]?key|secret|token)(?:$|_)",
    re.IGNORECASE,
)
_CONFIG_ADAPTER = TypeAdapter(HarnessConfig)


def harness_config_dir(root: str | Path | None = None) -> Path:
    """Resolve ``config/harness`` without retaining a machine-specific path."""
    if root is None:
        configured_root = os.environ.get("HARNESS_CONFIG_ROOT")
        root_path = (
            Path(configured_root)
            if configured_root
            else Path(__file__).resolve().parents[3]
        )
    else:
        root_path = Path(root)
    if root_path.name == "harness" and root_path.parent.name == "config":
        return root_path
    return root_path / "config" / "harness"


def _strip_comment(text: str) -> str:
    quote: str | None = None
    bracket_depth = 0
    escaped = False
    for index, character in enumerate(text):
        if quote == '"' and escaped:
            escaped = False
            continue
        if quote == '"' and character == "\\":
            escaped = True
            continue
        if character in {"'", '"'}:
            quote = (
                character if quote is None else None if quote == character else quote
            )
            continue
        if quote is None:
            if character == "[":
                bracket_depth += 1
            elif character == "]":
                bracket_depth -= 1
            elif character == "#" and bracket_depth == 0:
                if index == 0 or text[index - 1].isspace():
                    return text[:index].rstrip()
    if quote is not None or bracket_depth != 0:
        raise ValueError("unterminated YAML quote or flow sequence")
    return text.rstrip()


def _split_mapping_entry(text: str) -> tuple[str, str]:
    quote: str | None = None
    bracket_depth = 0
    for index, character in enumerate(text):
        if character in {"'", '"'}:
            quote = (
                character if quote is None else None if quote == character else quote
            )
        elif quote is None:
            if character == "[":
                bracket_depth += 1
            elif character == "]":
                bracket_depth -= 1
            elif character == ":" and bracket_depth == 0:
                if index + 1 == len(text) or text[index + 1].isspace():
                    key = text[:index].strip()
                    if key:
                        return key, text[index + 1 :].strip()
    raise ValueError(f"expected a YAML mapping entry: {text!r}")


def _split_flow_values(text: str) -> list[str]:
    values: list[str] = []
    start = 0
    quote: str | None = None
    depth = 0
    for index, character in enumerate(text):
        if character in {"'", '"'}:
            quote = (
                character if quote is None else None if quote == character else quote
            )
        elif quote is None:
            if character == "[":
                depth += 1
            elif character == "]":
                depth -= 1
            elif character == "," and depth == 0:
                values.append(text[start:index].strip())
                start = index + 1
    if quote is not None or depth != 0:
        raise ValueError(f"invalid YAML flow sequence: {text!r}")
    values.append(text[start:].strip())
    return values


def _unquote(value: str) -> str:
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1].replace("''", "'")
    if value.startswith('"') and value.endswith('"'):
        parsed = json.loads(value)
        if not isinstance(parsed, str):
            raise ValueError(f"expected a YAML string: {value!r}")
        return parsed
    return value


def _scalar(value: str) -> Any:
    if not value:
        return None
    if value[0] in {"'", '"'}:
        if len(value) < 2 or value[-1] != value[0]:
            raise ValueError(f"unterminated quoted YAML value: {value!r}")
        return _unquote(value)
    if value.startswith("[") and value.endswith("]"):
        contents = value[1:-1].strip()
        return (
            [] if not contents else [_scalar(v) for v in _split_flow_values(contents)]
        )
    if value in {"null", "Null", "NULL", "~"}:
        return None
    if value in {"true", "True", "TRUE"}:
        return True
    if value in {"false", "False", "FALSE"}:
        return False
    if _INTEGER.fullmatch(value):
        return int(value)
    if _DECIMAL.fullmatch(value):
        try:
            return Decimal(value)
        except InvalidOperation as error:
            raise ValueError(f"invalid exact decimal: {value!r}") from error
    if value.startswith(("&", "*", "!", "{")):
        raise ValueError(f"unsupported YAML value: {value!r}")
    return value


def _clean_lines(text: str) -> list[_YamlLine]:
    lines: list[_YamlLine] = []
    for number, raw_line in enumerate(text.splitlines(), 1):
        indent_text = raw_line[: len(raw_line) - len(raw_line.lstrip())]
        if "\t" in indent_text:
            raise ValueError(f"YAML line {number} uses tabs for indentation")
        content = _strip_comment(raw_line.lstrip(" "))
        if content:
            lines.append(_YamlLine(number, len(indent_text), content))
    return lines


def _parse_block(lines: list[_YamlLine], index: int, indent: int) -> tuple[Any, int]:
    if index >= len(lines) or lines[index].indent != indent:
        raise ValueError("invalid YAML indentation")
    sequence = lines[index].text == "-" or lines[index].text.startswith("- ")
    if sequence:
        result: list[Any] = []
        while index < len(lines) and lines[index].indent == indent:
            line = lines[index]
            if not (line.text == "-" or line.text.startswith("- ")):
                break
            item = line.text[1:].strip()
            if item:
                result.append(_scalar(item))
                index += 1
            elif index + 1 < len(lines) and lines[index + 1].indent > indent:
                parsed, index = _parse_block(lines, index + 1, lines[index + 1].indent)
                result.append(parsed)
            else:
                raise ValueError(f"YAML line {line.number} has an empty sequence item")
        return result, index

    result_map: dict[str, Any] = {}
    while index < len(lines) and lines[index].indent == indent:
        line = lines[index]
        if line.text == "-" or line.text.startswith("- "):
            break
        raw_key, raw_value = _split_mapping_entry(line.text)
        key = _unquote(raw_key)
        if not key or any(character.isspace() for character in key):
            raise ValueError(f"invalid YAML key on line {line.number}: {raw_key!r}")
        if key in result_map:
            raise ValueError(f"duplicate YAML key {key!r} on line {line.number}")
        index += 1
        if raw_value:
            parsed = _scalar(raw_value)
        elif index < len(lines) and lines[index].indent > indent:
            parsed, index = _parse_block(lines, index, lines[index].indent)
        else:
            parsed = None
        result_map[key] = parsed
    return result_map, index


def load_yaml_mapping(path: str | Path) -> dict[str, Any]:
    """Load the non-executable YAML subset used by harness configuration."""
    config_path = Path(path)
    lines = _clean_lines(config_path.read_text(encoding="utf-8"))
    if not lines:
        raise ValueError(f"configuration file is empty: {config_path}")
    parsed, index = _parse_block(lines, 0, lines[0].indent)
    if index != len(lines):
        raise ValueError(f"invalid YAML near line {lines[index].number}")
    if not isinstance(parsed, dict):
        raise ValueError(f"configuration root must be a mapping: {config_path}")
    return parsed


def _is_reference_tree(value: object) -> bool:
    if isinstance(value, str):
        return _ENVIRONMENT_REFERENCE.fullmatch(value) is not None
    if isinstance(value, Mapping):
        return bool(value) and all(_is_reference_tree(item) for item in value.values())
    if isinstance(value, list | tuple):
        return bool(value) and all(_is_reference_tree(item) for item in value)
    return False


def _reject_committed_secrets(value: object, path: str = "") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            item_path = f"{path}.{key}" if path else str(key)
            if isinstance(key, str) and _SECRET_KEY.search(key):
                if not _is_reference_tree(item):
                    raise ConfigurationSecretError(
                        f"{item_path} must contain only ${{ENV_VAR}} references"
                    )
            _reject_committed_secrets(item, item_path)
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            _reject_committed_secrets(item, f"{path}.{index}")


def load_configuration(path: str | Path) -> HarnessConfig:
    """Load and strictly validate one API, backtest, or trader document."""
    values = load_yaml_mapping(path)
    _reject_committed_secrets(values)
    return _CONFIG_ADAPTER.validate_python(values)


def load_example(
    shell: Literal["api", "backtest", "trader"],
    root: str | Path | None = None,
) -> HarnessConfig:
    """Load the committed example for an execution shell."""
    return load_configuration(harness_config_dir(root) / f"{shell}.yaml")


def environment_variable_name(reference: str) -> str:
    """Extract a validated environment-variable name from ``${NAME}``."""
    match = _ENVIRONMENT_REFERENCE.fullmatch(reference)
    if match is None:
        raise ValueError("environment reference must use ${ENV_VAR} syntax")
    return match.group(1)


def resolve_environment_reference(
    reference: str,
    environ: Mapping[str, str] | None = None,
) -> SecretStr:
    """Resolve one reference at runtime without placing its value in config."""
    variable = environment_variable_name(reference)
    values = os.environ if environ is None else environ
    try:
        value = values[variable]
    except KeyError as error:
        raise ValueError(
            f"runtime environment variable {variable} is not set"
        ) from error
    if not value:
        raise ValueError(f"runtime environment variable {variable} must not be empty")
    return SecretStr(value)


def configuration_shell(
    configuration: HarnessConfig,
) -> Literal["api", "backtest", "trader"]:
    """Return the discriminated execution shell with a precise static type."""
    return configuration.shell


__all__ = [
    "ConfigurationSecretError",
    "configuration_shell",
    "environment_variable_name",
    "harness_config_dir",
    "load_configuration",
    "load_example",
    "load_yaml_mapping",
    "resolve_environment_reference",
]
