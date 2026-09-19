"""Thin HTTP adapter for the workspace chat service.

The router only translates validated HTTP payloads into service calls.  It has
no knowledge of CLEO, queues, storage engines, or execution implementation
details; applications inject a :class:`ChatService` through app state.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    HTTPException,
    Path,
    Query,
    Request,
)
from pydantic import BaseModel, ConfigDict, Field

from thesis.chat import (
    ChatService,
    CrossWorkspaceLinkError,
    InvalidLifecycleTransition,
    MessageRole,
    MessageState,
    RecordNotFound,
)


class ChatHTTPModel(BaseModel):
    """Strict request and response boundary for later UI clients."""

    model_config = ConfigDict(extra="forbid")


class CreateChatRequest(ChatHTTPModel):
    title: str = Field(min_length=1, max_length=256)
    owner_actor_id: str = Field(min_length=1, max_length=128)
    investigation_ids: tuple[str, ...] = ()
    strategy_ids: tuple[str, ...] = ()
    experiment_ids: tuple[str, ...] = ()


class CreateInvestigationRequest(ChatHTTPModel):
    title: str = Field(min_length=1, max_length=256)
    objective: str = Field(min_length=1, max_length=8_192)
    owner_actor_id: str = Field(min_length=1, max_length=128)
    chat_ids: tuple[str, ...] = ()
    strategy_ids: tuple[str, ...] = ()
    experiment_ids: tuple[str, ...] = ()


class CreateMessageRequest(ChatHTTPModel):
    actor_id: str = Field(min_length=1, max_length=128)
    body: str = Field(min_length=1, max_length=32_768)
    role: MessageRole = MessageRole.USER
    attachment_ids: tuple[str, ...] = ()
    investigation_ids: tuple[str, ...] = ()
    execution_id: str | None = None
    intent_epoch: int = Field(default=0, ge=0)
    targets_execution_id: str | None = None
    steering_intent: str | None = Field(default=None, max_length=256)


class MessageStateRequest(ChatHTTPModel):
    state: MessageState


class CreateAttachmentRequest(ChatHTTPModel):
    message_id: str = Field(min_length=1, max_length=128)
    actor_id: str = Field(min_length=1, max_length=128)
    filename: str = Field(min_length=1, max_length=512)
    media_type: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=0)


class UploadAttachmentRequest(ChatHTTPModel):
    content_uri: str = Field(min_length=1, max_length=2_048)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ScanAttachmentRequest(ChatHTTPModel):
    available: bool
    error_message: str | None = Field(default=None, max_length=2_048)


class SteerWorkflowRequest(ChatHTTPModel):
    actor_id: str = Field(min_length=1, max_length=128)
    body: str = Field(min_length=1, max_length=32_768)
    message_id: str | None = None


class ActorRequest(ChatHTTPModel):
    actor_id: str = Field(min_length=1, max_length=128)


class ToolResultRequest(ChatHTTPModel):
    result: dict[str, object] | None = None
    actor_id: str | None = None
    success: bool = True
    error_message: str | None = Field(default=None, max_length=2_048)
    intent_epoch: int | None = Field(default=None, ge=0)


class SubmitToolJobRequest(ChatHTTPModel):
    tool_name: str = Field(min_length=1, max_length=128)
    actor_id: str = Field(min_length=1, max_length=128)
    correlation_id: str | None = Field(default=None, max_length=128)
    investigation_id: str | None = None
    strategy_ids: tuple[str, ...] = ()
    experiment_ids: tuple[str, ...] = ()


def get_chat_service(request: Request) -> ChatService:
    """Resolve the service from app state so applications control persistence."""
    service = getattr(request.app.state, "chat_service", None)
    if service is None:
        service = ChatService()
        request.app.state.chat_service = service
    if not isinstance(service, ChatService):
        raise RuntimeError("app.state.chat_service must be a ChatService")
    return service


def _service_error(error: Exception) -> HTTPException:
    if isinstance(error, RecordNotFound):
        return HTTPException(status_code=404, detail=str(error))
    if isinstance(error, (CrossWorkspaceLinkError, InvalidLifecycleTransition)):
        return HTTPException(status_code=409, detail=str(error))
    if isinstance(error, ValueError):
        return HTTPException(status_code=422, detail=str(error))
    return HTTPException(status_code=500, detail="chat service request failed")


def create_chat_router() -> APIRouter:
    """Create the UI-facing router.

    The router resolves the service from ``app.state.chat_service`` so
    applications control persistence by setting the service on app state.
    """
    router = APIRouter(prefix="/v1/workspaces/{workspace_id}", tags=["chat"])

    @router.post("/chats")
    async def create_chat(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        request: CreateChatRequest,
        chat_service: Annotated[ChatService, Depends(get_chat_service)],
    ) -> dict[str, object]:
        try:
            chat = chat_service.create_chat(
                workspace_id,
                title=request.title,
                owner_actor_id=request.owner_actor_id,
                investigation_ids=request.investigation_ids,
                strategy_ids=request.strategy_ids,
                experiment_ids=request.experiment_ids,
            )
        except Exception as error:
            raise _service_error(error) from error
        return chat.as_dict()

    @router.get("/chats")
    async def list_chats(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        chat_service: Annotated[ChatService, Depends(get_chat_service)],
    ) -> dict[str, object]:
        return {
            "items": [chat.as_dict() for chat in chat_service.list_chats(workspace_id)]
        }

    @router.get("/chats/{chat_id}")
    async def get_chat(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        chat_id: Annotated[str, Path(min_length=1, max_length=128)],
        chat_service: Annotated[ChatService, Depends(get_chat_service)],
    ) -> dict[str, object]:
        try:
            return chat_service.get_chat(workspace_id, chat_id).as_dict()
        except Exception as error:
            raise _service_error(error) from error

    @router.get("/chats/{chat_id}/execution")
    async def get_chat_execution(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        chat_id: Annotated[str, Path(min_length=1, max_length=128)],
        chat_service: Annotated[ChatService, Depends(get_chat_service)],
    ) -> dict[str, object] | None:
        """Return the chat's most recent workflow execution, or null.

        The UI hydrates a chat's execution timeline from this route; a chat
        with no launched workflow answers null rather than 404 so the absence
        is not logged as an error.
        """
        try:
            chat_service.get_chat(workspace_id, chat_id)
            matches = [
                execution
                for execution in chat_service.list_executions(workspace_id)
                if execution.chat_id == chat_id
            ]
        except Exception as error:
            raise _service_error(error) from error
        if not matches:
            return None
        return max(matches, key=lambda execution: execution.created_at).as_dict()

    @router.post("/chats/{chat_id}/archive")
    async def archive_chat(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        chat_id: Annotated[str, Path(min_length=1, max_length=128)],
        chat_service: Annotated[ChatService, Depends(get_chat_service)],
    ) -> dict[str, object]:
        try:
            chat = chat_service.archive_chat(workspace_id, chat_id)
        except Exception as error:
            raise _service_error(error) from error
        return chat.as_dict()

    @router.delete("/chats/{chat_id}", status_code=204)
    async def delete_chat(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        chat_id: Annotated[str, Path(min_length=1, max_length=128)],
        chat_service: Annotated[ChatService, Depends(get_chat_service)],
    ) -> None:
        try:
            chat_service.delete_chat(workspace_id, chat_id)
        except Exception as error:
            raise _service_error(error) from error

    @router.post("/chats/{chat_id}/messages")
    async def create_message(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        chat_id: Annotated[str, Path(min_length=1, max_length=128)],
        request: CreateMessageRequest,
        background_tasks: BackgroundTasks,
        http_request: Request,
        chat_service: Annotated[ChatService, Depends(get_chat_service)],
    ) -> dict[str, object]:
        """Post a chat message and percolate steering when fields are present.

        Steering is expressed only through chat messages: the persisted
        message is the durable acknowledgement, and the service bumps the
        intent epoch of the targeted (or all active) executions so the
        orchestrator replans at the next safe model or tool boundary.
        """
        steering = (
            request.targets_execution_id is not None
            or request.steering_intent is not None
        )
        try:
            if steering:
                if (
                    request.execution_id is not None
                    and request.execution_id != request.targets_execution_id
                ):
                    raise ValueError("execution_id conflicts with targets_execution_id")
                percolation = chat_service.post_steering_message(
                    workspace_id,
                    chat_id,
                    actor_id=request.actor_id,
                    body=request.body,
                    role=request.role,
                    attachment_ids=request.attachment_ids,
                    investigation_ids=request.investigation_ids or None,
                    targets_execution_id=request.targets_execution_id,
                    steering_intent=request.steering_intent,
                )
            else:
                message = chat_service.create_message(
                    workspace_id,
                    chat_id,
                    actor_id=request.actor_id,
                    body=request.body,
                    role=request.role,
                    attachment_ids=request.attachment_ids,
                    investigation_ids=request.investigation_ids,
                    execution_id=request.execution_id,
                    intent_epoch=request.intent_epoch,
                )
        except Exception as error:
            raise _service_error(error) from error
        if not steering:
            reply_service = getattr(http_request.app.state, "chat_reply_service", None)
            if reply_service is not None and request.role is MessageRole.USER:
                background_tasks.add_task(reply_service.respond, workspace_id, chat_id)
            return message.as_dict()
        result = percolation.message.as_dict()
        result["steering"] = {
            "intent": request.steering_intent,
            "targetsExecutionId": request.targets_execution_id,
            "percolatedTo": [
                {
                    "executionId": execution.execution_id,
                    "intentEpoch": execution.intent_epoch,
                }
                for execution in percolation.executions
            ],
        }
        return result

    @router.get("/chats/{chat_id}/messages")
    async def list_messages(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        chat_id: Annotated[str, Path(min_length=1, max_length=128)],
        chat_service: Annotated[ChatService, Depends(get_chat_service)],
    ) -> dict[str, object]:
        try:
            messages = chat_service.list_messages(workspace_id, chat_id)
        except Exception as error:
            raise _service_error(error) from error
        return {"items": [message.as_dict() for message in messages]}

    @router.post("/chats/{chat_id}/messages/{message_id}/state")
    async def transition_message(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        chat_id: Annotated[str, Path(min_length=1, max_length=128)],
        message_id: Annotated[str, Path(min_length=1, max_length=128)],
        request: MessageStateRequest,
        chat_service: Annotated[ChatService, Depends(get_chat_service)],
    ) -> dict[str, object]:
        try:
            message = chat_service.get_message(workspace_id, message_id)
            if message.chat_id != chat_id:
                raise CrossWorkspaceLinkError(
                    "message does not belong to the selected chat"
                )
            message = chat_service.transition_message(
                workspace_id, message_id, request.state
            )
        except Exception as error:
            raise _service_error(error) from error
        return message.as_dict()

    @router.post("/chats/{chat_id}/attachments")
    async def create_attachment(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        chat_id: Annotated[str, Path(min_length=1, max_length=128)],
        request: CreateAttachmentRequest,
        chat_service: Annotated[ChatService, Depends(get_chat_service)],
    ) -> dict[str, object]:
        try:
            attachment = chat_service.create_attachment(
                workspace_id,
                chat_id,
                request.message_id,
                actor_id=request.actor_id,
                filename=request.filename,
                media_type=request.media_type,
                size_bytes=request.size_bytes,
            )
        except Exception as error:
            raise _service_error(error) from error
        return attachment.as_dict()

    @router.get("/chats/{chat_id}/attachments")
    async def list_attachments(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        chat_id: Annotated[str, Path(min_length=1, max_length=128)],
        chat_service: Annotated[ChatService, Depends(get_chat_service)],
    ) -> dict[str, object]:
        try:
            attachments = chat_service.list_attachments(workspace_id, chat_id)
        except Exception as error:
            raise _service_error(error) from error
        return {"items": [item.as_dict() for item in attachments]}

    @router.post("/attachments/{attachment_id}/upload")
    async def upload_attachment(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        attachment_id: Annotated[str, Path(min_length=1, max_length=128)],
        request: UploadAttachmentRequest,
        chat_service: Annotated[ChatService, Depends(get_chat_service)],
    ) -> dict[str, object]:
        try:
            attachment = chat_service.upload_attachment(
                workspace_id,
                attachment_id,
                content_uri=request.content_uri,
                sha256=request.sha256,
            )
        except Exception as error:
            raise _service_error(error) from error
        return attachment.as_dict()

    @router.post("/attachments/{attachment_id}/scan")
    async def scan_attachment(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        attachment_id: Annotated[str, Path(min_length=1, max_length=128)],
        request: ScanAttachmentRequest,
        chat_service: Annotated[ChatService, Depends(get_chat_service)],
    ) -> dict[str, object]:
        try:
            attachment = chat_service.scan_attachment(
                workspace_id,
                attachment_id,
                available=request.available,
                error_message=request.error_message,
            )
        except Exception as error:
            raise _service_error(error) from error
        return attachment.as_dict()

    @router.post("/investigations")
    async def create_investigation(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        request: CreateInvestigationRequest,
        chat_service: Annotated[ChatService, Depends(get_chat_service)],
    ) -> dict[str, object]:
        try:
            investigation = chat_service.create_investigation(
                workspace_id,
                title=request.title,
                objective=request.objective,
                owner_actor_id=request.owner_actor_id,
                chat_ids=request.chat_ids,
                strategy_ids=request.strategy_ids,
                experiment_ids=request.experiment_ids,
            )
        except Exception as error:
            raise _service_error(error) from error
        return investigation.as_dict()

    @router.get("/investigations")
    async def list_investigations(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        chat_service: Annotated[ChatService, Depends(get_chat_service)],
    ) -> dict[str, object]:
        return {
            "items": [
                item.as_dict()
                for item in chat_service.list_investigations(workspace_id)
            ]
        }

    @router.get("/investigations/{investigation_id}")
    async def get_investigation(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        investigation_id: Annotated[str, Path(min_length=1, max_length=128)],
        chat_service: Annotated[ChatService, Depends(get_chat_service)],
    ) -> dict[str, object]:
        try:
            investigation = chat_service.get_investigation(
                workspace_id, investigation_id
            )
        except Exception as error:
            raise _service_error(error) from error
        return investigation.as_dict()

    @router.post(
        "/executions/{execution_id}/steer",
        deprecated=True,
        summary="Post a steering message into the execution's chat",
    )
    async def steer_workflow(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        execution_id: Annotated[str, Path(min_length=1, max_length=128)],
        request: SteerWorkflowRequest,
        chat_service: Annotated[ChatService, Depends(get_chat_service)],
    ) -> dict[str, object]:
        """Deprecated chat-scoped alias.

        All steering enters through chat messages; this route remains only so
        transcripts hold every steering act.  It posts a steering message into
        the execution's originating chat, which durably acknowledges the
        intent and bumps the execution's intent epoch.
        """
        try:
            execution = chat_service.steer_workflow(
                workspace_id,
                execution_id,
                actor_id=request.actor_id,
                body=request.body,
                message_id=request.message_id,
            )
        except Exception as error:
            raise _service_error(error) from error
        return execution.as_dict()

    @router.post("/executions/{execution_id}/start")
    async def start_workflow(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        execution_id: Annotated[str, Path(min_length=1, max_length=128)],
        request: ActorRequest,
        chat_service: Annotated[ChatService, Depends(get_chat_service)],
    ) -> dict[str, object]:
        try:
            execution = chat_service.start_workflow(
                workspace_id, execution_id, actor_id=request.actor_id
            )
        except Exception as error:
            raise _service_error(error) from error
        return execution.as_dict()

    @router.post("/executions/{execution_id}/stop")
    async def stop_workflow(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        execution_id: Annotated[str, Path(min_length=1, max_length=128)],
        request: ActorRequest,
        chat_service: Annotated[ChatService, Depends(get_chat_service)],
    ) -> dict[str, object]:
        try:
            execution = chat_service.stop_workflow(
                workspace_id, execution_id, actor_id=request.actor_id
            )
        except Exception as error:
            raise _service_error(error) from error
        return execution.as_dict()

    @router.post("/executions/{execution_id}/cancel")
    async def cancel_workflow(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        execution_id: Annotated[str, Path(min_length=1, max_length=128)],
        request: ActorRequest,
        chat_service: Annotated[ChatService, Depends(get_chat_service)],
    ) -> dict[str, object]:
        try:
            execution = chat_service.cancel_workflow(
                workspace_id, execution_id, actor_id=request.actor_id
            )
        except Exception as error:
            raise _service_error(error) from error
        return execution.as_dict()

    @router.get("/executions")
    async def list_executions(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        chat_service: Annotated[ChatService, Depends(get_chat_service)],
    ) -> dict[str, object]:
        return {
            "items": [
                execution.as_dict()
                for execution in chat_service.list_executions(workspace_id)
            ]
        }

    @router.get("/executions/{execution_id}")
    async def get_execution(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        execution_id: Annotated[str, Path(min_length=1, max_length=128)],
        chat_service: Annotated[ChatService, Depends(get_chat_service)],
    ) -> dict[str, object]:
        try:
            execution = chat_service.get_execution(workspace_id, execution_id)
        except Exception as error:
            raise _service_error(error) from error
        return execution.as_dict()

    @router.post("/executions/{execution_id}/tools")
    async def submit_tool_job(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        execution_id: Annotated[str, Path(min_length=1, max_length=128)],
        request: SubmitToolJobRequest,
        chat_service: Annotated[ChatService, Depends(get_chat_service)],
    ) -> dict[str, object]:
        try:
            job = chat_service.submit_tool_job(
                workspace_id,
                execution_id,
                tool_name=request.tool_name,
                actor_id=request.actor_id,
                correlation_id=request.correlation_id,
                investigation_id=request.investigation_id,
                strategy_ids=request.strategy_ids,
                experiment_ids=request.experiment_ids,
            )
        except Exception as error:
            raise _service_error(error) from error
        return job.as_dict()

    @router.get("/executions/{execution_id}/events")
    async def replay_events(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        execution_id: Annotated[str, Path(min_length=1, max_length=128)],
        chat_service: Annotated[ChatService, Depends(get_chat_service)],
        after_cursor: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int | None, Query(ge=1, le=1_000)] = None,
    ) -> dict[str, object]:
        try:
            replay = chat_service.replay_events(
                workspace_id,
                execution_id,
                after_cursor=after_cursor,
                limit=limit,
            )
        except Exception as error:
            raise _service_error(error) from error
        return replay.as_dict()

    @router.post("/tool-jobs/{tool_job_id}/result")
    async def record_tool_result(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        tool_job_id: Annotated[str, Path(min_length=1, max_length=128)],
        request: ToolResultRequest,
        chat_service: Annotated[ChatService, Depends(get_chat_service)],
    ) -> dict[str, object]:
        try:
            job = chat_service.record_tool_result(
                workspace_id,
                tool_job_id,
                result=request.result,
                actor_id=request.actor_id,
                success=request.success,
                error_message=request.error_message,
                intent_epoch=request.intent_epoch,
            )
        except Exception as error:
            raise _service_error(error) from error
        return job.as_dict()

    @router.post("/tool-jobs/{tool_job_id}/start")
    async def start_tool_job(
        workspace_id: Annotated[str, Path(min_length=1, max_length=128)],
        tool_job_id: Annotated[str, Path(min_length=1, max_length=128)],
        request: ActorRequest,
        chat_service: Annotated[ChatService, Depends(get_chat_service)],
    ) -> dict[str, object]:
        try:
            job = chat_service.start_tool_job(
                workspace_id, tool_job_id, actor_id=request.actor_id
            )
        except Exception as error:
            raise _service_error(error) from error
        return job.as_dict()

    return router


__all__ = [
    "ActorRequest",
    "ChatHTTPModel",
    "CreateAttachmentRequest",
    "CreateChatRequest",
    "CreateInvestigationRequest",
    "CreateMessageRequest",
    "MessageStateRequest",
    "ScanAttachmentRequest",
    "SteerWorkflowRequest",
    "SubmitToolJobRequest",
    "ToolResultRequest",
    "UploadAttachmentRequest",
    "create_chat_router",
    "get_chat_service",
]
