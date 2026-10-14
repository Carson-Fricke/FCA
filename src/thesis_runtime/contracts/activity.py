"""Immutable runtime activity metadata and its trusted wire codec."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, ClassVar, Literal, cast

from pydantic import ConfigDict, Field, TypeAdapter
from pydantic.alias_generators import to_camel
from pydantic.json_schema import JsonSchemaValue

from thesis_runtime.contracts.records import ChatMessage


def _activity_alias(name: str) -> str:
    return {"kind": "activityKind", "stage": "toolActivity"}.get(name, to_camel(name))


_ACTIVITY_CONFIG = ConfigDict(
    alias_generator=_activity_alias,
    populate_by_name=True,
    json_schema_extra={"additionalProperties": True},
)


@dataclass(frozen=True, slots=True)
class ExternalInput:
    __pydantic_config__: ClassVar[ConfigDict] = _ACTIVITY_CONFIG
    sub_agent_id: str | None = None
    kind: Literal["external_input"] = "external_input"


@dataclass(frozen=True, slots=True)
class ModelOutput:
    __pydantic_config__: ClassVar[ConfigDict] = _ACTIVITY_CONFIG
    sub_agent_id: str | None = None
    invocation_id: str | None = None
    kind: Literal["model_output"] = "model_output"


@dataclass(frozen=True, slots=True)
class RuntimeTool:
    __pydantic_config__: ClassVar[ConfigDict] = _ACTIVITY_CONFIG
    sub_agent_id: str | None = None
    stage: Literal["started", "finished"] = "started"
    tool_call_id: str | None = None
    kind: Literal["runtime_tool"] = "runtime_tool"


@dataclass(frozen=True, slots=True)
class Delivery:
    __pydantic_config__: ClassVar[ConfigDict] = _ACTIVITY_CONFIG
    sub_agent_id: str | None = None
    delivery_key: str | None = None
    kind: Literal["delivery"] = "delivery"


type Activity = ExternalInput | ModelOutput | RuntimeTool | Delivery
type RuntimeActivity = Annotated[Activity, Field(discriminator="kind")]

RUNTIME_ACTIVITY_ADAPTER = TypeAdapter(RuntimeActivity)
CHAT_MESSAGE_ADAPTER = TypeAdapter(ChatMessage)


def runtime_activity_schema() -> JsonSchemaValue:
    """Generate metadata schema allowing the writer's extra provenance fields."""
    return RUNTIME_ACTIVITY_ADAPTER.json_schema(by_alias=True, mode="serialization")


def chat_message_schema() -> JsonSchemaValue:
    """Generate the record wire schema with canonical runtime metadata definitions."""
    schema = CHAT_MESSAGE_ADAPTER.json_schema(mode="serialization")
    activity = runtime_activity_schema()
    schema.setdefault("$defs", {}).update(activity.pop("$defs", {}))
    schema["properties"]["data"] = {"anyOf": [activity, {"type": "null"}]}
    return schema


def decode_activity(message: ChatMessage) -> Activity:
    """Decode the explicit current discriminator; reject untagged old metadata."""
    metadata = dict(message.data or {})
    metadata.pop(
        "kind", None
    )  # Extension provenance is not the activity discriminator.
    return RUNTIME_ACTIVITY_ADAPTER.validate_python(metadata, by_name=False)


def activity_data(activity: Activity) -> dict[str, object]:
    """Encode the timeline activity wire from typed trusted metadata."""
    return cast(
        dict[str, object],
        RUNTIME_ACTIVITY_ADAPTER.dump_python(
            activity,
            mode="json",
            by_alias=True,
            fallback=lambda value: dict(value) if isinstance(value, Mapping) else value,
        ),
    )


def owns_anchor(message: ChatMessage, sub_agent_id: str | None) -> bool:
    return decode_activity(message).sub_agent_id == sub_agent_id


def is_external_input(message: ChatMessage) -> bool:
    return isinstance(decode_activity(message), ExternalInput | Delivery)
