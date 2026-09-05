"""Server-sent event streaming for the chat/execution boundary.

The stream fans out both surfaces of one execution:

- durable execution machine events (the machine timeline), identified by the
  store cursor so ``Last-Event-ID`` resume reuses the existing replay cursor
  semantics; and
- chat message events for the execution's originating chat, because the chat
  is the sole messaging surface.  Chat message events are ephemeral: they
  carry no ``id`` field, are re-snapshotted on (re)connect, and clients
  suppress duplicates by ``messageId``.

Provider token deltas are allowed as a documented ephemeral event kind
(``provider.delta``) during model phases.  A delta source is injected by the
application composition; without one the stream never emits deltas.

Poll pacing and termination are injected so runtimes and deterministic tests
never depend on wall-clock decisions.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query
from fastapi.responses import StreamingResponse

from thesis.chat import ChatMessage
from thesis.chat.service import ChatService
from thesis.execution.events import ExecutionEvent
from thesis.http.chat import _service_error, get_chat_service

TEXT_EVENT_STREAM_MEDIA_TYPE = "text/event-stream"
EXECUTION_EVENT_KIND = "execution.event"
CHAT_MESSAGE_EVENT_KIND = "chat.message"
PROVIDER_DELTA_EVENT_KIND = "provider.delta"
EPHEMERAL_EVENT_KINDS = frozenset({CHAT_MESSAGE_EVENT_KIND, PROVIDER_DELTA_EVENT_KIND})
DEFAULT_POLL_INTERVAL_SECONDS = 1.0

ProviderDeltaSource = Callable[[str, str], Sequence[Mapping[str, object]]]
StreamSleeper = Callable[[float], Awaitable[None]]


async def _default_sleeper(seconds: float) -> None:
    if seconds > 0:
        await asyncio.sleep(seconds)


def format_sse_event(
    *,
    data: Mapping[str, object],
    event: str,
    event_id: str | None = None,
) -> str:
    """Render one SSE frame; durable frames carry ``id`` for cursor resume."""
    lines = []
    if event_id is not None:
        lines.append(f"id: {event_id}")
    lines.append(f"event: {event}")
    payload = json.dumps(dict(data), separators=(",", ":"), sort_keys=True)
    lines.append(f"data: {payload}")
    return "\n".join(lines) + "\n\n"


def format_execution_event(event: ExecutionEvent) -> str:
    """Frame one durable machine event; the SSE id is the replay cursor."""
    return format_sse_event(
        event_id=str(event.cursor),
        event=event.type.value,
        data={"kind": EXECUTION_EVENT_KIND, **event.as_dict()},
    )


def format_chat_message_event(message: ChatMessage) -> str:
    """Frame one chat message as an ephemeral event without an SSE id."""
    return format_sse_event(
        event=CHAT_MESSAGE_EVENT_KIND,
        data={"kind": CHAT_MESSAGE_EVENT_KIND, "message": message.as_dict()},
    )


def format_provider_delta_event(execution_id: str, delta: Mapping[str, object]) -> str:
    """Frame one provider token delta as an ephemeral event without an SSE id."""
    return format_sse_event(
        event=PROVIDER_DELTA_EVENT_KIND,
        data={
            "kind": PROVIDER_DELTA_EVENT_KIND,
            "executionId": execution_id,
            "delta": dict(delta),
        },
    )


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
                "message": "Last-Event-ID must be an execution event cursor",
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


async def _execution_event_stream(
    chat_service: ChatService,
    *,
    workspace_id: str,
    execution_id: str,
    chat_id: str,
    after_cursor: int,
    include_chat_messages: bool,
    include_provider_deltas: bool,
    provider_delta_source: ProviderDeltaSource | None,
    poll_interval_seconds: float,
    wait: StreamSleeper,
    max_poll_cycles: int | None,
):
    """Yield SSE frames, tailing both surfaces until the injected bound."""
    cursor = after_cursor
    emitted_messages: set[str] = set()
    cycles = 0
    while True:
        replay = chat_service.replay_events(
            workspace_id, execution_id, after_cursor=cursor
        )
        for event in replay:
            cursor = event.cursor
            yield format_execution_event(event)
        if include_chat_messages:
            for message in chat_service.list_messages(workspace_id, chat_id):
                if message.message_id in emitted_messages:
                    continue
                emitted_messages.add(message.message_id)
                yield format_chat_message_event(message)
        if include_provider_deltas and provider_delta_source is not None:
            for delta in provider_delta_source(workspace_id, execution_id):
                yield format_provider_delta_event(execution_id, delta)
        cycles += 1
        if max_poll_cycles is not None and cycles >= max_poll_cycles:
            return
        await wait(poll_interval_seconds)


def create_streaming_router(
    *,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    sleeper: StreamSleeper | None = None,
    provider_delta_source: ProviderDeltaSource | None = None,
    max_poll_cycles: int | None = None,
) -> APIRouter:
    """Create the SSE router.

    The router resolves the service from ``app.state.chat_service``; poll
    pacing, termination bounds, and the provider delta source are injected
    here so runtimes and deterministic tests control streaming behavior
    without wall-clock decisions.
    """
    router = APIRouter(prefix="/v1/workspaces/{workspace_id}", tags=["streaming"])
    wait = sleeper if sleeper is not None else _default_sleeper

    @router.get("/executions/{execution_id}/events/stream")
    async def stream_execution_events(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        execution_id: Annotated[str, Path(min_length=1, max_length=128)],
        chat_service: Annotated[ChatService, Depends(get_chat_service)],
        after_cursor: Annotated[int, Query(ge=0)] = 0,
        chat_messages: Annotated[bool, Query()] = True,
        provider_deltas: Annotated[bool, Query()] = True,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ) -> StreamingResponse:
        """Stream execution machine events and chat messages over SSE."""
        try:
            execution = chat_service.get_execution(workspace_id, execution_id)
        except Exception as error:
            raise _service_error(error) from error
        resume = _resume_cursor(last_event_id, after_cursor)
        return StreamingResponse(
            _execution_event_stream(
                chat_service,
                workspace_id=workspace_id,
                execution_id=execution.execution_id,
                chat_id=execution.chat_id,
                after_cursor=resume,
                include_chat_messages=chat_messages,
                include_provider_deltas=(
                    provider_deltas and provider_delta_source is not None
                ),
                provider_delta_source=provider_delta_source,
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


__all__ = [
    "CHAT_MESSAGE_EVENT_KIND",
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "EPHEMERAL_EVENT_KINDS",
    "EXECUTION_EVENT_KIND",
    "PROVIDER_DELTA_EVENT_KIND",
    "ProviderDeltaSource",
    "StreamSleeper",
    "TEXT_EVENT_STREAM_MEDIA_TYPE",
    "create_streaming_router",
    "format_chat_message_event",
    "format_execution_event",
    "format_provider_delta_event",
    "format_sse_event",
]
