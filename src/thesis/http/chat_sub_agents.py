"""Chat-scoped REST surface for durable sub-agents.

Sub-agents are attached to a chat, not to a workflow execution, so this router
serves the durable ownership view: it projects the first-class
:class:`~thesis.chat.contracts.SubAgentRecord` store into a nested tree and
exposes list, read, children, operator-input, and operator-cancel routes.
Because the tree is a pure function of durable records, a process restart
reconstructs exactly the same nodes.

The execution-scoped router in :mod:`thesis.http.sub_agents` remains the
event-log projection used for streaming a workflow's sub-agent activity; this
router is the durable record view that also covers children spawned directly
from chat.

Operator input is trusted attribution: the answering actor is derived from the
authenticated principal the application exposes to this router, never from the
request body.  The shared auth dependency currently discards its result, so the
application must either set ``request.state.authenticated_principal`` (or
``authenticated_operator_id``) from that dependency or inject an
``actor_resolver`` into :func:`create_chat_sub_agent_router`.  When neither is
present the endpoint fails closed and reports exactly what is missing.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Path,
    Query,
    Request,
    status,
)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from thesis.agent.runtime.conversation import Conversations
from thesis.agent.runtime.delivery import (
    NodeInputNotAccepted,
    NodeInputNotFound,
)
from thesis.agent.runtime.lifecycle import SubAgentLifecycle
from thesis.agent.runtime.policy import (
    AnsweringActor,
    OperatorActorKind,
    OperatorPolicyDenied,
)
from thesis.agent.runtime.store import PersistenceSubAgentStore
from thesis.agent.runtime.tree import (
    ChatSubAgentTree,
    project_chat_sub_agents,
)
from thesis.chat import ChatService, RecordNotFound, WorkflowExecutionState
from thesis.http.chat import _service_error, get_chat_service
from thesis.http.streaming import (
    TEXT_EVENT_STREAM_MEDIA_TYPE,
    StreamSleeper,
    _resume_cursor,
)

DEFAULT_POLL_INTERVAL_SECONDS = 1.0

#: Execution states that no longer have live work to stop.
_TERMINAL_EXECUTION_STATES = frozenset(
    {
        WorkflowExecutionState.COMPLETED,
        WorkflowExecutionState.FAILED,
        WorkflowExecutionState.STOPPED,
        WorkflowExecutionState.CANCELLED,
    }
)

#: ``app.state`` key holding the :class:`Conversations` service that owns every
#: conversation's turns; the operator-answer route resolves it.
CONVERSATIONS_STATE_KEY = "conversations"

#: ``request.state`` key holding the authenticated principal.  The value is the
#: same string the shared auth dependency returns, for example
#: ``"session:<account-id>"`` for a cookie session.
AUTHENTICATED_PRINCIPAL_STATE_KEY = "authenticated_principal"

_SESSION_PRINCIPAL_PREFIX = "session:"

#: Resolves the authenticated principal for one request, or ``None``.
ActorResolver = Callable[[Request], str | None]


def _store_for(chat_service: ChatService) -> PersistenceSubAgentStore:
    return PersistenceSubAgentStore(chat_service.persistence)


def _not_found() -> HTTPException:
    return HTTPException(
        status_code=404,
        detail={"code": "sub_agent_not_found", "message": "sub-agent was not found"},
    )


def _actor_identity_unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        detail={
            "code": "actor_identity_unavailable",
            "message": (
                "the authenticated principal is not exposed to the chat "
                "sub-agent router; set request.state.authenticated_principal "
                "(or inject actor_resolver) from the shared auth dependency"
            ),
        },
    )


def authenticated_actor_id(principal: str) -> str:
    """Derive a non-secret actor id from one authenticated principal.

    A cookie session principal is ``"session:<account-id>"`` and yields the
    account id.  Every other principal (a static bearer token or an Entra JWT)
    is a credential, so only a stable digest of it is persisted as the actor.
    """
    if principal.startswith(_SESSION_PRINCIPAL_PREFIX):
        account_id = principal[len(_SESSION_PRINCIPAL_PREFIX) :]
        if account_id:
            return account_id
    digest = hashlib.sha256(principal.encode("utf-8")).hexdigest()
    return f"principal-{digest[:24]}"


def authenticated_principal(
    request: Request, resolver: ActorResolver | None = None
) -> str | None:
    """Return the trusted principal for ``request``, or ``None``.

    The strongest available identity is used: an injected ``actor_resolver``
    first, then the authenticated principal the application exposes on
    ``request.state``.
    """
    if resolver is not None:
        value = resolver(request)
        return value if isinstance(value, str) and value.strip() else None
    value = getattr(request.state, AUTHENTICATED_PRINCIPAL_STATE_KEY, None)
    return value if isinstance(value, str) and value.strip() else None


def _resolve_conversations(request: Request) -> Conversations | None:
    service = getattr(request.app.state, CONVERSATIONS_STATE_KEY, None)
    return service if isinstance(service, Conversations) else None


class ChatSubAgentMessageRequest(BaseModel):
    """One operator input message addressed to a chat-scoped sub-agent.

    The answering actor is never accepted here: it is derived from the
    authenticated session by the route.
    """

    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1, max_length=32_768)


def _utc_now() -> datetime:
    return datetime.now(UTC)


async def _default_sleeper(seconds: float) -> None:
    if seconds > 0:
        await asyncio.sleep(seconds)


def _node_not_executable() -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "code": "sub_agent_without_execution",
            "message": ("sub-agent has no workflow execution; it has no activity log"),
        },
    )


def _conversations_unavailable() -> HTTPException:
    return HTTPException(
        status_code=503,
        detail={
            "code": "conversations_unavailable",
            "message": (
                "the conversations service is not composed; operator answers require it"
            ),
        },
    )


def _stop_node_execution(
    chat_service: ChatService,
    workspace_id: str,
    execution_id: str | None,
    *,
    actor_id: str,
) -> str | None:
    """Stop a node's execution when it still has live work, else ``None``.

    A node without an execution, or one whose execution is absent or already
    terminal, has nothing left to stop; the operator cancel still succeeds so
    the affordance is idempotent.
    """
    if execution_id is None:
        return None
    try:
        execution = chat_service.get_execution(workspace_id, execution_id)
    except RecordNotFound:
        return None
    if execution.state in _TERMINAL_EXECUTION_STATES:
        return None
    chat_service.stop_workflow(workspace_id, execution_id, actor_id=actor_id)
    return execution_id


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
) -> AsyncIterator[str]:
    """Tail the execution stream, yielding only frames for the node scope."""
    from thesis.http.streaming import format_execution_event

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


def create_chat_sub_agent_router(
    *,
    actor_resolver: ActorResolver | None = None,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    sleeper: StreamSleeper | None = None,
    max_poll_cycles: int | None = None,
) -> APIRouter:
    """Create the durable, chat-scoped sub-agent router.

    ``actor_resolver`` is the seam for the authenticated principal when the
    application cannot set it on request state.  Answers are admitted by the
    composed conversations service's operator policies; without a service the
    route fails closed with a service-unavailable error.
    """
    router = APIRouter(
        prefix="/v1/workspaces/{workspace_id}/chats/{chat_id}",
        tags=["sub-agents"],
    )
    wait = sleeper if sleeper is not None else _default_sleeper

    def _tree(
        workspace_id: str, chat_id: str, chat_service: ChatService
    ) -> tuple[PersistenceSubAgentStore, ChatSubAgentTree]:
        store = _store_for(chat_service)
        records = store.list_sub_agents(workspace_id)
        tree = project_chat_sub_agents(
            records, workspace_id=workspace_id, chat_id=chat_id
        )
        return store, tree

    @router.get("/sub_agents")
    async def list_chat_sub_agents(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        chat_id: Annotated[str, Path(min_length=1, max_length=128)],
        chat_service: Annotated[ChatService, Depends(get_chat_service)],
    ) -> dict[str, object]:
        """Return the chat's durable sub-agent tree."""
        try:
            _, tree = _tree(workspace_id, chat_id, chat_service)
        except Exception as error:
            raise _service_error(error) from error
        return tree.as_dict()

    @router.get("/sub_agents/{sub_agent_id}")
    async def get_chat_sub_agent(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        chat_id: Annotated[str, Path(min_length=1, max_length=128)],
        sub_agent_id: Annotated[str, Path(min_length=1, max_length=128)],
        chat_service: Annotated[ChatService, Depends(get_chat_service)],
    ) -> dict[str, object]:
        """Return one node including its subtree."""
        try:
            _, tree = _tree(workspace_id, chat_id, chat_service)
        except Exception as error:
            raise _service_error(error) from error
        node = tree.get(sub_agent_id)
        if node is None:
            raise _not_found()
        return node.as_dict()

    @router.get("/sub_agents/{sub_agent_id}/sub_agents")
    async def list_chat_sub_agent_children(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        chat_id: Annotated[str, Path(min_length=1, max_length=128)],
        sub_agent_id: Annotated[str, Path(min_length=1, max_length=128)],
        chat_service: Annotated[ChatService, Depends(get_chat_service)],
    ) -> dict[str, object]:
        """Return one node's direct children."""
        try:
            _, tree = _tree(workspace_id, chat_id, chat_service)
        except Exception as error:
            raise _service_error(error) from error
        node = tree.get(sub_agent_id)
        if node is None:
            raise _not_found()
        return {"items": [child.as_dict() for child in node.children]}

    @router.get("/sub_agents/{sub_agent_id}/events")
    async def replay_chat_sub_agent_events(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        chat_id: Annotated[str, Path(min_length=1, max_length=128)],
        sub_agent_id: Annotated[str, Path(min_length=1, max_length=128)],
        chat_service: Annotated[ChatService, Depends(get_chat_service)],
        after_cursor: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int | None, Query(ge=1, le=1_000)] = None,
        include_descendants: Annotated[bool, Query()] = False,
    ) -> dict[str, object]:
        """Replay one node's activity, preserving the execution cursor."""
        from thesis.execution.sub_agents import filter_replay

        try:
            _, tree = _tree(workspace_id, chat_id, chat_service)
            node = tree.get(sub_agent_id)
            if node is None:
                raise _not_found()
            if node.workflow_execution_id is None:
                raise _node_not_executable()
            page = chat_service.replay_events(
                workspace_id, node.workflow_execution_id, after_cursor=after_cursor
            )
        except HTTPException:
            raise
        except Exception as error:
            raise _service_error(error) from error
        scope = (
            tree.subtree_ids(sub_agent_id)
            if include_descendants
            else frozenset({sub_agent_id})
        )
        return filter_replay(page, scope, limit=limit).as_dict()

    @router.get("/sub_agents/{sub_agent_id}/events/stream")
    async def stream_chat_sub_agent_events(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        chat_id: Annotated[str, Path(min_length=1, max_length=128)],
        sub_agent_id: Annotated[str, Path(min_length=1, max_length=128)],
        chat_service: Annotated[ChatService, Depends(get_chat_service)],
        after_cursor: Annotated[int, Query(ge=0)] = 0,
        include_descendants: Annotated[bool, Query()] = False,
        last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
    ) -> StreamingResponse:
        """Stream one node's activity as SSE frames with a resumable cursor."""
        try:
            _, tree = _tree(workspace_id, chat_id, chat_service)
            node = tree.get(sub_agent_id)
            if node is None:
                raise _not_found()
            if node.workflow_execution_id is None:
                raise _node_not_executable()
        except HTTPException:
            raise
        except Exception as error:
            raise _service_error(error) from error
        resume = _resume_cursor(last_event_id, after_cursor)
        scope = (
            tree.subtree_ids(sub_agent_id)
            if include_descendants
            else frozenset({sub_agent_id})
        )
        return StreamingResponse(
            _node_event_stream(
                chat_service,
                workspace_id=workspace_id,
                execution_id=node.workflow_execution_id,
                node_ids=scope,
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

    @router.post("/sub_agents/{sub_agent_id}/messages")
    async def post_chat_sub_agent_message(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        chat_id: Annotated[str, Path(min_length=1, max_length=128)],
        sub_agent_id: Annotated[str, Path(min_length=1, max_length=128)],
        request: ChatSubAgentMessageRequest,
        http_request: Request,
        chat_service: Annotated[ChatService, Depends(get_chat_service)],
    ) -> dict[str, object]:
        """Persist one operator message for a chat-scoped sub-agent.

        The answering actor is derived from the authenticated principal; a
        request whose principal the application does not expose fails closed.
        """
        principal = authenticated_principal(http_request, actor_resolver)
        if principal is None:
            raise _actor_identity_unavailable()
        actor = AnsweringActor(
            actor_id=authenticated_actor_id(principal),
            kind=OperatorActorKind.HUMAN_OPERATOR,
        )
        conversations = _resolve_conversations(http_request)
        if conversations is None:
            raise _conversations_unavailable()
        try:
            # The admission layer above the one intake (REQ-A4): the trusted
            # caller answers through the service, which runs the policy gate,
            # the input-state check, and the resume before the intake is
            # performed on the generalized transport.
            receipt = conversations.answer(
                workspace_id,
                chat_id,
                sub_agent_id,
                body=request.body,
                actor=actor,
            )
        except NodeInputNotFound as error:
            raise _not_found() from error
        except NodeInputNotAccepted as error:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "sub_agent_not_accepting_input",
                    "message": "sub-agent is not accepting input",
                },
            ) from error
        except OperatorPolicyDenied as error:
            raise HTTPException(status_code=403, detail=error.as_dict()) from error
        except Exception as error:
            raise _service_error(error) from error
        result = receipt.message.as_dict()
        result["subAgentId"] = sub_agent_id
        result["operatorPolicy"] = receipt.decision.as_dict()
        return result

    @router.post("/sub_agents/{sub_agent_id}/cancel")
    async def cancel_chat_sub_agent(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        chat_id: Annotated[str, Path(min_length=1, max_length=128)],
        sub_agent_id: Annotated[str, Path(min_length=1, max_length=128)],
        http_request: Request,
        chat_service: Annotated[ChatService, Depends(get_chat_service)],
    ) -> dict[str, object]:
        """Cancel one node and its descendants, then stop its execution.

        The cancelling operator is the authenticated principal the application
        exposes to this router, never a body-supplied actor; a request whose
        principal is not exposed fails closed exactly like the message route.
        Already-terminal nodes and descendants are left untouched and are not
        reported as cancelled, so the affordance is idempotent.
        """
        principal = authenticated_principal(http_request, actor_resolver)
        if principal is None:
            raise _actor_identity_unavailable()
        actor_id = authenticated_actor_id(principal)
        lifecycle = SubAgentLifecycle(chat_service, store=_store_for(chat_service))
        record = lifecycle.get(workspace_id, sub_agent_id)
        if record is None or record.chat_id != chat_id:
            raise _not_found()
        try:
            cancelled = lifecycle.cancel_subtree(workspace_id, sub_agent_id)
            stopped_execution_id = _stop_node_execution(
                chat_service,
                workspace_id,
                record.workflow_execution_id,
                actor_id=actor_id,
            )
        except HTTPException:
            raise
        except Exception as error:
            raise _service_error(error) from error
        return {
            "subAgentId": sub_agent_id,
            "chatId": chat_id,
            "cancelledSubAgentIds": list(cancelled),
            "stoppedExecutionId": stopped_execution_id,
        }

    return router


__all__ = [
    "AUTHENTICATED_PRINCIPAL_STATE_KEY",
    "CONVERSATIONS_STATE_KEY",
    "ActorResolver",
    "ChatSubAgentMessageRequest",
    "authenticated_actor_id",
    "authenticated_principal",
    "create_chat_sub_agent_router",
]
