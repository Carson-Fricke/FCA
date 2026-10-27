"""Strict OpenAI-compatible SSE completion assembly at the provider boundary."""

import codecs
import json
import re
from collections.abc import Iterator, Mapping

from thesis_runtime.adapters.providers.invocations import (
    ModelCompletion,
    Role,
    parse_chat_completion,
)
from thesis_runtime.adapters.providers.reasoning import ReasoningStream, reasoning_text
from thesis_runtime.contracts.errors import ProviderRejectedError
from thesis_runtime.contracts.output import OutputDelta


def sse_data(chunks: Iterator[bytes]) -> Iterator[str]:
    """Decode UTF-8 and CR/LF framing with bounded partial lines and events."""
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    buffer = ""
    data: list[str] = []
    size = 0
    limit = 1024 * 1024

    def lines(*, final: bool = False) -> Iterator[str]:
        nonlocal buffer, data, size
        while match := re.search(r"[\r\n]", buffer):
            position = match.start()
            if buffer[position] == "\r" and position + 1 == len(buffer) and not final:
                break
            width = 2 if buffer[position : position + 2] == "\r\n" else 1
            line, buffer = buffer[:position], buffer[position + width :]
            if not line:
                if data:
                    yield "\n".join(data)
                data, size = [], 0
            elif line.startswith("data:"):
                value = line[5:].removeprefix(" ")
                try:
                    size += len(value.encode("utf-8"))
                except UnicodeEncodeError as error:
                    raise ProviderRejectedError(
                        "provider emitted invalid Unicode output"
                    ) from error
                if size > limit:
                    raise ProviderRejectedError(
                        "provider SSE frame exceeded the size limit"
                    )
                data.append(value)
        if len(buffer) > limit:
            raise ProviderRejectedError("provider SSE line exceeded the size limit")

    try:
        for chunk in chunks:
            buffer += decoder.decode(chunk)
            yield from lines()
        buffer += decoder.decode(b"", final=True)
        yield from lines(final=True)
    except UnicodeDecodeError as error:
        raise ProviderRejectedError("provider SSE contained invalid UTF-8") from error
    if buffer or data:
        raise ProviderRejectedError(
            "provider SSE frame ended prematurely", transient=True
        )


def parse_completion_stream(
    events: Iterator[str], *, provider: str, model: str, role: Role
) -> Iterator[OutputDelta | ModelCompletion]:
    """Assemble tool arguments privately; emit readable reasoning and answer deltas.

    A finish frame alone is insufficient: usage may follow, and EOF without DONE
    is an interrupted request. Scalar reasoning wins over duplicate detail fields.
    """
    text: list[str] = []
    reasoning = ReasoningStream()
    display_reasoning: list[str] = []
    calls: dict[int, dict[str, object]] = {}
    metadata: dict[str, object] = {}
    finished = False
    finish_reason = None
    size = 0
    for raw in events:
        if raw == "[DONE]":
            if not finished:
                raise ProviderRejectedError("provider stream omitted its finish reason")
            message = {
                "content": "".join(text),
                **reasoning.fields(),
                "tool_calls": [calls[index] for index in sorted(calls)],
            }
            yield parse_chat_completion(
                body=json.dumps(
                    {
                        **metadata,
                        "choices": [
                            {"message": message, "finish_reason": finish_reason}
                        ],
                    }
                ),
                provider=provider,
                fallback_model=model,
                role=role,
                display_reasoning="".join(display_reasoning),
            )
            return
        try:
            data = json.loads(raw)
        except ValueError as error:
            raise ProviderRejectedError(
                "provider emitted malformed stream JSON"
            ) from error
        if not isinstance(data, dict):
            raise ProviderRejectedError("provider emitted malformed stream data")
        if data.get("error") is not None:
            raise _stream_error(data["error"], "provider reported a stream error")
        for key in ("id", "model", "provider", "usage"):
            if data.get(key) is not None:
                metadata[key] = data[key]
        choices = data.get("choices", [])
        if not isinstance(choices, list):
            raise ProviderRejectedError("provider emitted malformed stream choices")
        for choice in choices:
            if not isinstance(choice, dict) or choice.get("index", 0) != 0:
                raise ProviderRejectedError(
                    "provider emitted an unexpected stream choice"
                )
            if (
                choice.get("finish_reason") == "error"
                or choice.get("error") is not None
            ):
                raise _stream_error(
                    choice.get("error"), "provider reported an interrupted completion"
                )
            delta = choice.get("delta", {})
            if not isinstance(delta, dict):
                raise ProviderRejectedError("provider emitted a malformed delta")
            answer = delta.get("content")
            thought = reasoning_text(delta)
            if finished and (
                answer
                or thought
                or delta.get("tool_calls")
                or delta.get("reasoning_details")
            ):
                raise ProviderRejectedError(
                    "provider emitted content after its finish reason"
                )
            # Count opaque blocks too: encrypted continuation has no display text.
            size += len(
                json.dumps(
                    delta.get("reasoning_details", []), ensure_ascii=True
                ).encode("utf-8")
            )
            if size > 8 * 1024 * 1024:
                raise ProviderRejectedError(
                    "provider completion exceeded the output limit"
                )
            reasoning.add(delta)
            for channel, value in (("reasoning", thought), ("text", answer)):
                if value is None:
                    continue
                if not isinstance(value, str):
                    raise ProviderRejectedError("provider emitted non-text output")
                try:
                    size += len(value.encode("utf-8"))
                except UnicodeEncodeError as error:
                    raise ProviderRejectedError(
                        "provider emitted invalid Unicode output"
                    ) from error
                if size > 8 * 1024 * 1024:
                    raise ProviderRejectedError(
                        "provider completion exceeded the output limit"
                    )
                if value:
                    if channel == "text":
                        text.append(value)
                    else:
                        display_reasoning.append(value)
                    yield OutputDelta(channel, value)
            tool_calls = delta.get("tool_calls") or []
            if not isinstance(tool_calls, list):
                raise ProviderRejectedError("provider emitted malformed tool calls")
            for call in tool_calls:
                if (
                    not isinstance(call, dict)
                    or type(call.get("index")) is not int
                    or call["index"] < 0
                ):
                    raise ProviderRejectedError(
                        "provider tool delta requires a valid index"
                    )
                if call.get("type") not in (None, "function"):
                    raise ProviderRejectedError(
                        "provider emitted an unsupported tool type"
                    )
                if len(calls) >= 128 and call["index"] not in calls:
                    raise ProviderRejectedError("provider emitted too many tool calls")
                target = calls.setdefault(
                    call["index"],
                    {
                        "id": "",
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    },
                )
                function = call.get("function", {})
                if not isinstance(function, dict):
                    raise ProviderRejectedError(
                        "provider emitted malformed tool arguments"
                    )
                for key, source, destination in (
                    ("id", call, target),
                    ("name", function, target["function"]),
                    ("arguments", function, target["function"]),
                ):
                    value = source.get(key, "")
                    if value is None:
                        continue
                    if not isinstance(value, str) or not isinstance(destination, dict):
                        raise ProviderRejectedError(
                            "provider emitted a malformed tool fragment"
                        )
                    try:
                        size += len(value.encode("utf-8"))
                    except UnicodeEncodeError as error:
                        raise ProviderRejectedError(
                            "provider emitted invalid Unicode tool data"
                        ) from error
                    if size > 8 * 1024 * 1024:
                        raise ProviderRejectedError(
                            "provider completion exceeded the output limit"
                        )
                    if key == "id" and destination[key]:
                        if value and value != destination[key]:
                            raise ProviderRejectedError(
                                "provider changed a tool call identity"
                            )
                    else:
                        destination[key] += value
            if choice.get("finish_reason") is not None:
                finished = True
                finish_reason = choice["finish_reason"]
    raise ProviderRejectedError("provider stream ended before DONE", transient=True)


def _stream_error(detail: object, message: str) -> ProviderRejectedError:
    """Keep numeric provider status, never raw upstream messages or credentials."""
    code = detail.get("code") if isinstance(detail, Mapping) else None
    status = code if type(code) is int else None
    return ProviderRejectedError(message, status=status, transient=status is None)
