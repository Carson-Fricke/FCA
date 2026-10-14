"""Versioned hosted-tool exchange with attribution outside model arguments."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from thesis_runtime.contracts.context import ToolContext
from thesis_runtime.contracts.datum import Datum, DatumWire, ToolOperation
from thesis_runtime.tools.registry import RegisteredTool, ToolExecution


class ToolTransport(Protocol):
    """A transport adapter authenticates the host and exchanges JSON envelopes."""

    def invoke(self, request: Mapping[str, object]) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class HostedTool:
    """A remote implementation of the same dispatch contract as a local tool.

    A transport fault remains an unknown outcome. The registry records it and
    never retries it; recovery reconciles the retained invocation identity.
    """

    name: str
    transport: ToolTransport

    def __call__(
        self, arguments: Mapping[str, object], context: ToolContext
    ) -> ToolExecution:
        if context.tool_invocation_id is None or context.idempotency_key is None:
            raise ValueError("hosted tool requires durable invocation identity")
        request = json.loads(
            json.dumps(
                {
                    "protocolVersion": 1,
                    "tool": self.name,
                    "arguments": dict(arguments),
                    "context": context.as_dict(),
                },
                allow_nan=False,
            )
        )
        response = self.transport.invoke(request)
        response = json.loads(json.dumps(dict(response), allow_nan=False))
        if (
            response.get("protocolVersion") != 1
            or response.get("invocationId") != context.tool_invocation_id
        ):
            raise ValueError("hosted tool acknowledgement does not match invocation")
        outcome = response.get("outcome")
        if not isinstance(outcome, Mapping) or not isinstance(outcome.get("ok"), bool):
            raise ValueError("hosted tool returned an invalid outcome")
        structured, text, failure = (
            outcome.get("structured", {}),
            outcome.get("text", ""),
            outcome.get("failure"),
        )
        terminal = outcome.get("terminal", False)
        if (
            not isinstance(structured, Mapping)
            or not isinstance(text, str)
            or failure is not None
            and not isinstance(failure, Mapping)
            or not isinstance(terminal, bool)
        ):
            raise ValueError("hosted tool returned invalid result fields")
        raw_datums = outcome.get("datums", [])
        if not isinstance(raw_datums, list):
            raise ValueError("hosted tool datums must be an array")
        datums = tuple(
            Datum(**DatumWire.model_validate(item).model_dump()) for item in raw_datums
        )
        return ToolExecution(
            ok=outcome["ok"],
            structured=dict(structured),
            text=text,
            failure=None if failure is None else dict(failure),
            terminal=terminal,
            datums=datums,
        )


def hosted_tool(
    descriptor: Mapping[str, object],
    transport: ToolTransport,
    *,
    operation: ToolOperation = "execute",
) -> RegisteredTool:
    """Register a hosted implementation without provider or FCA dependencies."""
    name = descriptor.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("hosted tool descriptor requires a name")
    return RegisteredTool(descriptor, HostedTool(name, transport), operation)
