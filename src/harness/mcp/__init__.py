"""MCP server for harness backtests, sweeps, and the supervised trader.

The package exposes the MCP tool surface over the harness's published
boundaries: the local process commands (``harness backtest-job`` and
``harness trader``), the programmatic sweeps runner, and the authenticated
remote quant-harness API. Everything is boundary-injected — see
:mod:`harness.mcp.tools` for the tool catalog and capability record,
:mod:`harness.mcp.server` for protocol dispatch, :mod:`harness.mcp.transport`
for the stdio framing, and :mod:`harness.mcp.auth` for bearer resolution.

``harness mcp`` (or ``python -m harness.mcp``) serves the server over stdio;
``harness.mcp.__main__.app`` is the Typer command the harness CLI registers.
"""

from harness.mcp.auth import (
    BEARER_TOKEN_VARIABLE,
    BearerTokenSource,
    EnvironmentBearerTokenSource,
    MissingBearerCredentialError,
    bearer_headers,
    bearer_token_from_environ,
)
from harness.mcp.server import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    PROTOCOL_VERSION,
    SERVER_NAME,
    SERVER_VERSION,
    JsonRpcError,
    McpServer,
    create_server,
    error_response,
)
from harness.mcp.tools import (
    TOOL_BACKTEST_STATUS,
    TOOL_GET_RUN,
    TOOL_LAUNCH_TRADER,
    TOOL_LIST_ARTIFACTS,
    TOOL_RUN_BACKTEST,
    TOOL_RUN_SWEEP,
    HarnessTools,
    HarnessToolsContext,
    ProcessOutcome,
    ProcessRunner,
    RemoteResponse,
    RemoteTransport,
    SweepSubmitter,
    Tool,
    ToolCapabilities,
    ToolCatalog,
    ToolExecutionError,
    build_catalog,
)
from harness.mcp.transport import (
    FramingError,
    InProcessTransport,
    JsonRpcHandler,
    decode_message,
    encode_message,
    serve_stdio,
)

__all__ = [
    "BEARER_TOKEN_VARIABLE",
    "FramingError",
    "HarnessTools",
    "HarnessToolsContext",
    "INTERNAL_ERROR",
    "INVALID_PARAMS",
    "INVALID_REQUEST",
    "InProcessTransport",
    "JsonRpcError",
    "JsonRpcHandler",
    "METHOD_NOT_FOUND",
    "McpServer",
    "PARSE_ERROR",
    "PROTOCOL_VERSION",
    "ProcessOutcome",
    "ProcessRunner",
    "RemoteResponse",
    "RemoteTransport",
    "SERVER_NAME",
    "SERVER_VERSION",
    "SweepSubmitter",
    "TOOL_BACKTEST_STATUS",
    "TOOL_GET_RUN",
    "TOOL_LAUNCH_TRADER",
    "TOOL_LIST_ARTIFACTS",
    "TOOL_RUN_BACKTEST",
    "TOOL_RUN_SWEEP",
    "Tool",
    "ToolCapabilities",
    "ToolCatalog",
    "ToolExecutionError",
    "BearerTokenSource",
    "EnvironmentBearerTokenSource",
    "MissingBearerCredentialError",
    "bearer_headers",
    "bearer_token_from_environ",
    "build_catalog",
    "create_server",
    "decode_message",
    "encode_message",
    "error_response",
    "serve_stdio",
]
