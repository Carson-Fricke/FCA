"""Bounded display projections over canonical receipts and timeline activity.

The durable record and its hash are never modified. Previews explicitly advertise
detail availability at the original sequence; detail reads and SSE retain the
complete current wire. This module owns no persistence or execution effects.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, cast

from thesis.http.wire import TranscriptMessageWire
from thesis_runtime.contracts.records import ChatMessage

PAGE_BYTES = 256 * 1024
MESSAGE_TEXT_CHARS = 2_000
RECEIPT_BYTES = 8 * 1024


def _size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode())


def _preview(value: Any, depth: int = 0, budget: list[int] | None = None) -> Any:
    budget = [RECEIPT_BYTES] if budget is None else budget
    if isinstance(value, str):
        text = value[:240]
        while text and _size(text) > budget[0]:
            text = text[: len(text) // 2]
        budget[0] -= _size(text)
        return text
    if isinstance(value, Mapping):
        budget[0] -= 2
        if depth >= 5:
            return {}
        projected = {}
        priority = ("datumType", "ok", "failure", "summary", "text")
        keys = [key for key in priority if key in value] + [
            key for key in value if key not in priority
        ]
        for key in keys[:32]:
            cost = _size(key) + 2
            if budget[0] < cost + 8:
                break
            budget[0] -= cost
            projected[key] = _preview(value[key], depth + 1, budget)
        return projected
    if isinstance(value, list | tuple):
        budget[0] -= 2
        projected = []
        for item in value[:8]:
            if budget[0] < 8:
                break
            budget[0] -= 1
            projected.append(_preview(item, depth + 1, budget))
        return projected
    budget[0] -= _size(value)
    return value


def _receipt(value: Mapping[str, Any] | None) -> tuple[dict[str, Any] | None, bool]:
    if value is None:
        return None, False
    # Continuation is opaque provider replay evidence, not display content.
    result = {
        key: item
        for key, item in value.items()
        if key not in {"provider_continuation", "reasoning"}
    }
    if _size(result) <= RECEIPT_BYTES:
        return result, False
    preview = _preview(result)
    if "datums" in preview:
        for datum in preview["datums"]:
            datum["detailsAvailable"] = True
    elif "datumType" in preview:
        preview["detailsAvailable"] = True
    return preview, True


def project_message(message: ChatMessage) -> dict[str, Any]:
    """Project display fields, with a separate receipt instead of canonical context."""
    record = cast(dict[str, Any], message.as_dict())
    metadata = dict(record.pop("data") or {})
    session = metadata.pop("sessionMessage")
    content = session["content"]
    metadata.pop("datums", None)
    metadata.pop("reasoning", None)
    metadata_cut = _size(metadata) > RECEIPT_BYTES
    record.pop("content_hash")
    result, result_cut = _receipt(content.get("result"))
    tool_result, tool_cut = _receipt(record.pop("tool_result"))
    requests = [dict(request) for request in content.get("requests", ())]
    request_cut = any(len(request["arguments"]) > 240 for request in requests)
    if request_cut:
        requests = [
            {**request, "arguments": request["arguments"][:240]} for request in requests
        ]
    body = record.pop("body")
    reasoning = record.pop("reasoning")
    view = {
        **record,
        "body": body[:MESSAGE_TEXT_CHARS],
        "reasoning": reasoning[:240] if reasoning else reasoning,
        "data": (_preview(metadata) if metadata_cut else metadata)
        if metadata
        else None,
        "tool_result": tool_result,
        "receipt": {
            "requests": requests,
            "correlation": content.get("correlation"),
            "result": result,
        },
        "detailsAvailable": result_cut
        or metadata_cut
        or tool_cut
        or request_cut
        or len(body) > MESSAGE_TEXT_CHARS
        or bool(reasoning and len(reasoning) > 240),
    }
    return TranscriptMessageWire.model_validate(view).model_dump(
        mode="json", by_alias=True
    )


def project_activity(item: dict[str, Any]) -> dict[str, Any]:
    """Bound large historical extension payloads, retaining their exact detail seq."""
    if item["kind"] != "event" or _size(item["payload"]["payload"]) <= RECEIPT_BYTES:
        return item
    payload = _preview(item["payload"]["payload"])
    payload["detailsSeq"] = item["seq"]
    for key in ("toolResult", "tool_result"):
        if isinstance(payload.get(key), dict):
            payload[key]["detailsSeq"] = item["seq"]
    return {**item, "payload": {**item["payload"], "payload": payload}}


def bound_page(items: list[dict[str, Any]], *, newest: bool) -> list[dict[str, Any]]:
    """Keep a contiguous sequence interval so paging never skips omitted items."""
    selected = []
    size = 0
    for item in reversed(items) if newest else items:
        item_size = _size(item)
        if selected and size + item_size > PAGE_BYTES - 1024:
            break
        selected.append(item)
        size += item_size
    return list(reversed(selected)) if newest else selected
