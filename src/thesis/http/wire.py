"""Source-derived record models and HTTP response envelopes.

Record fields come from the durable dataclasses, not a parallel field inventory.
Runtime metadata and tool outcomes retain their canonical runtime schemas.
"""

from __future__ import annotations

from dataclasses import fields
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, get_type_hints

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    TypeAdapter,
    WithJsonSchema,
    create_model,
)
from pydantic.alias_generators import to_camel

from thesis_runtime.contracts.records import (
    ChatMessage,
    ChatThread,
    RecordLinks,
    WorkflowExecution,
)
from thesis_runtime.execution.events import ExecutionEvent
from thesis_runtime.subagents.policy import OperatorActorKind
from thesis_runtime.subagents.tree import SubAgentNodeWire


class RecordWire(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _record_model(
    name: str,
    record: type[ChatMessage]
    | type[ChatThread]
    | type[WorkflowExecution]
    | type[ExecutionEvent],
    *,
    camel: bool = False,
) -> Any:
    hints = get_type_hints(record)
    definitions: dict[str, Any] = {}
    for field in fields(record):
        annotation = hints[field.name]
        if annotation is RecordLinks:
            annotation = Annotated[
                RecordLinks,
                PlainSerializer(
                    lambda links: links.as_dict(), return_type=dict[str, object]
                ),
                WithJsonSchema(
                    TypeAdapter(RecordLinks).json_schema(), mode="serialization"
                ),
            ]
        elif annotation is datetime:
            annotation = Annotated[
                datetime, PlainSerializer(_timestamp, return_type=str)
            ]
        elif annotation == datetime | None:
            annotation = (
                Annotated[datetime, PlainSerializer(_timestamp, return_type=str)] | None
            )
        alias = (
            ("eventType" if field.name == "event_type" else to_camel(field.name))
            if camel
            else field.name
        )
        definitions[field.name] = (annotation, Field(alias=alias))
    return create_model(name, __base__=RecordWire, **definitions)


class ChatMessageWire(_record_model("ChatMessageFields", ChatMessage)):
    """Complete serialized chat record, including persisted hash and timestamps."""


class ChatThreadWire(_record_model("ChatThreadFields", ChatThread)):
    """Complete serialized thread with source-derived links and provenance."""


class ChatThreadListWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: tuple[ChatThreadWire, ...]


class WorkflowExecutionWire(
    _record_model("WorkflowExecutionFields", WorkflowExecution)
):
    """Complete serialized workflow execution record."""


class WorkflowMetadataWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: tuple[WorkflowExecutionWire, ...]
    after: int = Field(ge=0)


class ExecutionEventWire(
    _record_model("ExecutionEventFields", ExecutionEvent, camel=True)
):
    """Execution event fields with the aliases used by ExecutionEvent.as_dict."""


class SteeringExecutionWire(BaseModel):
    model_config = ConfigDict(
        extra="forbid", alias_generator=to_camel, populate_by_name=True
    )

    execution_id: str
    intent_epoch: int


class SteeringPercolationWire(BaseModel):
    """The steering acknowledgement attached to an accepted chat message."""

    model_config = ConfigDict(
        extra="forbid", alias_generator=to_camel, populate_by_name=True
    )

    intent: str | None
    targets_execution_id: str | None
    percolated_to: tuple[SteeringExecutionWire, ...]


class CreateMessageResponseWire(ChatMessageWire):
    steering: SteeringPercolationWire | None = None


class OperatorPolicyDecisionWire(BaseModel):
    """Flattened provenance returned by OperatorPolicyDecision.as_dict()."""

    model_config = ConfigDict(
        extra="forbid", alias_generator=to_camel, populate_by_name=True
    )

    allowed: bool
    actor_id: str
    actor_kind: OperatorActorKind
    allowed_actor_kinds: tuple[OperatorActorKind, ...]
    policy: Literal["human_only", "custom"]
    role: str
    node_id: str | None


class NodeInputReceiptWire(ChatMessageWire):
    """Node-answer HTTP receipt: durable message plus admission provenance."""

    sub_agent_id: str = Field(alias="subAgentId")
    operator_policy: OperatorPolicyDecisionWire = Field(alias="operatorPolicy")


class ChatMessageListWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: tuple[ChatMessageWire, ...]


class SubAgentNodeListWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: tuple[SubAgentNodeWire, ...]


class TimelineEnvelopeWire(BaseModel):
    model_config = ConfigDict(
        extra="forbid", alias_generator=to_camel, populate_by_name=True
    )

    id: str
    chat_id: str
    workspace_id: str
    seq: int = Field(ge=1)
    ts: str
    execution_id: str | None = None
    thread_id: str | None = None


class TimelineMessageWire(TimelineEnvelopeWire):
    kind: Literal["message"]
    payload: ChatMessageWire


class TimelineEventWire(TimelineEnvelopeWire):
    kind: Literal["event"]
    payload: ExecutionEventWire


class TimelineExecutionWire(TimelineEnvelopeWire):
    kind: Literal["execution"]
    payload: WorkflowExecutionWire


class ModelOutputIdentityWire(BaseModel):
    model_config = ConfigDict(
        extra="forbid", alias_generator=to_camel, populate_by_name=True
    )
    invocation_id: str
    message_id: str
    actor_id: str


class ModelOutputStartedWire(ModelOutputIdentityWire):
    state: Literal["started"]
    started_at: str


class ModelOutputDeltaWire(ModelOutputIdentityWire):
    state: Literal["delta"]
    channel: Literal["reasoning", "text"]
    offset: int = Field(ge=0)
    text: str


class ModelOutputInterruptedWire(ModelOutputIdentityWire):
    state: Literal["interrupted"]
    failure: dict[str, object]


class TimelineOutputWire(TimelineEnvelopeWire):
    kind: Literal["output"]
    payload: Annotated[
        ModelOutputStartedWire | ModelOutputDeltaWire | ModelOutputInterruptedWire,
        Field(discriminator="state"),
    ]


type TimelineItemWire = Annotated[
    TimelineMessageWire
    | TimelineEventWire
    | TimelineExecutionWire
    | TimelineOutputWire,
    Field(discriminator="kind"),
]


class TimelinePageWire(BaseModel):
    model_config = ConfigDict(
        extra="forbid", alias_generator=to_camel, populate_by_name=True
    )

    items: tuple[TimelineItemWire, ...]
    next_before: int | None
    after: int = Field(ge=0)
    output_prefixes: tuple[TimelineOutputWire, ...] = Field(
        default=(), alias="outputPrefixes"
    )


class WorkflowSplitWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    branches: dict[str, str]
    join: str


class WorkflowDefinitionWire(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_id: str
    version: str
    title: str
    phases: tuple[str, ...]
    terminal_phases: tuple[str, ...]
    transitions: dict[str, list[str]]
    splits: dict[str, WorkflowSplitWire]
