"""Shared configuration file and environment loading helpers."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic_settings import BaseSettings
from pydantic_settings.sources import EnvSettingsSource


@dataclass(frozen=True, slots=True)
class _YamlLine:
    number: int
    indent: int
    text: str


_INTEGER = re.compile(r"[-+]?\d+")
_DECIMAL = re.compile(r"[-+]?(?:\d+\.\d*|\.\d+)")
_SECRET_KEY = re.compile(
    r"(?:^|_)(?:access[_-]?key|api[_-]?key|api[_-]?secret|bearer|"
    r"client[_-]?secret|connection[_-]?string|credential|password|"
    r"private[_-]?key|secret|token)(?:$|_)",
    re.IGNORECASE,
)


def market_lake_config_dir(root: str | Path | None = None) -> Path:
    """Resolve the market-lake config directory without machine-specific paths."""
    if root is None:
        root_path = Path(__file__).resolve().parents[3]
    else:
        root_path = Path(root)

    if root_path.name == "market-lake" and root_path.parent.name == "config":
        return root_path
    return root_path / "config" / "market-lake"


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
            if quote is None:
                quote = character
            elif quote == character:
                quote = None
            continue
        if quote is None:
            if character in "[({":
                bracket_depth += 1
            elif character in "])}":
                bracket_depth -= 1
            elif (
                character == "#"
                and bracket_depth == 0
                and (index == 0 or text[index - 1].isspace())
            ):
                return text[:index].rstrip()
    if quote is not None:
        raise ValueError("unterminated quoted YAML value")
    if bracket_depth != 0:
        raise ValueError("unclosed YAML flow collection")
    return text.rstrip()


def _split_mapping_entry(text: str) -> tuple[str, str]:
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
            if quote is None:
                quote = character
            elif quote == character:
                quote = None
            continue
        if quote is None:
            if character in "[({":
                bracket_depth += 1
            elif character in "])}":
                bracket_depth -= 1
            elif character == ":" and bracket_depth == 0:
                if index + 1 == len(text) or text[index + 1].isspace():
                    key = text[:index].strip()
                    if not key:
                        break
                    return key, text[index + 1 :].strip()
    raise ValueError(f"expected a mapping entry: {text!r}")


def _split_flow_values(text: str) -> list[str]:
    values: list[str] = []
    start = 0
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
            if quote is None:
                quote = character
            elif quote == character:
                quote = None
        elif quote is None:
            if character == "[":
                bracket_depth += 1
            elif character == "]":
                bracket_depth -= 1
            elif character == "," and bracket_depth == 0:
                values.append(text[start:index].strip())
                start = index + 1
    if quote is not None or bracket_depth != 0:
        raise ValueError(f"invalid flow collection: {text!r}")
    values.append(text[start:].strip())
    return values


def _unquote(value: str) -> str:
    if value.startswith("'") and value.endswith("'"):
        return value[1:-1].replace("''", "'")
    if value.startswith('"') and value.endswith('"'):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as error:
            raise ValueError(f"invalid double-quoted YAML value: {value!r}") from error
        if not isinstance(parsed, str):
            raise ValueError(f"expected a string value: {value!r}")
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
        if not contents:
            return []
        return [_scalar(item) for item in _split_flow_values(contents)]
    if value in {"null", "Null", "NULL", "~"}:
        return None
    if value in {"true", "True", "TRUE"}:
        return True
    if value in {"false", "False", "FALSE"}:
        return False
    if _INTEGER.fullmatch(value):
        return int(value)
    if _DECIMAL.fullmatch(value):
        return float(value)
    if value.startswith(("&", "*", "!", "{")):
        raise ValueError(f"unsupported YAML value: {value!r}")
    return value


def _key(value: str) -> str:
    parsed = _unquote(value)
    if not parsed or any(character.isspace() for character in parsed):
        raise ValueError(f"invalid YAML mapping key: {value!r}")
    if _SECRET_KEY.search(parsed):
        raise ValueError(
            f"secret-bearing YAML key {parsed!r} is not allowed; use the environment"
            " or a Key Vault reference"
        )
    return parsed


def _clean_lines(text: str) -> list[_YamlLine]:
    lines: list[_YamlLine] = []
    for number, raw_line in enumerate(text.splitlines(), 1):
        if "\t" in raw_line[: len(raw_line) - len(raw_line.lstrip())]:
            raise ValueError(f"YAML line {number} uses tabs for indentation")
        content = _strip_comment(raw_line.lstrip(" "))
        if not content:
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        lines.append(_YamlLine(number, indent, content))
    return lines


def _parse_block(lines: list[_YamlLine], index: int, indent: int) -> tuple[Any, int]:
    if index >= len(lines) or lines[index].indent != indent:
        raise ValueError("invalid YAML indentation")
    is_sequence = lines[index].text == "-" or lines[index].text.startswith("- ")
    if is_sequence:
        result: list[Any] = []
        while index < len(lines) and lines[index].indent == indent:
            line = lines[index]
            if not (line.text == "-" or line.text.startswith("- ")):
                break
            item = line.text[1:].strip()
            if not item:
                if index + 1 >= len(lines) or lines[index + 1].indent <= indent:
                    raise ValueError(
                        f"YAML line {line.number} has an empty sequence item"
                    )
                value, index = _parse_block(lines, index + 1, lines[index + 1].indent)
            else:
                value = _scalar(item)
                index += 1
                if index < len(lines) and lines[index].indent > indent:
                    raise ValueError(
                        f"unexpected YAML indentation on line {lines[index].number}"
                    )
            result.append(value)
        return result, index

    result_map: dict[str, Any] = {}
    while index < len(lines) and lines[index].indent == indent:
        line = lines[index]
        if line.text == "-" or line.text.startswith("- "):
            break
        raw_key, raw_value = _split_mapping_entry(line.text)
        key = _key(raw_key)
        if key in result_map:
            raise ValueError(f"duplicate YAML key {key!r} on line {line.number}")
        index += 1
        if raw_value:
            value = _scalar(raw_value)
            if index < len(lines) and lines[index].indent > indent:
                raise ValueError(
                    f"unexpected YAML indentation on line {lines[index].number}"
                )
        elif index < len(lines) and lines[index].indent > indent:
            value, index = _parse_block(lines, index, lines[index].indent)
        else:
            value = None
        result_map[key] = value
    return result_map, index


def load_yaml_mapping(path: Path) -> dict[str, Any]:
    """Load the strict YAML subset used by the committed market-lake files."""
    lines = _clean_lines(path.read_text(encoding="utf-8"))
    if not lines:
        raise ValueError(f"configuration file is empty: {path}")
    parsed, index = _parse_block(lines, 0, lines[0].indent)
    if index != len(lines):
        raise ValueError(f"invalid YAML near line {lines[index].number}")
    if not isinstance(parsed, dict):
        raise ValueError(f"configuration root must be a mapping: {path}")
    return parsed


def _environment_data(
    model_type: type[BaseSettings], environ: Mapping[str, str]
) -> dict[str, Any]:
    source = EnvSettingsSource(model_type)
    source.env_vars = {key.lower(): value for key, value in environ.items()}
    return source()


def _merge_mappings(
    defaults: Mapping[str, Any], overrides: Mapping[str, Any]
) -> dict[str, Any]:
    merged = dict(defaults)
    for key, value in overrides.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = _merge_mappings(merged[key], value)
        else:
            merged[key] = value
    return merged


def validate_yaml_settings[SettingsModel: BaseSettings](
    model_type: type[SettingsModel],
    defaults: Mapping[str, Any],
    environ: Mapping[str, str] | None = None,
) -> SettingsModel:
    """Validate YAML data, putting environment values above file defaults."""
    overrides = _environment_data(
        model_type, os.environ if environ is None else environ
    )
    return model_type.model_validate(_merge_mappings(defaults, overrides))
