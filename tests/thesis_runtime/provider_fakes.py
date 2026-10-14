"""External model-port scripts shared by runtime and HTTP behavior tests."""

import json
from collections.abc import Iterator
from dataclasses import replace
from json import dumps as json_dumps

import httpx

from thesis_runtime.adapters.providers.invocations import ModelCompletion
from thesis_runtime.contracts.errors import ProviderRejectedError
from thesis_runtime.contracts.messages import SessionMessage


def completion_frames(
    completion: SessionMessage | ModelCompletion, message_id: str | None
) -> Iterator[SessionMessage | ModelCompletion]:
    if isinstance(completion, SessionMessage) and message_id is not None:
        completion = replace(completion, message_id=message_id)
    yield completion


def completion_events(status: int, body: str) -> Iterator[str]:
    if not 200 <= status < 300:
        raise ProviderRejectedError("provider rejected request", status=status)
    try:
        response = json.loads(body)
    except ValueError:
        yield body
        return
    if not isinstance(response, dict):
        yield body
        return
    choices = response.get("choices", [])
    if isinstance(choices, list):
        for choice in choices:
            if isinstance(choice, dict) and "message" in choice:
                choice["delta"] = choice.pop("message")
                choice["finish_reason"] = "stop"
                for index, call in enumerate(choice["delta"].get("tool_calls") or []):
                    call["index"] = index
    yield json.dumps(response)
    yield "[DONE]"


def stream_response(status: int, *, json: dict[str, object]) -> httpx.Response:
    """A scripted HTTP SSE response, including a terminal frame."""
    body = "".join(
        f"data: {event}\n\n" for event in completion_events(status, json_dumps(json))
    )
    return httpx.Response(
        status, text=body, headers={"Content-Type": "text/event-stream"}
    )
