"""Recursive, workspace-scoped REST surface for execution sub-agents.

The hierarchy is not stored as a mutable resource: every route projects it from
the execution's ordered event log.  A route path is the chain of sub-agent ids
from the top-level phase orchestrator down to the requested node, so the same
router serves arbitrarily deep trees.  Event pages and SSE streams keep the
execution's single global cursor: filtering to a node (or its whole subtree)
never renumbers events, so ``after_cursor`` resume and event-id deduplication
still work.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from thesis.chat import ChatService, MessageRole, TranscriptItemKind
from thesis.chat.contracts import WorkflowExecution
from thesis.execution.events import ExecutionEventType
from thesis.execution.sub_agents import (
    STATUS_NEEDS_INPUT,
    SubAgentContextSpec,
    SubAgentNode,
    SubAgentTree,
    append_sub_agent_event,
    filter_replay,
)
from thesis.http.chat import _service_error, get_chat_service
from thesis.http.streaming import (
    TEXT_EVENT_STREAM_MEDIA_TYPE,
    StreamSleeper,
    _resume_cursor,
    format_execution_event,
)

DEFAULT_POLL_INTERVAL_SECONDS = 1.0


def _utc_now() -> datetime:
    return datetime.now(UTC)


async def _default_sleeper(seconds: float) -> None:
    if seconds > 0:
        await asyncio.sleep(seconds)


class SubAgentHTTPModel(BaseModel):
    """Strict request boundary for the sub-agent surface."""

    model_config = ConfigDict(extra="forbid")


class SubAgentMessageRequest(SubAgentHTTPModel):
    """One user input message addressed to a sub-agent node."""

    actor_id: str = Field(min_length=1, max_length=128)
    body: str = Field(min_length=1, max_length=32_768)


def _load_tree(
    chat_service: ChatService, workspace_id: str, execution_id: str
) -> tuple[WorkflowExecution, SubAgentTree]:
    execution = chat_service.get_execution(workspace_id, execution_id)
    replay = chat_service.replay_events(workspace_id, execution_id)
    tree = SubAgentTree.from_events(execution_id, replay.events)
    return execution, tree


def _resolve_node(tree: SubAgentTree, node_path: str) -> SubAgentNode | None:
    """Resolve a canonical ancestor chain to exactly one node, or ``None``."""
    segments = [segment for segment in node_path.split("/") if segment]
    if not segments:
        return None
    node = tree.get(segments[-1])
    if node is None:
        return None
    expected_ancestors = list(reversed(segments[:-1]))
    current = node.parent_sub_agent_id
    for expected in expected_ancestors:
        if current != expected:
            return None
        parent = tree.get(current)
        current = None if parent is None else parent.parent_sub_agent_id
    if current is not None:
        return None
    return node


def _node_scope(tree: SubAgentTree, node: SubAgentNode, include_descendants: bool):
    if include_descendants:
        return tree.subtree_ids(node.sub_agent_id)
    return tree.own_ids(node.sub_agent_id)


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={"code": "sub_agent_not_found", "message": "sub-agent was not found"},
    )


async def _node_event_stream(
    chat_service: ChatService,
    *,
    workspace_id: str,
    execution_id: str,
    node_ids: frozenset[str],
    after_cursor: int,
    wait: StreamSleeper,
    poll_interval_seconds: float,
    max_poll_cycles: int | None,
):
    """Tail the execution stream, yielding only frames for the node scope."""
    cursor = after_cursor
    cycles = 0
    while True:
        replay = chat_service.replay_events(
            workspace_id, execution_id, after_cursor=cursor
        )
        for event in replay:
            cursor = event.cursor
            sub_agent_id = event.payload.get("subAgentId")
            if isinstance(sub_agent_id, str) and sub_agent_id in node_ids:
                yield format_execution_event(event)
        cycles += 1
        if max_poll_cycles is not None and cycles >= max_poll_cycles:
            return
        await wait(poll_interval_seconds)


def create_sub_agent_router(
    *,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    sleeper: StreamSleeper | None = None,
    max_poll_cycles: int | None = None,
) -> APIRouter:
    """Create the recursive sub-agent router under executions."""
    router = APIRouter(
        prefix="/v1/workspaces/{workspace_id}/executions/{execution_id}",
        tags=["sub-agents"],
    )
    wait = sleeper if sleeper is not None else _default_sleeper

    @router.get("/sub_agents")
    async def list_sub_agents(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        execution_id: Annotated[str, Path(min_length=1, max_length=128)],
        chat_service: Annotated[ChatService, Depends(get_chat_service)],
    ) -> dict[str, object]:
        """List top-level sub-agents (the phase orchestrators)."""
        try:
            _, tree = _load_tree(chat_service, workspace_id, execution_id)
        except Exception as error:
            raise _service_error(error) from error
        return {"items": [node.as_dict() for node in tree.roots]}

    @router.get("/sub_agents/{node_path:path}/events/stream")
    async def stream_node_events(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        execution_id: Annotated[str, Path(min_length=1, max_length=128)],
        node_path: Annotated[str, Path()],
        chat_service: Annotated[ChatService, Depends(get_chat_service)],
        after_cursor: Annotated[int, Query(ge=0)] = 0,
        include_descendants: Annotated[bool, Query()] = False,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ) -> StreamingResponse:
        """Stream the node's own events (or its subtree) as SSE frames."""
        try:
            _, tree = _load_tree(chat_service, workspace_id, execution_id)
        except Exception as error:
            raise _service_error(error) from error
        node = _resolve_node(tree, node_path)
        if node is None:
            raise _not_found()
        resume = _resume_cursor(last_event_id, after_cursor)
        return StreamingResponse(
            _node_event_stream(
                chat_service,
                workspace_id=workspace_id,
                execution_id=execution_id,
                node_ids=_node_scope(tree, node, include_descendants),
                after_cursor=resume,
                wait=wait,
                poll_interval_seconds=poll_interval_seconds,
                max_poll_cycles=max_poll_cycles,
            ),
            media_type=TEXT_EVENT_STREAM_MEDIA_TYPE,
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @router.get("/sub_agents/{node_path:path}/events")
    async def replay_node_events(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        execution_id: Annotated[str, Path(min_length=1, max_length=128)],
        node_path: Annotated[str, Path()],
        chat_service: Annotated[ChatService, Depends(get_chat_service)],
        after_cursor: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int | None, Query(ge=1, le=1_000)] = None,
        include_descendants: Annotated[bool, Query()] = False,
    ) -> dict[str, object]:
        """Replay the node's own events, preserving the global cursor."""
        try:
            _, tree = _load_tree(chat_service, workspace_id, execution_id)
        except Exception as error:
            raise _service_error(error) from error
        node = _resolve_node(tree, node_path)
        if node is None:
            raise _not_found()
        node_ids = _node_scope(tree, node, include_descendants)
        try:
            page = chat_service.replay_events(
                workspace_id, execution_id, after_cursor=after_cursor
            )
        except Exception as error:
            raise _service_error(error) from error
        return filter_replay(page, node_ids, limit=limit).as_dict()

    @router.post("/sub_agents/{node_path:path}/messages")
    async def post_node_message(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        execution_id: Annotated[str, Path(min_length=1, max_length=128)],
        node_path: Annotated[str, Path()],
        request: SubAgentMessageRequest,
        chat_service: Annotated[ChatService, Depends(get_chat_service)],
    ) -> dict[str, object]:
        """Append an input message to the single conversation for one node."""
        try:
            execution, tree = _load_tree(chat_service, workspace_id, execution_id)
        except Exception as error:
            raise _service_error(error) from error
        node = _resolve_node(tree, node_path)
        if node is None:
            raise _not_found()
        if not node.accepts_input:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "sub_agent_not_accepting_input",
                    "message": "sub-agent is not accepting input",
                },
            )
        try:
            message = chat_service.create_message(
                workspace_id,
                execution.chat_id,
                actor_id=request.actor_id,
                body=request.body,
                role=MessageRole.USER,
                execution_id=execution_id,
                kind=TranscriptItemKind.SUB_AGENT,
                data={
                    "executionId": execution_id,
                    "parentSubAgentId": node.parent_sub_agent_id,
                    "phase": node.phase,
                    "subAgentId": node.sub_agent_id,
                },
            )
        except Exception as error:
            raise _service_error(error) from error
        # On-demand nodes keep accepting input for the whole phase; a node that
        # was answering an explicit request (``needs_input``) flips back off.
        stays_open = node.accepts_input and node.status != STATUS_NEEDS_INPUT
        append_sub_agent_event(
            chat_service.event_store,
            execution_id=execution_id,
            workspace_id=workspace_id,
            event_type=ExecutionEventType.SUB_AGENT_PROGRESS,
            occurred_at=_utc_now(),
            sub_agent_id=node.sub_agent_id,
            parent_sub_agent_id=node.parent_sub_agent_id,
            phase=node.phase,
            role=node.role,
            context=SubAgentContextSpec(),
            accepts_input=stays_open,
            extra={
                "inputMessageId": message.message_id,
                "kind": "thinking",
                "text": "user input received",
            },
        )
        result = message.as_dict()
        result["subAgentId"] = node.sub_agent_id
        return result

    @router.get("/sub_agents/{node_path:path}/sub_agents")
    async def list_node_children(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        execution_id: Annotated[str, Path(min_length=1, max_length=128)],
        node_path: Annotated[str, Path()],
        chat_service: Annotated[ChatService, Depends(get_chat_service)],
    ) -> dict[str, object]:
        """List the direct children of one node (arbitrarily deep)."""
        try:
            _, tree = _load_tree(chat_service, workspace_id, execution_id)
        except Exception as error:
            raise _service_error(error) from error
        node = _resolve_node(tree, node_path)
        if node is None:
            raise _not_found()
        return {"items": [child.as_dict() for child in node.children]}

    @router.get("/sub_agents/{node_path:path}")
    async def get_sub_agent(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        execution_id: Annotated[str, Path(min_length=1, max_length=128)],
        node_path: Annotated[str, Path()],
        chat_service: Annotated[ChatService, Depends(get_chat_service)],
    ) -> dict[str, object]:
        """Return one node's projected record including its subtree."""
        try:
            _, tree = _load_tree(chat_service, workspace_id, execution_id)
        except Exception as error:
            raise _service_error(error) from error
        node = _resolve_node(tree, node_path)
        if node is None:
            raise _not_found()
        return node.as_dict()

    return router


__all__ = [
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "SubAgentMessageRequest",
    "create_sub_agent_router",
]
