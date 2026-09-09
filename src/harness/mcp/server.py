"""JSON-RPC 2.0 / MCP protocol dispatch for the harness MCP server.

The server is deterministic and pure: ``handle`` takes one decoded request
mapping and returns one response mapping, or ``None`` for notifications.
There is no wall clock anywhere in dispatch — status timestamps arrive from
the remote API payloads, and no result synthesizes local time.

Protocol decisions:

- ``initialize`` answers with the fixed protocol version, the tools
  capability, and the server info.
- ``ping`` answers with an empty result.
- ``tools/list`` answers with the capability-filtered, schema-bearing tool
  entries.
- ``tools/call`` validates ``arguments`` against the tool's JSON schema
  (type/required/additionalProperties/minLength), dispatches, and returns
  text content plus ``structuredContent``. Domain failures raised by a
  handler (:class:`harness.mcp.tools.ToolExecutionError`) become ``isError``
  results; every protocol-level problem becomes a JSON-RPC error object with
  a precise code: -32700 parse error, -32600 invalid request, -32601 method
  not found, -32602 invalid params, -32603 internal error. Calling an
  unimplemented or unknown tool is invalid params (-32602): the tool is
  absent from the catalog, so it cannot be dispatched.
- Requests without an ``id`` are notifications: they are validated and
  dispatched but never answered.
- Batched requests (JSON arrays) are out of scope for this server and are
  rejected as invalid requests.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from harness.mcp.tools import (
    HarnessToolsContext,
    ToolCatalog,
    ToolExecutionError,
    build_catalog,
)

PROTOCOL_VERSION: Final = "2024-11-05"
SERVER_NAME: Final = "harness-mcp"
SERVER_VERSION: Final = "0.1.0"

PARSE_ERROR: Final = -32700
INVALID_REQUEST: Final = -32600
METHOD_NOT_FOUND: Final = -32601
INVALID_PARAMS: Final = -32602
INTERNAL_ERROR: Final = -32603

_JSONRPC_VERSION: Final = "2.0"


class JsonRpcError(Exception):
    """A protocol-level failure rendered as a JSON-RPC error object."""

    def __init__(self, code: int, message: str, data: object | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def error_response(
    request_id: object,
    code: int,
    message: str,
    data: object | None = None,
) -> dict[str, object]:
    """Build one JSON-RPC error response with an omitted-when-null data slot."""
    error: dict[str, object] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": _JSONRPC_VERSION, "id": request_id, "error": error}


def _valid_request_id(value: object) -> bool:
    if isinstance(value, bool):
        return False
    return isinstance(value, (str, int))


def _response_id(value: object) -> object:
    return value if _valid_request_id(value) else None


def _dumps(payload: Mapping[str, object]) -> str:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=True)


def _validate_arguments(
    schema: Mapping[str, object], arguments: Mapping[str, object]
) -> None:
    """Apply the minimal JSON-Schema checks the tool contracts rely on."""
    properties = schema.get("properties")
    properties = properties if isinstance(properties, Mapping) else {}
    if schema.get("additionalProperties") is False:
        for name in arguments:
            if name not in properties:
                raise JsonRpcError(INVALID_PARAMS, f"unexpected argument {name!r}")
    required = schema.get("required")
    if isinstance(required, (list, tuple)):
        for name in required:
            if name not in arguments:
                raise JsonRpcError(
                    INVALID_PARAMS, f"missing required argument {name!r}"
                )
    for name, value in arguments.items():
        property_schema = properties.get(name)
        if not isinstance(property_schema, Mapping):
            continue
        expected = property_schema.get("type")
        if expected == "string" and not isinstance(value, str):
            raise JsonRpcError(INVALID_PARAMS, f"argument {name!r} must be a string")
        if expected == "object" and not isinstance(value, Mapping):
            raise JsonRpcError(INVALID_PARAMS, f"argument {name!r} must be an object")
        minimum_length = property_schema.get("minLength")
        if (
            isinstance(value, str)
            and isinstance(minimum_length, int)
            and not isinstance(minimum_length, bool)
            and len(value) < minimum_length
        ):
            raise JsonRpcError(
                INVALID_PARAMS,
                f"argument {name!r} must not be empty",
            )
        maximum_length = property_schema.get("maxLength")
        if (
            isinstance(value, str)
            and isinstance(maximum_length, int)
            and not isinstance(maximum_length, bool)
            and len(value) > maximum_length
        ):
            raise JsonRpcError(
                INVALID_PARAMS,
                f"argument {name!r} must be at most {maximum_length} characters",
            )


@dataclass(frozen=True, slots=True)
class McpServer:
    """Dispatch one MCP request mapping to one response mapping."""

    catalog: ToolCatalog
    server_name: str = SERVER_NAME
    server_version: str = SERVER_VERSION
    protocol_version: str = PROTOCOL_VERSION

    def handle(self, request: object) -> dict[str, object] | None:
        """Dispatch one decoded request; notifications answer ``None``."""
        if not isinstance(request, Mapping):
            return error_response(
                None, INVALID_REQUEST, "a request must be a JSON object"
            )
        if request.get("jsonrpc") != _JSONRPC_VERSION:
            return error_response(
                _response_id(request.get("id")),
                INVALID_REQUEST,
                f"jsonrpc must be the exact string {_JSONRPC_VERSION!r}",
            )
        method = request.get("method")
        if not isinstance(method, str) or not method:
            return error_response(
                _response_id(request.get("id")),
                INVALID_REQUEST,
                "method must be a non-empty string",
            )
        raw_id = request.get("id")
        if "id" in request and not _valid_request_id(raw_id):
            return error_response(
                None, INVALID_REQUEST, "id must be a string or an integer"
            )
        params = request.get("params", {})
        if not isinstance(params, Mapping):
            return error_response(
                _response_id(raw_id),
                INVALID_REQUEST,
                "params must be a JSON object when present",
            )
        notification = "id" not in request
        try:
            result = self._dispatch(method, params)
        except JsonRpcError as error:
            return (
                None
                if notification
                else error_response(raw_id, error.code, error.message, error.data)
            )
        except Exception as error:
            return (
                None
                if notification
                else error_response(
                    raw_id,
                    INTERNAL_ERROR,
                    "internal error",
                    {"exception": type(error).__name__},
                )
            )
        if notification:
            return None
        return {"jsonrpc": _JSONRPC_VERSION, "id": raw_id, "result": result}

    def describe(self) -> dict[str, object]:
        """Return the serialized server description; it carries no secrets."""
        return {
            "protocolVersion": self.protocol_version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": self.server_name, "version": self.server_version},
            "tools": list(self.catalog.names()),
        }

    def _dispatch(self, method: str, params: Mapping[str, object]) -> dict[str, object]:
        if method == "initialize":
            return {
                "protocolVersion": self.protocol_version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {
                    "name": self.server_name,
                    "version": self.server_version,
                },
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": [tool.schema_entry() for tool in self.catalog.tools]}
        if method == "tools/call":
            return self._call_tool(params)
        raise JsonRpcError(METHOD_NOT_FOUND, f"unknown method {method!r}")

    def _call_tool(self, params: Mapping[str, object]) -> dict[str, object]:
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise JsonRpcError(
                INVALID_PARAMS, "tools/call requires a non-empty 'name' string"
            )
        tool = self.catalog.resolve(name)
        if tool is None:
            raise JsonRpcError(
                INVALID_PARAMS,
                f"unknown tool {name!r}; published tools: {list(self.catalog.names())}",
            )
        arguments = params.get("arguments", {})
        if not isinstance(arguments, Mapping):
            raise JsonRpcError(INVALID_PARAMS, "tool arguments must be a JSON object")
        _validate_arguments(tool.input_schema, arguments)
        try:
            payload = tool.handler(dict(arguments))
        except ToolExecutionError as error:
            result: dict[str, object] = {
                "content": [{"type": "text", "text": str(error)}],
                "isError": True,
            }
            if error.payload is not None:
                result["structuredContent"] = error.payload
            return result
        if not isinstance(payload, Mapping):
            raise JsonRpcError(
                INTERNAL_ERROR, f"tool {name!r} returned a non-object payload"
            )
        content = _dumps(payload)
        return {
            "content": [{"type": "text", "text": content}],
            "isError": False,
            "structuredContent": dict(payload),
        }


def create_server(
    context: HarnessToolsContext,
    *,
    server_name: str = SERVER_NAME,
    server_version: str = SERVER_VERSION,
    protocol_version: str = PROTOCOL_VERSION,
) -> McpServer:
    """Compose one server from an injected boundary context."""
    return McpServer(
        catalog=build_catalog(context),
        server_name=server_name,
        server_version=server_version,
        protocol_version=protocol_version,
    )


__all__ = [
    "INTERNAL_ERROR",
    "INVALID_PARAMS",
    "INVALID_REQUEST",
    "METHOD_NOT_FOUND",
    "McpServer",
    "PARSE_ERROR",
    "PROTOCOL_VERSION",
    "SERVER_NAME",
    "SERVER_VERSION",
    "JsonRpcError",
    "create_server",
    "error_response",
]
