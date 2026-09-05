"""UI-facing Thesis HTTP contract composition."""

from thesis.http.application import (
    CombinedAuthenticator,
    EntraJwtAuthenticator,
    create_app,
)
from thesis.http.chat import (
    ActorRequest,
    ChatHTTPModel,
    CreateAttachmentRequest,
    CreateChatRequest,
    CreateInvestigationRequest,
    CreateMessageRequest,
    MessageStateRequest,
    ScanAttachmentRequest,
    SteerWorkflowRequest,
    SubmitToolJobRequest,
    ToolResultRequest,
    UploadAttachmentRequest,
    WorkflowRequest,
    create_chat_router,
    get_chat_service,
)
from thesis.http.streaming import create_streaming_router

__all__ = [
    "ActorRequest",
    "ChatHTTPModel",
    "CombinedAuthenticator",
    "CreateAttachmentRequest",
    "CreateChatRequest",
    "CreateInvestigationRequest",
    "CreateMessageRequest",
    "EntraJwtAuthenticator",
    "MessageStateRequest",
    "ScanAttachmentRequest",
    "SteerWorkflowRequest",
    "SubmitToolJobRequest",
    "ToolResultRequest",
    "UploadAttachmentRequest",
    "WorkflowRequest",
    "create_app",
    "create_chat_router",
    "create_streaming_router",
    "get_chat_service",
]
