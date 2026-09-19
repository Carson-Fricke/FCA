"""Shared JSON extraction and text-normalization helpers for composers."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping

from thesis.agent.router import InvocationResult

from ..errors import (
    FalsificationComposeError,
    WorkflowError,
)
from ..primitives import _text


def _balanced_objects(text: str) -> Iterator[str]:
    """Yield every balanced top-level ``{...}`` slice, in source order.

    The scan respects JSON string quoting and escapes so braces inside string
    literals never affect the nesting depth.
    """
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    yield text[start : index + 1]
                    break
        start = text.find("{", start + 1)


_FENCED_JSON = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def _extract_json_document(output: str) -> object | None:
    """Best-effort extraction of a JSON document from tolerant model output.

    Tried in order: the raw output as-is; every fenced code block (``json`` or
    bare); then each balanced top-level object.  Returns ``None`` when no
    candidate parses as JSON.
    """
    candidates: list[str] = [output]
    candidates.extend(match.group(1) for match in _FENCED_JSON.finditer(output))
    candidates.extend(_balanced_objects(output))
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def _json_document(invocation: InvocationResult, label: str) -> dict[str, object]:
    output = invocation.response.output
    if not isinstance(output, str) or not output.strip():
        raise FalsificationComposeError(f"model provider returned no {label} content")
    document = _extract_json_document(output)
    if document is None:
        raise FalsificationComposeError(
            f"model output is not valid JSON {label} content"
        )
    if not isinstance(document, dict):
        raise FalsificationComposeError(f"{label} content must be a JSON object")
    return document


def _compose_text(document: Mapping[str, object], key: str, label: str) -> str:
    try:
        return _text(document.get(key), label, max_length=8_192)
    except WorkflowError as error:
        raise FalsificationComposeError(str(error)) from error


def _optional_compose_text(
    document: Mapping[str, object],
    key: str,
    default: str,
    *,
    max_length: int,
) -> str:
    """Return a trimmed optional field or ``default`` when it is absent/blank."""
    value = document.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    if isinstance(value, str) and len(value) > max_length:
        value = value[:max_length].strip()
    try:
        return _text(value, key, max_length=max_length)
    except WorkflowError as error:
        raise FalsificationComposeError(str(error)) from error


def _model_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FalsificationComposeError(f"{label} must be non-empty text")
    return value.strip()
