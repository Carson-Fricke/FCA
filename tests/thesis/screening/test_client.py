"""Typed MCP screener client tests over an in-process fake transport.

The real :class:`~thesis.integrations.mcp.HarnessMcpClient` is exercised
against a fake JSON-RPC handler, so the frozen tool names, the typed argument
shapes, and the end-to-end service lifecycle run through the production client
code without a socket.
"""

from __future__ import annotations

import json
from collections.abc import Mapping

import pytest

from thesis.integrations.mcp import (
    TOOL_GET_SCREENER_STATUS,
    TOOL_RUN_SCREENER,
    TOOL_SCREENER_CONFIG_TEMPLATE,
    HarnessMcpTopology,
    HarnessMcpTransportError,
    open_harness_mcp_client,
)
from thesis.screening import ScreenerService, ScreenerUnavailableError

WORKSPACE = "alpha-lab"
RUN_ID = "scr-run-001"
AS_OF = "2026-09-28"


def _reply(message: Mapping[str, object], result: Mapping[str, object]):
    return {"jsonrpc": "2.0", "id": message.get("id"), "result": dict(result)}


class _ScreenerHandler:
    """In-process MCP handler serving the screener tool catalog and artifacts."""

    def __init__(
        self,
        *,
        document: Mapping[str, object],
        states: tuple[str, ...] = ("running", "complete"),
        transport_failure: str | None = None,
    ) -> None:
        self.states = list(states)
        self.document = dict(document)
        self.transport_failure = transport_failure
        self.calls: list[dict[str, object]] = []

    def handle(self, request: object) -> dict[str, object] | None:
        if not isinstance(request, Mapping):
            raise TypeError("request must be a mapping")
        message = dict(request)
        if "id" not in message:
            return None
        method = message.get("method")
        if method == "initialize":
            return _reply(message, {"protocolVersion": "2024-11-05"})
        if method == "tools/list":
            return _reply(
                message,
                {
                    "tools": [
                        {
                            "name": TOOL_RUN_SCREENER,
                            "description": "run",
                            "inputSchema": {},
                        },
                        {
                            "name": TOOL_GET_SCREENER_STATUS,
                            "description": "status",
                            "inputSchema": {},
                        },
                        {
                            "name": TOOL_SCREENER_CONFIG_TEMPLATE,
                            "description": "template",
                            "inputSchema": {},
                        },
                    ]
                },
            )
        if method != "tools/call":
            return _reply(message, {})
        if self.transport_failure is not None:
            raise HarnessMcpTransportError(self.transport_failure)
        params = message.get("params")
        if not isinstance(params, Mapping):
            raise TypeError("params must be a mapping")
        name = str(params.get("name"))
        arguments = params.get("arguments")
        arguments = dict(arguments) if isinstance(arguments, Mapping) else {}
        self.calls.append({"name": name, "arguments": arguments})
        return _reply(message, self._result(name, arguments))

    def _result(self, name: str, arguments: Mapping[str, object]) -> dict[str, object]:
        if name == TOOL_RUN_SCREENER:
            state = self.states[0] if self.states else "complete"
            return {
                "isError": False,
                "structuredContent": {"run_id": RUN_ID, "state": state},
            }
        if name == TOOL_GET_SCREENER_STATUS:
            state = self.states.pop(0) if self.states else "complete"
            return {
                "isError": False,
                "structuredContent": {
                    "job_id": arguments.get("job_id"),
                    "state": state,
                },
            }
        if name == "get_run":
            return {
                "isError": False,
                "structuredContent": {"run_id": RUN_ID, "status": "complete"},
            }
        if name == "list_artifacts":
            return {
                "isError": False,
                "structuredContent": {
                    "run_id": RUN_ID,
                    "artifacts": [{"name": "screen.json", "sha256": "a" * 64}],
                },
            }
        if name == "read_artifact":
            return {
                "isError": False,
                "structuredContent": {
                    "encoding": "utf-8",
                    "content": json.dumps(self.document),
                },
            }
        if name == TOOL_SCREENER_CONFIG_TEMPLATE:
            return {
                "isError": False,
                "structuredContent": {"shell": "screen", "schema_version": 1},
            }
        return {"isError": False, "structuredContent": {}}


def _client(handler: _ScreenerHandler):
    return open_harness_mcp_client(
        HarnessMcpTopology(thesis_location="local", harness_location="local"),
        in_process_handler=handler,
    )


def test_frozen_tool_names() -> None:
    assert TOOL_RUN_SCREENER == "run_screener"
    assert TOOL_GET_SCREENER_STATUS == "get_screener_status"
    assert TOOL_SCREENER_CONFIG_TEMPLATE == "screener_config_template"


def test_typed_client_methods_send_frozen_names_and_arguments(
    screen_document_factory,
) -> None:
    handler = _ScreenerHandler(document=screen_document_factory())
    client = _client(handler)
    client.initialize()

    launched = client.run_screener(config={"as_of": AS_OF}, workspace=WORKSPACE)
    assert launched.structured["run_id"] == RUN_ID
    assert handler.calls[-1] == {
        "name": TOOL_RUN_SCREENER,
        "arguments": {"config": {"as_of": AS_OF}, "workspace": WORKSPACE},
    }

    status = client.get_screener_status(job_id=RUN_ID)
    assert status.structured["state"] == "running"
    assert handler.calls[-1] == {
        "name": TOOL_GET_SCREENER_STATUS,
        "arguments": {"job_id": RUN_ID},
    }

    template = client.screener_config_template()
    assert template.structured == {"shell": "screen", "schema_version": 1}
    assert handler.calls[-1] == {
        "name": TOOL_SCREENER_CONFIG_TEMPLATE,
        "arguments": {},
    }


def test_service_lifecycle_over_the_real_client(
    definition_factory, screen_document_factory
) -> None:
    handler = _ScreenerHandler(
        document=screen_document_factory(), states=("running", "complete")
    )
    client = _client(handler)
    definition = definition_factory()
    slept: list[float] = []
    service = ScreenerService(
        tools=client,
        sleep=slept.append,
        poll_interval_seconds=0.01,
        max_polls=3,
    )
    service.save_definition(WORKSPACE, definition)

    record = service.run(WORKSPACE, definition.definition_id, AS_OF)

    assert [call["name"] for call in handler.calls] == [
        TOOL_RUN_SCREENER,
        TOOL_GET_SCREENER_STATUS,
        TOOL_GET_SCREENER_STATUS,
        "get_run",
        "list_artifacts",
        "read_artifact",
    ]
    launch = handler.calls[0]["arguments"]
    assert isinstance(launch, Mapping)
    config = launch["config"]
    assert isinstance(config, Mapping)
    assert launch["workspace"] == WORKSPACE
    assert config["bundle_hash"] == definition.source_sha256
    assert slept == [0.01]
    assert record.run_id == RUN_ID
    assert record.rows[0].metrics == {"momentum_12_1": "0.44"}
    assert service.get_result(WORKSPACE, record.result_id) is record


def test_transport_failure_is_a_typed_unavailable_error(
    definition_factory, screen_document_factory
) -> None:
    handler = _ScreenerHandler(
        document=screen_document_factory(),
        transport_failure="the harness MCP endpoint is unreachable",
    )
    client = _client(handler)
    definition = definition_factory()
    service = ScreenerService(tools=client, sleep=lambda seconds: None)
    service.save_definition(WORKSPACE, definition)

    with pytest.raises(ScreenerUnavailableError):
        service.run(WORKSPACE, definition.definition_id, AS_OF)

    assert service.list_results(WORKSPACE) == ()
