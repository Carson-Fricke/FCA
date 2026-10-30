"""Pure native protocol projection at the provider boundary, not the runtime."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import closing
from dataclasses import replace
from typing import Protocol, cast
from uuid import uuid4

from thesis_runtime.adapters.providers.families.base import (
    ModelFamilyAdapter,
    NativeToolsAdapter,
)
from thesis_runtime.adapters.providers.invocations import (
    ModelCompletion,
    ProviderMessage,
    ProviderRejectedError,
    Role,
    ToolCall,
    content_digest,
    messages_payload,
)
from thesis_runtime.contracts.completion import CompletionOptions
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
        messages: Sequence[ProviderMessage],
        role: Role = "primary",
        tools: Sequence[Mapping[str, object]] | None = None,
        response_format: Mapping[str, object] | None = None,
        options: CompletionOptions | None = None,
    ) -> Generator[OutputDelta | ModelCompletion, None, None]: ...


def _mapping_json(value: object) -> dict[str, object]:
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError("provider request must contain only JSON values")


def _continuation(
    message: SessionMessage, origin: tuple[str, str] | None
) -> Mapping[str, object] | None:
    """Only completed outgoing model messages can supply native continuation."""
    result = message.content.result or {}
    if message.direction != "outgoing" or result.get("interrupted") is True:
        return None
    value = result.get("provider_continuation")
    if value is None:
        if "provider_continuation" not in result and result.get("provider_metadata"):
            raise ValueError(
                "outdated model message has no captured provider continuation"
            )
        return None
    if not isinstance(value, Mapping) or not isinstance(value.get("fields"), Mapping):
        raise ValueError("invalid provider continuation in canonical message")
    if not all(
        isinstance(value.get(name), str) and value[name]
        for name in ("provider", "model")
    ):
        raise ValueError("provider continuation has no origin")
    if origin is not None and (value["provider"], value["model"]) != origin:
        raise ValueError("provider continuation belongs to a different provider/model")
    return cast(Mapping[str, object], value["fields"])


def _tool_evidence(content: MessageContent, request: ToolRequest) -> dict[str, object]:
    """Omit only arguments already present in this exact native call group."""
    value = content.as_dict()
    value.pop("requests")
    try:
        arguments = json.loads(request.arguments)
    except (ValueError, TypeError):
        return value
    result = value.get("result")
    if (
        isinstance(arguments, dict)
        and isinstance(result, dict)
        and result.get("arguments") == arguments
    ):
        result.pop("arguments", None)
    return value


def project_native_messages(
    messages: Sequence[SessionMessage],
    *,
    origin: tuple[str, str] | None = None,
    input_mode: str = "conversation",
) -> tuple[ProviderMessage, ...]:
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
    if input_mode == "transcript":
        # Original calls, results and continuation remain data in this request.
        # They cannot become the provider's active tool exchange.
        instructions = tuple(
            ProviderMessage("system", message.content.text)
            for message in scope
            if message.direction == "instruction"
        )
        transcript = json.dumps(
            {
                "transcript": [
                    message.as_dict()
                    for message in scope
                    if message.direction != "instruction"
                ]
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        return (*instructions, ProviderMessage("user", transcript))
    if input_mode != "conversation":
        raise ValueError("unknown completion input mode")
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

    projected: list[ProviderMessage] = []
    deferred: list[ProviderMessage] = []
    defer_until = -1
    for index, message in enumerate(scope):
        if index in consumed:
            native: list[ProviderMessage] = []
        elif index in groups:
            native = [
                ProviderMessage(
                    role="assistant",
                    content=message.content.text,
                    tool_calls=tuple(
                        ToolCall(request.id, request.name, request.arguments)
                        for request in message.content.requests
                    ),
                    continuation=_continuation(message, origin),
                )
            ]
            for correlation in groups[index]:
                request = next(
                    request
                    for request in message.content.requests
                    if request.id == correlation.request_id
                )
                datums = [
                    _tool_evidence(scope[item].content, request)
                    for item in results[correlation]
                ]
                content = json.dumps(
                    datums[0] if len(datums) == 1 else {"datums": datums},
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                )
                native.append(
                    ProviderMessage(
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
            # Opaque continuation cannot be replayed after its call group was lost.
            result = dict(message.content.result or {})
            result.pop("provider_continuation", None)
            evidence = replace(
                message, content=replace(message.content, result=result or None)
            )
            native = [
                ProviderMessage(
                    role="user",
                    content=(
                        f"Incomplete request group ({reason}) / runtime evidence:\n"
                        + json.dumps(
                            evidence.as_dict(),
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
                reasoning = str(result.get("reasoning") or "")
                content = (
                    (
                        "Model output interrupted by provider failure; partial evidence only.\n"
                        if result.get("failure") is not None
                        else "Model output interrupted by operator steering.\n"
                    )
                    + "Reasoning so far:\n"
                    + reasoning
                    + "\nPartial answer:\n"
                    + content
                )
            native = [
                ProviderMessage(
                    role=role,
                    content=(
                        content
                        if message.direction != "incoming"
                        and message.content.text.strip()
                        else message.content.as_content()
                    ),
                    continuation=_continuation(message, origin),
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
    stays separate from answer text in ``content.result['reasoning']``. Exact
    continuation is opaque result data; only this boundary interprets it.
    """

    __slots__ = ("_family", "_id_factory", "_provider", "_origin", "_max_output_tokens")

    def __init__(
        self,
        provider: NativeProvider,
        *,
        family: ModelFamilyAdapter | None = None,
        id_factory: Callable[[], str] | None = None,
        origin: tuple[str, str] | None = None,
        max_output_tokens: int = 32768,
    ) -> None:
        if (
            isinstance(max_output_tokens, bool)
            or not isinstance(max_output_tokens, int)
            or max_output_tokens < 1
        ):
            raise ValueError("max_output_tokens must be positive")
        self._max_output_tokens = max_output_tokens
        self._provider = provider
        self._origin = origin
        self._family = family if family is not None else NativeToolsAdapter()
        self._id_factory = id_factory if id_factory is not None else lambda: uuid4().hex

    def request_snapshot(
        self,
        *,
        messages: Sequence[SessionMessage],
        role: Role = "primary",
        tools: Sequence[Mapping[str, object]] | None = None,
        response_format: Mapping[str, object] | None = None,
        options: CompletionOptions | None = None,
    ) -> Mapping[str, object]:
        """Prepare detached protocol audit data without invoking any effects.

        This is a versioned provider request, not a canonical session message.
        The caller owns persistence before invocation starts. No IDs are created.
        """
        native_messages = self._family.prepare_messages(
            project_native_messages(
                messages,
                origin=self._origin,
                input_mode=options.input_mode if options else "conversation",
            )
        )
        return self._snapshot(native_messages, role, tools, response_format, options)

    def _snapshot(
        self,
        messages: Sequence[ProviderMessage],
        role: Role,
        tools: Sequence[Mapping[str, object]] | None,
        response_format: Mapping[str, object] | None,
        options: CompletionOptions | None = None,
    ) -> Mapping[str, object]:
        """Use the transport's encoder; no intermediate message reconstruction."""
        snapshot = json.loads(
            json.dumps(
                {
                    "projection": "native-session-v6",
                    "messages": messages_payload(messages),
                    "role": role,
                    "tools": [
                        {"type": "function", "function": dict(tool)}
                        for tool in self._family.prepare_tools(tools)
                    ]
                    if tools is not None
                    else None,
                    "response_format": response_format,
                    "stream": self._provider.streaming,
                    "max_tokens": options.max_output_tokens
                    if options
                    else self._max_output_tokens,
                    **(
                        {"tool_choice": "none"}
                        if options and not options.allow_tool_calls
                        else {}
                    ),
                    **({"input_mode": options.input_mode} if options else {}),
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
        options: CompletionOptions | None = None,
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
        native_messages = self._family.prepare_messages(
            project_native_messages(
                scope,
                origin=self._origin,
                input_mode=options.input_mode if options else "conversation",
            )
        )
        snapshot = self._snapshot(
            native_messages, role, tools, response_format, options
        )
        events = self._provider.stream(
            messages=tuple(native_messages),
            role=cast(Role, snapshot["role"]),
            tools=cast(list[Mapping[str, object]] | None, snapshot["tools"]),
            response_format=cast(
                Mapping[str, object] | None, snapshot["response_format"]
            ),
            **({"options": options} if options is not None else {}),
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
            raise ProviderRejectedError(
                "provider stream ended without completion", transient=True
            )
        diagnostics = {
            "model": completion.model,
            "provider": completion.provider,
            "upstream_provider": completion.upstream_provider,
            "generation_id": completion.generation_id,
            "finish_reason": completion.finish_reason,
            "prompt_tokens": completion.prompt_tokens,
            "completion_tokens": completion.completion_tokens,
        }
        if completion.finish_reason == "length" or (
            not completion.message.content.strip() and not completion.message.tool_calls
        ):
            raise ProviderRejectedError(
                "provider completion was truncated or contained no answer/tool requests; continue with a smaller actionable step",
                transient=True,
                diagnostics={
                    **diagnostics,
                    **(
                        {"rejected_response": completion.message.as_wire()}
                        if options and options.input_mode == "transcript"
                        else {}
                    ),
                },
            )
        message = completion.message
        reasoning = normalise_reasoning(message.reasoning)
        result: dict[str, object] = {"reasoning": reasoning} if reasoning else {}
        if message.continuation is not None:
            origin = self._origin or (completion.provider, completion.model)
            result["provider_continuation"] = {
                "provider": origin[0],
                "model": origin[1],
                "fields": dict(message.continuation),
            }
        if (
            completion.upstream_provider is not None
            or completion.generation_id is not None
            or completion.finish_reason is not None
        ):
            result.setdefault("provider_continuation", None)
            result["provider_metadata"] = {
                "model": completion.model,
                "provider": completion.provider,
                "upstream_provider": completion.upstream_provider,
                "generation_id": completion.generation_id,
                "model_family": self._family.family,
                "family_adapter_version": self._family.version,
                **diagnostics,
            }
        yield SessionMessage(
            message_id=message_id,
            direction="outgoing",
            content=MessageContent(
                text=message.content,
                source="agent",
                result=result or None,
                requests=tuple(
                    ToolRequest.from_provider(request)
                    for request in self._family.decode_calls(message.tool_calls)
                ),
            ),
        )


__all__ = ["NativeProvider", "NativeSessionAdapter", "project_native_messages"]
