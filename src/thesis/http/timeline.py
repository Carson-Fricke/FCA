"""Unified per-chat conversation stream (messages + events) over SSE.

One chat has exactly one timeline.  Opening the stream returns the most recent
``limit`` items in order and then tails new items by their monotonic ``seq``,
so a client needs a single connection regardless of how many sub-agents it
drills into.  The optional ``thread`` filter narrows the same endpoint to one
agent's thread when a view asks for a backfill.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request
from fastapi.responses import StreamingResponse

from thesis.chat import ChatService
from thesis.cosmos.timeline import CosmosTimeline
from thesis.http.chat import _service_error, get_chat_service
from thesis.http.streaming import (
    TEXT_EVENT_STREAM_MEDIA_TYPE,
    StreamSleeper,
    _resume_cursor,
)

DEFAULT_POLL_INTERVAL_SECONDS = 0.5
DEFAULT_LIMIT = 200


async def _default_sleeper(seconds: float) -> None:
    if seconds > 0:
        await asyncio.sleep(seconds)


def _timeline(request: Request) -> CosmosTimeline:
    timeline = getattr(request.app.state, "timeline", None)
    if not isinstance(timeline, CosmosTimeline):
        raise HTTPException(
            status_code=503,
            detail={
                "code": "timeline_unavailable",
                "message": "conversation persistence is not configured",
            },
        )
    return timeline


def _clean(item: dict[str, object]) -> dict[str, object]:
    """Strip Cosmos system properties from one item before transport."""
    return {key: value for key, value in item.items() if not key.startswith("_")}


def _seq(item: dict[str, object]) -> int:
    """Read an item's sequence number as an ``int``."""
    value = item.get("seq", 0)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return 0
    return 0


def _format(item: dict[str, object]) -> str:
    seq = _seq(item)
    data = json.dumps(_clean(item), default=str, separators=(",", ":"))
    return f"id: {seq}\nevent: timeline\ndata: {data}\n\n"


async def _timeline_stream(
    timeline: CosmosTimeline,
    *,
    chat_id: str,
    after_seq: int,
    limit: int,
    thread_id: str | None,
    poll_interval_seconds: float,
    wait: StreamSleeper,
    max_poll_cycles: int | None,
) -> AsyncIterator[str]:
    cursor = after_seq
    if cursor <= 0:
        recent = await asyncio.to_thread(
            timeline.last, chat_id=chat_id, limit=limit, thread_id=thread_id
        )
        for item in recent:
            cursor = max(cursor, _seq(item))
            yield _format(item)
    cycles = 0
    while True:
        items = await asyncio.to_thread(
            timeline.read_after,
            chat_id=chat_id,
            after_seq=cursor,
            limit=limit,
            thread_id=thread_id,
        )
        for item in items:
            cursor = max(cursor, _seq(item))
            yield _format(item)
        cycles += 1
        if max_poll_cycles is not None and cycles >= max_poll_cycles:
            return
        await wait(poll_interval_seconds)


def create_timeline_router(
    *,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    sleeper: StreamSleeper | None = None,
    max_poll_cycles: int | None = None,
) -> APIRouter:
    """Create the unified timeline REST and SSE router."""
    router = APIRouter(prefix="/v1/workspaces/{workspace_id}", tags=["timeline"])
    wait = sleeper if sleeper is not None else _default_sleeper

    @router.get("/chats/{chat_id}/timeline")
    async def read_timeline(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        chat_id: Annotated[str, Path(min_length=1, max_length=128)],
        request: Request,
        chat_service: Annotated[ChatService, Depends(get_chat_service)],
        after: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=1_000)] = DEFAULT_LIMIT,
        thread: Annotated[str | None, Query(max_length=128)] = None,
    ) -> dict[str, object]:
        """Return an ordered page of one chat's timeline for backfill."""
        try:
            chat_service.get_chat(workspace_id, chat_id)
        except Exception as error:
            raise _service_error(error) from error
        timeline = _timeline(request)
        if after > 0:
            items = await asyncio.to_thread(
                timeline.read_after,
                chat_id=chat_id,
                after_seq=after,
                limit=limit,
                thread_id=thread,
            )
        else:
            items = await asyncio.to_thread(
                timeline.last, chat_id=chat_id, limit=limit, thread_id=thread
            )
        return {"items": [_clean(item) for item in items]}

    @router.get("/chats/{chat_id}/timeline/stream")
    async def stream_timeline(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        chat_id: Annotated[str, Path(min_length=1, max_length=128)],
        request: Request,
        chat_service: Annotated[ChatService, Depends(get_chat_service)],
        after: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=1_000)] = DEFAULT_LIMIT,
        thread: Annotated[str | None, Query(max_length=128)] = None,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ) -> StreamingResponse:
        """Stream the chat timeline, backfilling the most recent items first."""
        try:
            chat_service.get_chat(workspace_id, chat_id)
        except Exception as error:
            raise _service_error(error) from error
        timeline = _timeline(request)
        resume = _resume_cursor(last_event_id, after)
        return StreamingResponse(
            _timeline_stream(
                timeline,
                chat_id=chat_id,
                after_seq=resume,
                limit=limit,
                thread_id=thread,
                poll_interval_seconds=poll_interval_seconds,
                wait=wait,
                max_poll_cycles=max_poll_cycles,
            ),
            media_type=TEXT_EVENT_STREAM_MEDIA_TYPE,
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return router


__all__ = ["DEFAULT_POLL_INTERVAL_SECONDS", "create_timeline_router"]
