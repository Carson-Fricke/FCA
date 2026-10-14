"""Tolerant JSON extraction from model output.

The model-facing surfaces sometimes receive JSON that is not a bare document:
prose around the payload, markdown fences, or several objects in one reply.
These helpers recover the first parseable document deterministically, so a
caller can repair or accept the call instead of failing it outright.

The composer layer (:mod:`thesis.extensions.workflows`) reuses these helpers
through its own thin wrappers, so the agent runtime and the composers recover
JSON with exactly one implementation.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping

_FENCED_JSON = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def balanced_objects(text: str) -> Iterator[str]:
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


def extract_json_document(output: str) -> object | None:
    """Best-effort extraction of a JSON document from tolerant model output.

    Tried in order: the raw output as-is; every fenced code block (``json`` or
    bare); then each balanced top-level object.  Returns ``None`` when no
    candidate parses as JSON.
    """
    candidates: list[str] = [output]
    candidates.extend(match.group(1) for match in _FENCED_JSON.finditer(output))
    candidates.extend(balanced_objects(output))
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def extract_json_object(output: str) -> Mapping[str, object] | None:
    """Extract the first JSON document that is a JSON object, or ``None``."""
    document = extract_json_document(output)
    return document if isinstance(document, Mapping) else None


__all__ = [
    "balanced_objects",
    "extract_json_document",
    "extract_json_object",
]
