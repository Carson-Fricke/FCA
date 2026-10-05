"""Shared transport primitives for the unified timeline stream."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import HTTPException

TEXT_EVENT_STREAM_MEDIA_TYPE = "text/event-stream"

StreamSleeper = Callable[[float], Awaitable[None]]


def _resume_cursor(last_event_id: str | None, after_cursor: int) -> int:
    """Resolve the reconnect cursor from Last-Event-ID or the query param."""
    if last_event_id is None or not last_event_id.strip():
        return after_cursor
    try:
        value = int(last_event_id.strip())
    except ValueError as error:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_last_event_id",
                "message": "Last-Event-ID must be a timeline sequence",
            },
        ) from error
    if value < 0:
        raise HTTPException(
            status_code=400,
            detail={
                "code": "invalid_last_event_id",
                "message": "Last-Event-ID must not be negative",
            },
        )
    return value


__all__ = ["StreamSleeper", "TEXT_EVENT_STREAM_MEDIA_TYPE"]
