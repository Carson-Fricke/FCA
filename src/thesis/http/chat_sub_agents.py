"""Chat-scoped REST surface for durable sub-agents.

Sub-agents are attached to a chat, not to a workflow execution, so this router
serves the durable ownership view: it projects the first-class
:class:`~thesis.chat.contracts.SubAgentRecord` store into a nested tree and
exposes list, read, children, and operator-input routes.  Because the tree is a
pure function of durable records, a process restart reconstructs exactly the
same nodes.

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

import hashlib
from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request, status
from pydantic import BaseModel, ConfigDict, Field

from thesis.agent.runtime.delivery import (
    NodeInputNotAccepted,
    NodeInputNotFound,
    NodeInputService,
    TurnScheduler,
)
from thesis.agent.runtime.policy import (
    AnsweringActor,
    OperatorActorKind,
    OperatorPolicyDenied,
    OperatorPolicyRegistry,
)
from thesis.agent.runtime.store import PersistenceSubAgentStore
from thesis.agent.runtime.tree import ChatSubAgentTree, project_chat_sub_agents
from thesis.chat import ChatService
from thesis.http.chat import _service_error, get_chat_service

#: ``app.state`` key holding the callable that runs one more node turn; the
#: callable receives ``(workspace_id, chat_id, sub_agent_id)``.
SUB_AGENT_TURN_STATE_KEY = "sub_agent_turn"

#: ``request.state`` key holding the authenticated principal.  The value is the
#: same string the shared auth dependency returns, for example
#: ``"session:<account-id>"`` for a cookie session.
AUTHENTICATED_PRINCIPAL_STATE_KEY = "authenticated_principal"

#: ``request.state``/``app.state`` key holding an
#: :class:`~thesis.agent.runtime.policy.OperatorPolicyRegistry`.
OPERATOR_POLICIES_STATE_KEY = "operator_policies"

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


def _resolve_policies(
    request: Request, injected: OperatorPolicyRegistry | None
) -> OperatorPolicyRegistry:
    runtime = getattr(request.app.state, OPERATOR_POLICIES_STATE_KEY, None)
    if isinstance(runtime, OperatorPolicyRegistry):
        return runtime
    if injected is not None:
        return injected
    return OperatorPolicyRegistry()


def _resolve_scheduler(request: Request) -> TurnScheduler | None:
    turn = getattr(request.app.state, SUB_AGENT_TURN_STATE_KEY, None)
    return turn if callable(turn) else None


class ChatSubAgentMessageRequest(BaseModel):
    """One operator input message addressed to a chat-scoped sub-agent.

    The answering actor is never accepted here: it is derived from the
    authenticated session by the route.
    """

    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1, max_length=32_768)


def create_chat_sub_agent_router(
    *,
    actor_resolver: ActorResolver | None = None,
    operator_policies: OperatorPolicyRegistry | None = None,
) -> APIRouter:
    """Create the durable, chat-scoped sub-agent router.

    ``actor_resolver`` is the seam for the authenticated principal when the
    application cannot set it on request state.  ``operator_policies`` is the
    runtime-level override map for per-node answering policy; without an
    override every node stays human-only.
    """
    router = APIRouter(
        prefix="/v1/workspaces/{workspace_id}/chats/{chat_id}",
        tags=["sub-agents"],
    )

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
        store = _store_for(chat_service)
        service = NodeInputService(
            chat=chat_service,
            store=store,
            policies=_resolve_policies(http_request, operator_policies),
            schedule=_resolve_scheduler(http_request),
        )
        try:
            receipt = service.accept(
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

    return router


__all__ = [
    "AUTHENTICATED_PRINCIPAL_STATE_KEY",
    "OPERATOR_POLICIES_STATE_KEY",
    "SUB_AGENT_TURN_STATE_KEY",
    "ActorResolver",
    "ChatSubAgentMessageRequest",
    "authenticated_actor_id",
    "authenticated_principal",
    "create_chat_sub_agent_router",
]
