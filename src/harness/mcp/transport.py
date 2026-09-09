"""stdio framing for the harness MCP JSON-RPC server.

Framing decision: one JSON-RPC message per line (newline-delimited JSON,
the MCP stdio convention). Message text is compact, ASCII-escaped JSON, so a
message can never contain an embedded newline and any line-oriented reader —
including this transport — can reassemble it exactly. Requests are read from
``stdin`` and responses are written to ``stdout`` with an explicit flush per
message; diagnostics go to ``stderr`` only, because stdout carries protocol
traffic and must stay clean.

Malformed JSON lines produce a JSON-RPC ``parse error`` response (code
-32700) with a ``null`` id, exactly as JSON-RPC 2.0 prescribes. EOF closes
the loop and shuts the server down gracefully.

Tests do not need a real process: :class:`InProcessTransport` calls the
server handler directly with decoded request dicts.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Protocol, TextIO

from harness.mcp.server import error_response

PARSER_ERROR_CODE: Final = -32700

_BLANK: Final = ""


class FramingError(ValueError):
    """Raised when one stdin line is not decodable JSON."""


class JsonRpcHandler(Protocol):
    """The dispatch surface :func:`serve_stdio` drives."""

    def handle(self, request: object) -> dict[str, object] | None: ...


def encode_message(message: Mapping[str, object]) -> str:
    """Render one response as a single-line, ASCII-safe JSON document."""
    return json.dumps(message, separators=(",", ":"), ensure_ascii=True)


def decode_message(line: str) -> object:
    """Decode one stdin line, raising :class:`FramingError` when malformed."""
    try:
        return json.loads(line)
    except ValueError as error:
        raise FramingError(f"line is not valid JSON: {error}") from error


def _parse_error_response(error: FramingError) -> dict[str, object]:
    return error_response(
        None,
        PARSER_ERROR_CODE,
        "parse error: the request line is not valid JSON",
        {"detail": str(error)},
    )


def serve_stdio(
    handler: JsonRpcHandler,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Serve newline-delimited JSON-RPC over stdio until EOF.

    Blank lines are skipped. Malformed JSON lines answer with one parse-error
    response; everything else is dispatched to ``handler.handle``. Requests
    without an ``id`` (notifications) never produce a response line. Returns
    ``0`` after a graceful EOF shutdown.
    """
    source = sys.stdin if stdin is None else stdin
    sink = sys.stdout if stdout is None else stdout
    diagnostics = sys.stderr if stderr is None else stderr
    diagnostics.write("harness mcp: serving JSON-RPC over stdio\n")
    diagnostics.flush()
    answered = 0
    for line in source:
        text = line.strip()
        if not text:
            continue
        try:
            request = decode_message(text)
        except FramingError as error:
            response: dict[str, object] | None = _parse_error_response(error)
        else:
            response = handler.handle(request)
        if response is not None:
            sink.write(encode_message(response) + "\n")
            sink.flush()
            answered += 1
    diagnostics.write(f"harness mcp: shutdown after {answered} responses\n")
    diagnostics.flush()
    return 0


@dataclass(frozen=True, slots=True)
class InProcessTransport:
    """Test seam that dispatches decoded request dicts without framing."""

    handler: JsonRpcHandler

    def call(self, request: Mapping[str, object]) -> dict[str, object] | None:
        """Dispatch one decoded request exactly as the stdio loop would."""
        return self.handler.handle(request)


__all__ = [
    "FramingError",
    "InProcessTransport",
    "JsonRpcHandler",
    "PARSER_ERROR_CODE",
    "decode_message",
    "encode_message",
    "serve_stdio",
]
