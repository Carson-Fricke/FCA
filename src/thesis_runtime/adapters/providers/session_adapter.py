"""Pure native protocol projection at the provider boundary, not the runtime."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import closing
from typing import Protocol, cast
from uuid import uuid4

from thesis_runtime.adapters.providers.families.base import (
    ModelFamilyAdapter,
    NativeToolsAdapter,
)
from thesis_runtime.adapters.providers.invocations import (
    ChatMessage,
    ModelCompletion,
    ProviderRejectedError,
    Role,
    ToolCall,
    content_digest,
)
from thesis_runtime.contracts.messages import (
    MessageContent,
    SessionMessage,
    ToolCorrelation,
    ToolRequest,
)
from thesis_runtime.contracts.output import OutputDelta
from thesis_runtime.contracts.records import normalise_reasoning


class NativeProvider(Protocol):
    """Native stream parsing and its explicit, immutable transport-streaming option."""

    @property
    def streaming(self) -> bool:
        """Whether the provider's actual request enables native streaming."""
        ...

    def stream(
        self,
        *,
        messages: Sequence[ChatMessage],
        role: Role = "primary",
        tools: Sequence[Mapping[str, object]] | None = None,
        response_format: Mapping[str, object] | None = None,
    ) -> Generator[OutputDelta | ModelCompletion, None, None]: ...


def _mapping_json(value: object) -> dict[str, object]:
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError("provider request must contain only JSON values")


def project_native_messages(
    messages: Sequence[SessionMessage],
) -> tuple[ChatMessage, ...]:
    """Project an immutable, already-scoped arrival sequence into native history.

    Only exact explicit correlations join. Complete groups emit contiguous native
    responses, deferring interleaved inputs until the group is closed. Incomplete
    or ambiguous groups remain ordinary runtime evidence in arrival order; no
    execution, response, scheduling, persistence, or authority is invented.
    Instruction authority comes only from the trusted port's explicit direction,
    never from content source, actor labels, or legacy persisted roles.
    """
    scope = tuple(messages)
    if len({message.message_id for message in scope}) != len(scope):
        raise ValueError("session message IDs must be unique within projection scope")
    counts = Counter(
        request.id for message in scope for request in message.content.requests
    )
    requests: dict[ToolCorrelation, int] = {}
    for index, message in enumerate(scope):
        for request in message.content.requests:
            requests[ToolCorrelation(message.message_id, request.id, request.name)] = (
                index
            )
    results: dict[ToolCorrelation, list[int]] = defaultdict(list)
    for index, message in enumerate(scope):
        correlation = message.content.correlation
        if (
            message.direction == "incoming"
            and correlation is not None
            and correlation in requests
            and index > requests[correlation]
        ):
            results[correlation].append(index)

    # Preserve unique provider IDs. Reuse cannot survive native normalizers,
    # so ambiguous envelopes degrade rather than changing durable identities.
    groups: dict[int, tuple[ToolCorrelation, ...]] = {}
    consumed: set[int] = set()
    for index, message in enumerate(scope):
        calls = message.content.requests
        if not calls or any(counts[request.id] != 1 for request in calls):
            continue
        correlations = tuple(
            ToolCorrelation(message.message_id, request.id, request.name)
            for request in calls
        )
        if all(results.get(correlation) for correlation in correlations):
            groups[index] = correlations
            consumed.update(
                result_index
                for correlation in correlations
                for result_index in results[correlation]
            )

    projected: list[ChatMessage] = []
    deferred: list[ChatMessage] = []
    defer_until = -1
    for index, message in enumerate(scope):
        if index in consumed:
            native: list[ChatMessage] = []
        elif index in groups:
            native = [
                ChatMessage(
                    role="assistant",
                    content=message.content.text,
                    tool_calls=tuple(
                        ToolCall(request.id, request.name, request.arguments)
                        for request in message.content.requests
                    ),
                )
            ]
            for correlation in groups[index]:
                datums = [scope[item].content for item in results[correlation]]
                content = datums[0].as_content()
                if len(datums) > 1:
                    content = json.dumps(
                        {
                            "datums": [
                                json.loads(datum.as_content()) for datum in datums
                            ]
                        },
                        ensure_ascii=True,
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                native.append(
                    ChatMessage(
                        role="tool",
                        content=content,
                        tool_call_id=correlation.request_id,
                    )
                )
            defer_until = max(
                defer_until,
                *(
                    item
                    for correlation in groups[index]
                    for item in results[correlation]
                ),
            )
        elif message.content.requests:
            reason = (
                "duplicate/reused request IDs"
                if any(counts[request.id] != 1 for request in message.content.requests)
                else "missing results"
            )
            native = [
                ChatMessage(
                    role="user",
                    content=(
                        f"Incomplete request group ({reason}) / runtime evidence:\n"
                        + json.dumps(
                            message.as_dict(),
                            ensure_ascii=True,
                            separators=(",", ":"),
                            sort_keys=True,
                        )
                    ),
                )
            ]
        else:
            role = {
                "incoming": "user",
                "outgoing": "assistant",
                "instruction": "system",
            }[message.direction]
            content = message.content.text
            result = message.content.result or {}
            if message.direction == "outgoing" and result.get("interrupted") is True:
                # A sealed interruption is canonical evidence, not a completed
                # answer or an executable tool envelope. Preserve its reasoning
                # in ordinary context for providers without a reasoning replay
                # channel; live execution and replay share this pure projection.
                content = (
                    (
                        "Model output interrupted by provider failure; partial evidence only.\n"
                        if result.get("failure") is not None
                        else "Model output interrupted by operator steering.\n"
                    )
                    + "Reasoning so far:\n"
                    + str(result.get("reasoning") or "")
                    + "\nPartial answer:\n"
                    + content
                )
            native = [
                ChatMessage(
                    role=role,
                    content=(
                        content
                        if message.direction != "incoming"
                        and message.content.text.strip()
                        else message.content.as_content()
                    ),
                )
            ]
        if index in groups and not deferred:
            projected.extend(native)
        else:
            deferred.extend(native)
        if index >= defer_until:
            projected.extend(deferred)
            deferred.clear()
    projected.extend(deferred)
    return tuple(projected)


class NativeSessionAdapter:
    """Keep native completions behind a canonical session-to-session boundary.

    The caller reserves the output ID before execution; synchronous consumers
    allocate a fresh ID at stream start through the ID factory. Reasoning
    stays separate from answer text in optional ``content.result['reasoning']``;
    usage and native protocol metadata do not become session fields.
    """

    __slots__ = ("_family", "_id_factory", "_provider")

    def __init__(
        self,
        provider: NativeProvider,
        *,
        family: ModelFamilyAdapter | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._provider = provider
        self._family = family if family is not None else NativeToolsAdapter()
        self._id_factory = id_factory if id_factory is not None else lambda: uuid4().hex

    def request_snapshot(
        self,
        *,
        messages: Sequence[SessionMessage],
        role: Role = "primary",
        tools: Sequence[Mapping[str, object]] | None = None,
        response_format: Mapping[str, object] | None = None,
    ) -> Mapping[str, object]:
        """Prepare detached protocol audit data without invoking any effects.

        This is a versioned provider request, not a canonical session message.
        The caller owns persistence before invocation starts. No IDs are created.
        """
        native_messages: list[dict[str, object]] = []
        for message in project_native_messages(messages):
            entry: dict[str, object] = {
                "role": message.role,
                "content": message.content,
            }
            if message.tool_calls:
                entry["tool_calls"] = [
                    request.as_wire() for request in message.tool_calls
                ]
            if message.tool_call_id is not None:
                entry["tool_call_id"] = message.tool_call_id
            native_messages.append(entry)
        snapshot = json.loads(
            json.dumps(
                {
                    "projection": "native-session-v2",
                    "messages": native_messages,
                    "role": role,
                    "tools": [
                        {"type": "function", "function": dict(tool)}
                        for tool in self._family.prepare_tools(tools)
                    ]
                    if tools is not None
                    else None,
                    "response_format": response_format,
                    "stream": self._provider.streaming,
                },
                default=_mapping_json,
                allow_nan=False,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return {
            **snapshot,
            "model_family": self._family.family,
            "family_adapter_version": self._family.version,
            "catalog_version": "sha256:" + content_digest(snapshot["tools"]),
        }

    def complete(
        self,
        *,
        messages: Sequence[SessionMessage],
        role: Role = "primary",
        tools: Sequence[Mapping[str, object]] | None = None,
        response_format: Mapping[str, object] | None = None,
    ) -> SessionMessage:
        with closing(
            self.stream(
                messages=messages,
                role=role,
                tools=tools,
                response_format=response_format,
            )
        ) as events:
            for event in events:
                if isinstance(event, SessionMessage):
                    return event
        raise ProviderRejectedError("provider stream omitted its completion")

    def stream(
        self,
        *,
        messages: Sequence[SessionMessage],
        message_id: str | None = None,
        role: Role = "primary",
        tools: Sequence[Mapping[str, object]] | None = None,
        response_format: Mapping[str, object] | None = None,
    ) -> Generator[OutputDelta | SessionMessage, None, None]:
        scope = tuple(messages)
        if message_id is None:
            message_id = self._id_factory()
        if (
            not isinstance(message_id, str)
            or not message_id.strip()
            or len(message_id) > 128
        ):
            raise ValueError(
                "completion message ID must be non-empty text of at most 128 characters"
            )
        if any(message.message_id == message_id for message in scope):
            raise ValueError("completion message ID must be fresh within session scope")
        snapshot = self.request_snapshot(
            messages=scope, role=role, tools=tools, response_format=response_format
        )
        native_messages: list[ChatMessage] = []
        for entry in cast(list[dict[str, object]], snapshot["messages"]):
            calls: list[ToolCall] = []
            for request in cast(list[dict[str, object]], entry.get("tool_calls", [])):
                function = cast(dict[str, str], request["function"])
                calls.append(
                    ToolCall(
                        cast(str, request["id"]),
                        function["name"],
                        function["arguments"],
                    )
                )
            native_messages.append(
                ChatMessage(
                    role=cast(str, entry["role"]),
                    content=cast(str, entry["content"]),
                    tool_calls=tuple(calls),
                    tool_call_id=cast(str | None, entry.get("tool_call_id")),
                )
            )
        events = self._provider.stream(
            messages=tuple(native_messages),
            role=cast(Role, snapshot["role"]),
            tools=cast(list[Mapping[str, object]] | None, snapshot["tools"]),
            response_format=cast(
                Mapping[str, object] | None, snapshot["response_format"]
            ),
        )
        completion: ModelCompletion | None = None
        with closing(events):
            for event in events:
                if completion is not None:
                    raise ProviderRejectedError(
                        "provider emitted output after completion"
                    )
                if isinstance(event, OutputDelta):
                    yield event
                elif isinstance(event, ModelCompletion):
                    completion = event
                else:
                    raise ProviderRejectedError(
                        "provider emitted an invalid completion"
                    )
        if completion is None:
            raise ProviderRejectedError("provider stream ended without completion")
        if not completion.content.strip() and not completion.tool_calls:
            raise ProviderRejectedError("provider returned no answer or tool requests")
        reasoning = normalise_reasoning(completion.reasoning)
        result: dict[str, object] = {"reasoning": reasoning} if reasoning else {}
        if (
            completion.upstream_provider is not None
            or completion.generation_id is not None
        ):
            result["provider_metadata"] = {
                "model": completion.model,
                "provider": completion.provider,
                "upstream_provider": completion.upstream_provider,
                "generation_id": completion.generation_id,
                "model_family": self._family.family,
                "family_adapter_version": self._family.version,
            }
        yield SessionMessage(
            message_id=message_id,
            direction="outgoing",
            content=MessageContent(
                text=completion.content,
                source="agent",
                result=result or None,
                requests=tuple(
                    ToolRequest.from_provider(request)
                    for request in self._family.decode_calls(completion.tool_calls)
                ),
            ),
        )


__all__ = ["NativeProvider", "NativeSessionAdapter", "project_native_messages"]
