"""Focused tests for the harness MCP server protocol, tools, and wiring.

Every boundary is faked in-process: the remote transport records the exact
URL, bearer headers, and payload; the process runner records exact argv; the
sweep submitter records the spec path. No network, no subprocesses, no wall
clock.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from io import StringIO
from pathlib import Path
from typing import Final

import pytest
from typer.testing import CliRunner

from harness.mcp import (
    TOOL_BACKTEST_STATUS,
    TOOL_GET_RUN,
    TOOL_LAUNCH_TRADER,
    TOOL_LIST_ARTIFACTS,
    TOOL_RUN_BACKTEST,
    TOOL_RUN_SWEEP,
    EnvironmentBearerTokenSource,
    InProcessTransport,
    McpServer,
    ToolCapabilities,
    ToolExecutionError,
    create_server,
    serve_stdio,
)
from harness.mcp.tools import HarnessToolsContext, ProcessOutcome, RemoteResponse
from harness.mcp.transport import decode_message, encode_message

BEARER_TOKEN: Final = "secret-token"
API_BASE_URL: Final = "https://harness.example"
CONFIG_ROOT: Final = Path(__file__).parents[3] / "config" / "harness"
DEFAULT_TOOLS: Final = frozenset(
    {
        TOOL_RUN_BACKTEST,
        TOOL_BACKTEST_STATUS,
        TOOL_GET_RUN,
        TOOL_LIST_ARTIFACTS,
        TOOL_RUN_SWEEP,
    }
)


def token_source(token: str | None) -> EnvironmentBearerTokenSource:
    values = {} if token is None else {"HARNESS_API_BEARER_TOKEN": token}
    return EnvironmentBearerTokenSource(values)


def remote_context(
    transport: FakeRemoteTransport,
    token: str | None = BEARER_TOKEN,
    capabilities: ToolCapabilities | None = None,
) -> HarnessToolsContext:
    return HarnessToolsContext(
        mode="remote",
        credentials=token_source(token),
        capabilities=ToolCapabilities() if capabilities is None else capabilities,
        api_base_url=API_BASE_URL,
        remote_transport=transport,
        runner=None,
    )


def local_context(
    runner: FakeRunner,
    sweeps: FakeSweeps | None = None,
    capabilities: ToolCapabilities | None = None,
) -> HarnessToolsContext:
    return HarnessToolsContext(
        mode="local",
        credentials=token_source(None),
        capabilities=ToolCapabilities() if capabilities is None else capabilities,
        api_base_url=None,
        remote_transport=None,
        runner=runner,
        sweep_submitter=sweeps,
    )


def rpc(
    method: str, params: Mapping[str, object] | None = None, id: object = 1
) -> dict[str, object]:
    request: dict[str, object] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        request["params"] = params
    if id is not None:
        request["id"] = id
    return request


def call(
    server: McpServer,
    method: str,
    params: Mapping[str, object] | None = None,
    id: object = 1,
) -> dict[str, object]:
    response = InProcessTransport(server).call(rpc(method, params, id))
    assert response is not None
    return response


def result_payload(response: dict[str, object]) -> dict[str, object]:
    result = response["result"]
    assert isinstance(result, dict)
    assert result["isError"] is False
    return result["structuredContent"]


def error_result(response: dict[str, object]) -> dict[str, object]:
    result = response["result"]
    assert isinstance(result, dict)
    assert result["isError"] is True
    return result


def error_code(response: dict[str, object]) -> int:
    error = response["error"]
    assert isinstance(error, dict)
    return error["code"]


def write_benchmark_configuration(tmp_path: Path) -> Path:
    config_path = tmp_path / "backtest.yaml"
    config_path.write_text(
        (CONFIG_ROOT / "backtest.yaml")
        .read_text(encoding="utf-8")
        .replace(
            "implementation: strategies.harness_examples.intraday_momentum:"
            "IntradayMomentumStrategy",
            "implementation: harness.strategy.reference:ReferenceStrategy",
        ),
        encoding="utf-8",
    )
    return config_path


class FakeRemoteTransport:
    """Remote boundary recording every call and replaying canned responses."""

    def __init__(
        self,
        get_responses: tuple[RemoteResponse, ...] = (),
        post_responses: tuple[RemoteResponse, ...] = (),
    ) -> None:
        self.get_calls: list[tuple[str, Mapping[str, str]]] = []
        self.post_calls: list[tuple[str, Mapping[str, str], Mapping[str, object]]] = []
        self._get_responses = list(get_responses)
        self._post_responses = list(post_responses)

    def get(self, url: str, headers: Mapping[str, str]) -> RemoteResponse:
        self.get_calls.append((url, dict(headers)))
        if self._get_responses:
            return self._get_responses.pop(0)
        return RemoteResponse(200, {}, {})

    def post(
        self,
        url: str,
        headers: Mapping[str, str],
        payload: Mapping[str, object],
    ) -> RemoteResponse:
        self.post_calls.append((url, dict(headers), payload))
        if self._post_responses:
            return self._post_responses.pop(0)
        return RemoteResponse(202, {}, {})


class FakeRunner:
    """Process boundary recording exact argv and returning canned outcomes."""

    def __init__(self, outcome: ProcessOutcome | None = None) -> None:
        self.argvs: list[tuple[str, ...]] = []
        self._outcome = outcome or ProcessOutcome(0, "status: completed\n", "")

    def __call__(self, argv: object) -> ProcessOutcome:
        assert isinstance(argv, (list, tuple))
        self.argvs.append(tuple(argv))
        return self._outcome


class FakeSweeps:
    """Sweep submitter recording spec paths and returning a canned record."""

    def __init__(self) -> None:
        self.spec_paths: list[str] = []

    def __call__(self, spec_path: str) -> Mapping[str, object]:
        self.spec_paths.append(spec_path)
        return {
            "sweep_id": "ab" * 32,
            "requested": 2,
            "expanded": 2,
            "duplicates_skipped": 0,
            "run_ids": ["run-1", "run-2"],
        }


def test_initialize_handshake_returns_protocol_capabilities_and_server_info() -> None:
    server = create_server(local_context(FakeRunner()))

    response = call(server, "initialize", {})

    result = response["result"]
    assert isinstance(result, dict)
    assert result["protocolVersion"] == "2024-11-05"
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert result["serverInfo"] == {"name": "harness-mcp", "version": "0.1.0"}


def test_ping_answers_an_empty_result() -> None:
    server = create_server(local_context(FakeRunner()))

    response = call(server, "ping")

    assert response["result"] == {}


def test_tools_list_contains_exactly_the_default_capability_set() -> None:
    server = create_server(local_context(FakeRunner(), FakeSweeps()))

    response = call(server, "tools/list")

    result = response["result"]
    assert isinstance(result, dict)
    names = [entry["name"] for entry in result["tools"]]
    assert sorted(names) == sorted(DEFAULT_TOOLS)
    for entry in result["tools"]:
        assert isinstance(entry["description"], str) and entry["description"]
        schema = entry["inputSchema"]
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert isinstance(schema["required"], list) and schema["required"]
        for property_schema in schema["properties"].values():
            assert property_schema["type"] == "string"


def test_launch_trader_is_absent_by_default_and_present_when_gated_on() -> None:
    default_server = create_server(local_context(FakeRunner()))

    denied = call(
        default_server,
        "tools/call",
        {
            "name": TOOL_LAUNCH_TRADER,
            "arguments": {"config_path": "trader.yaml"},
        },
    )

    assert error_code(denied) == -32602
    gated_server = create_server(
        local_context(FakeRunner(), capabilities=ToolCapabilities(trader=True))
    )
    listing = call(gated_server, "tools/list")
    names = [entry["name"] for entry in listing["result"]["tools"]]
    assert TOOL_LAUNCH_TRADER in names


def test_shadow_and_live_never_appear_in_the_tool_surface() -> None:
    server = create_server(
        local_context(FakeRunner(), capabilities=ToolCapabilities(trader=True))
    )

    listing = call(server, "tools/list")

    rendered = json.dumps(listing)
    assert "shadow" not in rendered
    assert "live" not in rendered
    assert TOOL_LAUNCH_TRADER in rendered
    assert set(ToolCapabilities().as_mapping()) == {
        "backtest",
        "backtest_status",
        "artifacts",
        "sweeps",
        "trader",
    }
    with pytest.raises(TypeError):
        ToolCapabilities(**{"shadow": True})
    with pytest.raises(TypeError):
        ToolCapabilities(**{"backtest": "yes"})


def test_tools_call_run_backtest_remote_mirrors_the_api_contract(tmp_path) -> None:
    transport = FakeRemoteTransport(
        post_responses=(
            RemoteResponse(
                202,
                {"Location": "/v1/backtests/jobs/momentum-spy-2024-01"},
                {"job_id": "momentum-spy-2024-01", "state": "queued"},
            ),
        )
    )
    server = create_server(remote_context(transport))
    config_path = write_benchmark_configuration(tmp_path)

    response = call(
        server,
        "tools/call",
        {"name": TOOL_RUN_BACKTEST, "arguments": {"config_path": str(config_path)}},
    )

    (url, headers, payload) = transport.post_calls[0]
    assert url == f"{API_BASE_URL}/v1/backtests/jobs"
    assert headers == {"Authorization": f"Bearer {BEARER_TOKEN}"}
    assert payload["shell"] == "backtest"
    assert payload["run_id"] == "momentum-spy-2024-01"
    assert isinstance(payload["strategy_instances"], list)
    assert result_payload(response) == {
        "boundary": "remote",
        "job_id": "momentum-spy-2024-01",
        "state": "queued",
        "location": "/v1/backtests/jobs/momentum-spy-2024-01",
    }


def test_remote_run_backtest_reports_credential_and_protocol_failures(
    tmp_path,
) -> None:
    rejected = FakeRemoteTransport(post_responses=(RemoteResponse(401, {}, {}),))
    response = call(
        create_server(remote_context(rejected)),
        "tools/call",
        {
            "name": TOOL_RUN_BACKTEST,
            "arguments": {"config_path": str(write_benchmark_configuration(tmp_path))},
        },
    )
    assert error_result(response)["content"][0]["text"].startswith(
        "the remote harness API rejected the bearer credential"
    )

    broken = FakeRemoteTransport(
        post_responses=(RemoteResponse(202, {}, {"job_id": "job-1"}),)
    )
    response = call(
        create_server(remote_context(broken)),
        "tools/call",
        {
            "name": TOOL_RUN_BACKTEST,
            "arguments": {"config_path": str(write_benchmark_configuration(tmp_path))},
        },
    )
    assert "non-empty 'state' string" in error_result(response)["content"][0]["text"]


def test_remote_run_backtest_without_a_bearer_reports_the_environment_variable(
    tmp_path,
) -> None:
    transport = FakeRemoteTransport()
    server = create_server(remote_context(transport, token=None))

    response = call(
        server,
        "tools/call",
        {
            "name": TOOL_RUN_BACKTEST,
            "arguments": {"config_path": str(write_benchmark_configuration(tmp_path))},
        },
    )

    assert transport.post_calls == []
    assert "HARNESS_API_BEARER_TOKEN" in error_result(response)["content"][0]["text"]


def test_tools_call_run_backtest_local_builds_exact_argv(tmp_path) -> None:
    runner = FakeRunner()
    server = create_server(local_context(runner))
    config_path = write_benchmark_configuration(tmp_path)
    workspace = tmp_path / "workspace"

    response = call(
        server,
        "tools/call",
        {
            "name": TOOL_RUN_BACKTEST,
            "arguments": {
                "config_path": str(config_path),
                "workspace": str(workspace),
            },
        },
    )

    assert runner.argvs == [
        (
            "harness",
            "backtest-job",
            "--config",
            str(config_path),
            "--output-dir",
            str(workspace),
        )
    ]
    assert result_payload(response) == {
        "boundary": "local",
        "exit_code": 0,
        "stdout": "status: completed\n",
        "stderr": "",
    }

    call(
        server,
        "tools/call",
        {"name": TOOL_RUN_BACKTEST, "arguments": {"config_path": str(config_path)}},
    )
    assert runner.argvs[-1] == ("harness", "backtest-job", "--config", str(config_path))


def test_local_backtest_reports_a_nonzero_exit_as_an_error(tmp_path) -> None:
    runner = FakeRunner(ProcessOutcome(2, "", "boom"))
    server = create_server(local_context(runner))

    response = call(
        server,
        "tools/call",
        {
            "name": TOOL_RUN_BACKTEST,
            "arguments": {"config_path": str(write_benchmark_configuration(tmp_path))},
        },
    )

    error = error_result(response)
    assert "exited with code 2" in error["content"][0]["text"]
    assert error["structuredContent"] == {
        "boundary": "local",
        "exit_code": 2,
        "stdout": "",
        "stderr": "boom",
    }


def test_launch_trader_runs_the_local_trader_process(tmp_path) -> None:
    runner = FakeRunner(ProcessOutcome(0, "session complete\n", ""))
    server = create_server(
        local_context(runner, capabilities=ToolCapabilities(trader=True))
    )
    config_path = write_benchmark_configuration(tmp_path)

    response = call(
        server,
        "tools/call",
        {"name": TOOL_LAUNCH_TRADER, "arguments": {"config_path": str(config_path)}},
    )

    assert runner.argvs == [("harness", "trader", "--config", str(config_path))]
    assert result_payload(response)["exit_code"] == 0


def test_tools_call_run_sweep_uses_the_injected_local_runner(tmp_path) -> None:
    sweeps = FakeSweeps()
    server = create_server(local_context(FakeRunner(), sweeps))
    spec_path = tmp_path / "sweep.json"

    response = call(
        server,
        "tools/call",
        {"name": TOOL_RUN_SWEEP, "arguments": {"spec_path": str(spec_path)}},
    )

    assert sweeps.spec_paths == [str(spec_path)]
    assert result_payload(response) == {
        "sweep_id": "ab" * 32,
        "requested": 2,
        "expanded": 2,
        "duplicates_skipped": 0,
        "run_ids": ["run-1", "run-2"],
    }


def test_tools_call_run_sweep_without_a_runner_reports_the_missing_boundary() -> None:
    server = create_server(local_context(FakeRunner(), sweeps=None))

    response = call(
        server,
        "tools/call",
        {"name": TOOL_RUN_SWEEP, "arguments": {"spec_path": "sweep.json"}},
    )

    assert "not composed" in error_result(response)["content"][0]["text"]


def test_get_backtest_status_maps_the_job_status_payload() -> None:
    body = {
        "job_id": "momentum-spy-2024-01",
        "state": "succeeded",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:01:00Z",
        "error_message": None,
    }
    transport = FakeRemoteTransport(get_responses=(RemoteResponse(200, {}, body),))
    server = create_server(remote_context(transport))

    response = call(
        server,
        "tools/call",
        {
            "name": TOOL_BACKTEST_STATUS,
            "arguments": {"job_id": "momentum-spy-2024-01"},
        },
    )

    url, headers = transport.get_calls[0]
    assert url == f"{API_BASE_URL}/v1/backtests/jobs/momentum-spy-2024-01"
    assert headers == {"Authorization": f"Bearer {BEARER_TOKEN}"}
    assert result_payload(response) == body


def test_get_run_and_list_artifacts_map_payloads_and_not_found() -> None:
    transport = FakeRemoteTransport(
        get_responses=(
            RemoteResponse(
                200,
                {},
                {
                    "run_id": "run-1",
                    "status": "complete",
                    "strategy_id": "spy-momentum",
                    "artifact_count": 1,
                    "total_bytes": 10,
                    "metrics": {"sharpe": "1.2345"},
                },
            ),
            RemoteResponse(
                200,
                {},
                {
                    "run_id": "run-1",
                    "artifacts": [
                        {
                            "name": "backtest.json",
                            "size": 10,
                            "object_path": "runs/run-1/backtest.json",
                            "sas_url": None,
                        }
                    ],
                },
            ),
            RemoteResponse(404, {}, {}),
        )
    )
    server = create_server(remote_context(transport))

    run_response = call(
        server, "tools/call", {"name": TOOL_GET_RUN, "arguments": {"run_id": "run-1"}}
    )
    artifacts_response = call(
        server,
        "tools/call",
        {"name": TOOL_LIST_ARTIFACTS, "arguments": {"run_id": "run-1"}},
    )
    missing_response = call(
        server, "tools/call", {"name": TOOL_GET_RUN, "arguments": {"run_id": "run-1"}}
    )

    assert result_payload(run_response)["run_id"] == "run-1"
    assert result_payload(artifacts_response)["artifacts"][0]["name"] == "backtest.json"
    assert transport.get_calls[-1][0].endswith("/v1/backtests/runs/run-1")
    assert (
        "run 'run-1' does not exist"
        in error_result(missing_response)["content"][0]["text"]
    )


def test_query_tools_report_a_missing_remote_boundary() -> None:
    server = create_server(local_context(FakeRunner()))

    response = call(
        server,
        "tools/call",
        {"name": TOOL_BACKTEST_STATUS, "arguments": {"job_id": "job-1"}},
    )

    assert "boundary is not configured" in error_result(response)["content"][0]["text"]


def test_unknown_tools_and_invalid_arguments_are_invalid_params() -> None:
    server = create_server(local_context(FakeRunner()))

    unknown = call(server, "tools/call", {"name": "nope", "arguments": {}})
    assert error_code(unknown) == -32602
    assert "unknown tool 'nope'" in unknown["error"]["message"]

    missing = call(server, "tools/call", {"name": TOOL_RUN_BACKTEST, "arguments": {}})
    assert error_code(missing) == -32602
    assert "missing required argument 'config_path'" in missing["error"]["message"]

    unexpected = call(
        server,
        "tools/call",
        {
            "name": TOOL_RUN_BACKTEST,
            "arguments": {"config_path": "a", "extra": 1},
        },
    )
    assert error_code(unexpected) == -32602
    assert "unexpected argument 'extra'" in unexpected["error"]["message"]

    typed = call(
        server,
        "tools/call",
        {"name": TOOL_RUN_BACKTEST, "arguments": {"config_path": 7}},
    )
    assert error_code(typed) == -32602
    assert "must be a string" in typed["error"]["message"]

    empty = call(
        server,
        "tools/call",
        {"name": TOOL_BACKTEST_STATUS, "arguments": {"job_id": ""}},
    )
    assert error_code(empty) == -32602

    oversized = call(
        server,
        "tools/call",
        {"name": TOOL_BACKTEST_STATUS, "arguments": {"job_id": "x" * 129}},
    )
    assert error_code(oversized) == -32602


def test_malformed_requests_produce_precise_error_objects() -> None:
    server = create_server(local_context(FakeRunner()))
    transport = InProcessTransport(server)

    assert error_code(transport.call(["not", "an", "object"])) == -32600
    assert error_code(transport.call({"method": "ping"})) == -32600
    assert (
        error_code(transport.call({"jsonrpc": "1.0", "method": "ping", "id": 1}))
        == -32600
    )
    assert (
        error_code(transport.call({"jsonrpc": "2.0", "method": 3, "id": 1})) == -32600
    )
    assert (
        error_code(transport.call({"jsonrpc": "2.0", "method": "ping", "id": True}))
        == -32600
    )
    assert (
        error_code(
            transport.call({"jsonrpc": "2.0", "method": "ping", "params": 5, "id": 1})
        )
        == -32600
    )
    assert error_code(transport.call(rpc("wat"))) == -32601

    unknown = transport.call(rpc("wat"))
    assert isinstance(unknown, dict)
    assert unknown["id"] == 1
    assert unknown["error"]["message"].startswith("unknown method 'wat'")

    broken_params = transport.call(
        {"jsonrpc": "2.0", "method": "ping", "params": {}, "id": "abc"}
    )
    assert isinstance(broken_params, dict)
    assert broken_params["id"] == "abc"

    assert transport.call({"jsonrpc": "2.0", "method": "ping"}) is None
    assert transport.call({"jsonrpc": "2.0", "method": "wat"}) is None


def test_stdio_transport_frames_newline_delimited_json_and_stops_on_eof() -> None:
    server = create_server(local_context(FakeRunner()))
    stdin = StringIO(
        '{"jsonrpc": "2.0", "id": 1, "method": "ping"}\n'
        "\n"
        "not json at all\n"
        '{"jsonrpc": "2.0", "method": "ping"}\n'
    )
    stdout = StringIO()
    stderr = StringIO()

    exit_code = serve_stdio(server, stdin=stdin, stdout=stdout, stderr=stderr)

    assert exit_code == 0
    lines = [decode_message(line) for line in stdout.getvalue().splitlines()]
    assert len(lines) == 2
    assert lines[0] == {"jsonrpc": "2.0", "id": 1, "result": {}}
    parse_error = lines[1]
    assert isinstance(parse_error, dict)
    assert parse_error["error"]["code"] == -32700
    assert parse_error["id"] is None
    assert "shutdown after 2 responses" in stderr.getvalue()
    assert stdout.getvalue().endswith("\n")
    for line in stdout.getvalue().splitlines():
        assert line == encode_message(decode_message(line))


def test_serialized_settings_and_server_state_carry_no_credentials() -> None:
    context = HarnessToolsContext(
        mode="local",
        credentials=token_source(BEARER_TOKEN),
        api_base_url=API_BASE_URL,
        runner=FakeRunner(),
        sweep_submitter=FakeSweeps(),
    )
    server = create_server(context)

    settings = json.dumps(context.as_settings(), sort_keys=True)
    described = json.dumps(server.describe(), sort_keys=True)

    assert BEARER_TOKEN not in settings
    assert BEARER_TOKEN not in described
    assert "HARNESS_API_BEARER_TOKEN" in settings
    assert json.loads(settings)["bearer_configured"] is True
    assert json.loads(settings)["capabilities"]["trader"] is False
    assert sorted(json.loads(described)["tools"]) == sorted(DEFAULT_TOOLS)

    unconfigured = json.dumps(
        HarnessToolsContext(
            mode="local",
            credentials=token_source(None),
            runner=FakeRunner(),
        ).as_settings()
    )
    assert json.loads(unconfigured)["bearer_configured"] is False


def test_tool_execution_error_carries_structured_payload() -> None:
    error = ToolExecutionError("boom", payload={"exit_code": 3})

    assert str(error) == "boom"
    assert error.payload == {"exit_code": 3}
    assert ToolExecutionError("plain").payload is None


def test_cli_registers_the_mcp_subcommand() -> None:
    from harness.cli import app as harness_app

    root_help = CliRunner().invoke(harness_app, ["--help"])
    assert root_help.exit_code == 0
    assert "mcp" in root_help.stdout

    command_help = CliRunner().invoke(harness_app, ["mcp", "--help"])
    assert command_help.exit_code == 0
    assert "Serve the harness MCP server over stdio." in command_help.stdout
    assert "--enable-trader" in command_help.stdout
