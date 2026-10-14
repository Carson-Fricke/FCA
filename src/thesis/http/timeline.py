"""Unified per-chat conversation stream (messages + events) over SSE.

One chat has exactly one timeline.  Opening the stream returns the most recent
``limit`` items in order and then tails new items by their monotonic ``seq``,
so a client needs a single connection regardless of how many sub-agents it
drills into.  The optional ``thread`` filter narrows the same endpoint to one
agent's thread when a view asks for a backfill.

The timeline is the workspace catalog itself; there is no projection repair.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import TypeAdapter

from thesis.http.chat import _service_error, get_chat_service
from thesis.http.streaming import (
    TEXT_EVENT_STREAM_MEDIA_TYPE,
    StreamSleeper,
    _resume_cursor,
)
from thesis.http.wire import TimelineItemWire, TimelinePageWire
from thesis_runtime.contracts.codec import record_from_payload
from thesis_runtime.contracts.records import ChatMessage, WorkflowExecution
from thesis_runtime.conversations import ChatService, RecordNotFound
from thesis_runtime.conversations.timeline import TimelineReader

DEFAULT_RECOVERY_INTERVAL_SECONDS = 15.0
DEFAULT_LIMIT = 200
TIMELINE_ITEM_ADAPTER = TypeAdapter(TimelineItemWire)


def _timeline(request: Request) -> TimelineReader:
    timeline = getattr(request.app.state, "timeline", None)
    if not isinstance(timeline, TimelineReader):
        raise HTTPException(
            status_code=503,
            detail={
                "code": "timeline_unavailable",
                "message": "conversation persistence is not configured",
            },
        )
    return timeline


_TIMELINE_WIRE_FIELDS = (
    "id",
    "chatId",
    "workspaceId",
    "seq",
    "executionId",
    "threadId",
    "kind",
    "payload",
)


def _timestamp_text(item: Mapping[str, object]) -> str:
    """Return the item's ISO timestamp, tolerating pre-hoist documents."""
    value = item.get("ts")
    if isinstance(value, str) and value:
        return value
    created = item.get("createdAt")
    if isinstance(created, str) and created:
        return created.replace("+00:00", "Z")
    payload = item.get("payload")
    if isinstance(payload, Mapping):
        created = payload.get("created_at") or payload.get("createdAt")
        if isinstance(created, str) and created:
            return created.replace("+00:00", "Z")
    return ""


def _clean(item: dict[str, object]) -> dict[str, object]:
    """Project one stored item onto the timeline wire contract.

    Storage documents carry routing and record fields beyond the wire shape
    (ownership, record type, record timestamps); the projection selects only
    wire fields so storage changes never leak into the API. ``ts`` falls back
    to the record timestamp for documents written before it was hoisted.
    """
    document = {key: item[key] for key in _TIMELINE_WIRE_FIELDS if key in item}
    document["ts"] = _timestamp_text(item)
    record_type = (
        ChatMessage
        if document.get("kind") == "message"
        else WorkflowExecution
        if document.get("kind") == "execution"
        else None
    )
    payload = document.get("payload")
    if record_type is not None and isinstance(payload, dict):
        document["payload"] = record_from_payload(record_type, payload).as_dict()
    wire = TIMELINE_ITEM_ADAPTER.validate_python(document)
    return TIMELINE_ITEM_ADAPTER.dump_python(
        wire, mode="json", by_alias=True, exclude_unset=True
    )


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
    timeline: TimelineReader,
    *,
    workspace_id: str,
    chat_id: str,
    after_seq: int,
    limit: int,
    thread_id: str | None,
    recovery_interval_seconds: float,
    wait: StreamSleeper | None,
    max_poll_cycles: int | None,
    chat_service: ChatService,
) -> AsyncIterator[str]:
    changed = asyncio.Event()
    loop = asyncio.get_running_loop()

    def on_commit() -> None:
        try:
            loop.call_soon_threadsafe(changed.set)
        except RuntimeError:
            pass  # A disconnected reader may have closed its event loop.

    release = await asyncio.to_thread(
        timeline.commits.subscribe, workspace_id, chat_id, on_commit
    )
    try:
        cursor = after_seq
        cycles = 0
        while True:
            # Clear before reading: a commit during the read leaves a pending
            # wake. Waiting is async and consumes no idle thread-pool slot.
            changed.clear()
            try:
                await asyncio.to_thread(chat_service.get_chat, workspace_id, chat_id)
            except RecordNotFound:
                return
            items = await asyncio.to_thread(
                timeline.read_after,
                workspace_id=workspace_id,
                chat_id=chat_id,
                after_seq=cursor,
                limit=limit,
                thread_id=thread_id,
            )
            try:
                await asyncio.to_thread(chat_service.get_chat, workspace_id, chat_id)
            except RecordNotFound:
                return
            for item in items:
                cursor = max(cursor, _seq(item))
                yield _format(item)
            cycles += 1
            if max_poll_cycles is not None and cycles >= max_poll_cycles:
                return
            if len(items) >= limit:
                continue
            if wait is not None:
                await wait(recovery_interval_seconds)
            else:
                try:
                    await asyncio.wait_for(
                        changed.wait(), timeout=recovery_interval_seconds
                    )
                except TimeoutError:
                    pass
    finally:
        release()


def create_timeline_router(
    *,
    recovery_interval_seconds: float = DEFAULT_RECOVERY_INTERVAL_SECONDS,
    sleeper: StreamSleeper | None = None,
    max_poll_cycles: int | None = None,
) -> APIRouter:
    """Create the unified timeline REST and SSE router."""
    router = APIRouter(prefix="/v1/workspaces/{workspace_id}", tags=["timeline"])
    wait = sleeper

    @router.get(
        "/chats/{chat_id}/timeline",
        response_model=TimelinePageWire,
        response_model_exclude_unset=True,
    )
    async def read_timeline(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        chat_id: Annotated[str, Path(min_length=1, max_length=128)],
        request: Request,
        chat_service: Annotated[ChatService, Depends(get_chat_service)],
        after: Annotated[
            int | None,
            Query(
                ge=0,
                description="Exclusive forward cursor; cannot be used with before.",
            ),
        ] = None,
        before: Annotated[
            int | None,
            Query(
                ge=0,
                description="Exclusive backward cursor; cannot be used with after.",
            ),
        ] = None,
        limit: Annotated[int, Query(ge=1, le=1_000)] = DEFAULT_LIMIT,
        thread: Annotated[str | None, Query(max_length=128)] = None,
    ) -> dict[str, object]:
        """Return an ascending page, optionally scoped to one thread.

        Without a cursor (or with ``after=0``), return the latest page. To
        load all older history, repeat with ``before=nextBefore`` and the
        same thread filter until ``nextBefore`` is null. A nonempty page's
        cursor is its minimum sequence; exhaustion may require an empty page.
        ``after`` and ``before`` are exclusive and cannot be supplied together.
        """
        if after is not None and before is not None:
            raise HTTPException(
                status_code=422,
                detail="after and before are mutually exclusive",
            )
        try:
            await asyncio.to_thread(chat_service.get_chat, workspace_id, chat_id)
        except Exception as error:
            raise _service_error(error) from error
        timeline = _timeline(request)
        # Capture the published watermark before querying history. New arrivals
        # above it belong to SSE, including arrivals during a filtered query.
        watermark = await asyncio.to_thread(
            timeline.latest_sequence, workspace_id=workspace_id, chat_id=chat_id
        )
        if before is not None:
            items = await asyncio.to_thread(
                timeline.read_before,
                workspace_id=workspace_id,
                chat_id=chat_id,
                before_seq=before,
                limit=limit,
                thread_id=thread,
            )
        elif after is not None and after > 0:
            items = await asyncio.to_thread(
                timeline.read_after,
                workspace_id=workspace_id,
                chat_id=chat_id,
                after_seq=after,
                limit=limit,
                thread_id=thread,
            )
        else:
            items = await asyncio.to_thread(
                timeline.read_before,
                workspace_id=workspace_id,
                chat_id=chat_id,
                before_seq=watermark + 1,
                limit=limit,
                thread_id=thread,
            )
        return {
            "items": [_clean(item) for item in items],
            "nextBefore": min(_seq(item) for item in items) if items else None,
            "after": watermark,
        }

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
            await asyncio.to_thread(chat_service.get_chat, workspace_id, chat_id)
        except Exception as error:
            raise _service_error(error) from error
        timeline = _timeline(request)
        resume = _resume_cursor(last_event_id, after)
        return StreamingResponse(
            _timeline_stream(
                timeline,
                workspace_id=workspace_id,
                chat_id=chat_id,
                after_seq=resume,
                limit=limit,
                thread_id=thread,
                recovery_interval_seconds=recovery_interval_seconds,
                wait=wait,
                max_poll_cycles=max_poll_cycles,
                chat_service=chat_service,
            ),
            media_type=TEXT_EVENT_STREAM_MEDIA_TYPE,
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return router


__all__ = ["DEFAULT_RECOVERY_INTERVAL_SECONDS", "create_timeline_router"]
