"""Bound agent-facing artifact pages over the full harness artifact reader.

Owns page selection and JSON-pointer inspection before results enter the session
inbox. Immutable artifacts and deterministic full-file readers stay with the
harness; this adapter never rewrites stored artifacts or conversation history.
"""

import json
import re
from collections.abc import Mapping
from typing import cast

from thesis.agent.harness_tools import HarnessToolProvider, HarnessToolSpec
from thesis_runtime.contracts.context import ToolContext
from thesis_runtime.contracts.failures import failure_from_exception
from thesis_runtime.tools.reading import DEFAULT_READ_TOKENS, MAX_READ_TOKENS, text_page
from thesis_runtime.tools.registry import RegisteredTool, ToolExecution


def _select(document: object, pointer: str) -> object:
    if not pointer:
        return document
    if not pointer.startswith("/") or re.search(r"~(?![01])", pointer):
        raise ValueError("json_pointer must be a JSON pointer such as /equity")
    for part in pointer[1:].split("/"):
        key = part.replace("~1", "/").replace("~0", "~")
        if isinstance(document, dict):
            document = document[key]
        elif isinstance(document, list) and re.fullmatch(r"0|[1-9][0-9]*", key):
            document = document[int(key)]
        else:
            raise ValueError("json_pointer does not identify an artifact value")
    return document


def artifact_registered_tool(
    provider: HarnessToolProvider, spec: HarnessToolSpec
) -> RegisteredTool:
    """Expose bounded content to agents over the injected full-file MCP port."""

    def handle(arguments: Mapping[str, object], context: ToolContext) -> ToolExecution:
        del context
        result = provider.call_tool(
            "read_artifact", {key: arguments[key] for key in ("job_id", "name")}
        )
        if not result.ok:
            return ToolExecution(
                ok=False,
                failure=dict(result.failure.as_dict())
                if result.failure
                else {"kind": "harness", "message": "Artifact read failed"},
            )
        artifact = result.structured
        content, encoding = artifact.get("content"), artifact.get("encoding")
        if (
            not isinstance(content, str)
            or not isinstance(encoding, str)
            or encoding not in {"utf-8", "hex"}
        ):
            return ToolExecution(
                ok=False,
                failure={
                    "kind": "protocol",
                    "message": "Artifact response requires encoded content",
                    "certainty": "observed_failure",
                    "retry_safe": False,
                },
            )
        pointer = arguments.get("json_pointer")
        if isinstance(pointer, str):
            try:
                if encoding != "utf-8":
                    raise ValueError("json_pointer requires a UTF-8 JSON artifact")
                content = json.dumps(
                    _select(json.loads(content), pointer),
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            except (ValueError, KeyError, IndexError) as error:
                return ToolExecution(
                    ok=False,
                    failure=failure_from_exception(
                        error,
                        **{
                            "kind": "invalid_arguments",
                            "message": f"artifact {arguments['name']!r}, json_pointer={pointer!r}: {error}",
                            "certainty": "observed_failure",
                            "retry_safe": False,
                        },
                    ),
                )
        try:
            page = text_page(
                content,
                token_offset=cast(int, arguments.get("token_offset", 0)),
                max_tokens=cast(int, arguments.get("max_tokens", DEFAULT_READ_TOKENS)),
            )
        except ValueError as error:
            return ToolExecution(
                ok=False,
                failure=failure_from_exception(
                    error,
                    **{
                        "kind": "invalid_arguments",
                        "message": str(error),
                        "certainty": "not_started",
                        "retry_safe": True,
                    },
                ),
            )
        next_offset = page["nextTokenOffset"]
        return ToolExecution(
            ok=True,
            structured={
                "job_id": arguments["job_id"],
                "name": arguments["name"],
                "encoding": encoding,
                "json_pointer": pointer,
                **page,
                "nextArguments": {**arguments, "token_offset": next_offset}
                if next_offset is not None
                else None,
            },
        )

    return RegisteredTool(
        descriptor={
            "name": spec.name,
            "description": (
                "Read a bounded page of an immutable job artifact. "
                "Ordinary artifacts are returned whole; copy nextArguments for token-budgeted continuation. "
                "For JSON, select a value with json_pointer (e.g. /equity or /trades/rows/0). "
                "Use get_job_result for headline metrics. Pages may be partial JSON; "
                "the complete artifact remains stored."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "required": ["job_id", "name"],
                "properties": {
                    "job_id": {"type": "string", "minLength": 1},
                    "name": {"type": "string", "minLength": 1},
                    "token_offset": {"type": "integer", "minimum": 0},
                    "max_tokens": {
                        "type": "integer",
                        "minimum": 32,
                        "maximum": MAX_READ_TOKENS,
                        "default": DEFAULT_READ_TOKENS,
                    },
                    "json_pointer": {"type": "string"},
                },
            },
        },
        handler=handle,
        operation="read",
    )
