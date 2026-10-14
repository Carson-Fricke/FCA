"""Shared JSON extraction and text-normalization helpers for composers."""

from __future__ import annotations

from collections.abc import Mapping

from thesis.agent.router import InvocationResult
from thesis_runtime.tools.json_recovery import extract_json_document

from ..errors import (
    FalsificationComposeError,
    WorkflowError,
)
from ..primitives import _text


def _extract_json_document(output: str) -> object | None:
    """Best-effort extraction of a JSON document from tolerant model output."""
    return extract_json_document(output)


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
