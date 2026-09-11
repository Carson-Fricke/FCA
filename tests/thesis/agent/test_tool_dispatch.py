"""Policy-scoped harness tool dispatch for the Thesis agent.

These tests prove the last mile between the model router and the harness MCP
tool provider: a role policy that selects ``harness-mcp`` resolves the skill
bundle and permits the harness tools, and a permitted tool request executes
end-to-end over both the local in-process/stdio and remote HTTP ``/mcp``
topologies.  Denied and unknown tools surface as typed failures before any
transport exchange.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import cast

import httpx
import pytest

from thesis.agent.harness_tools import (
    HARNESS_MCP_SKILL_ID,
    HARNESS_MCP_SKILL_VERSION,
    HARNESS_TOOL_NAMES,
    HarnessToolProvider,
    harness_mcp_skill,
    open_harness_tool_provider,
)
from thesis.agent.router import (
    ModelProfile,
    ModelProvider,
    ModelResponse,
    ModelRouter,
    Role,
    RolePolicy,
)
from thesis.agent.skills import SkillRef, SkillRegistry
from thesis.agent.tool_dispatch import (
    HarnessToolCall,
    HarnessToolDispatcher,
    harness_mcp_role_policy,
    with_harness_mcp,
)
from thesis.integrations.mcp import HarnessMcpTopology
from thesis.workflows import BacktestRunner
from thesis.workflows.runtime import build_model_router, build_workflow_runtime

HARNESS_URL = "http://harness.test:9000"
BEARER = "harness-secret"

_TOOL_NAMES = {name: {"type": "object"} for name in HARNESS_TOOL_NAMES}


class FakeHandler:
    """In-process MCP handler publishing the whole harness catalog."""

    def __init__(self, *, tools: tuple[str, ...] = HARNESS_TOOL_NAMES) -> None:
        self.calls: list[dict[str, object]] = []
        self._tools = tools

    def handle(self, request: object) -> dict[str, object] | None:
        if not isinstance(request, Mapping):
            raise TypeError("request must be a mapping")
        message = dict(request)
        if "id" not in message:
            return None
        method = message.get("method")
        if method == "initialize":
            result: dict[str, object] = {"protocolVersion": "2024-11-05"}
        elif method == "tools/list":
            result = {
                "tools": [
                    {"name": name, "description": "", "inputSchema": schema}
                    for name, schema in _TOOL_NAMES.items()
                    if name in self._tools
                ]
            }
        elif method == "tools/call":
            params = message.get("params")
            assert isinstance(params, dict)
            self.calls.append(dict(params))
            result = {
                "isError": False,
                "content": [{"type": "text", "text": "ok"}],
                "structuredContent": {"jobId": "job-1", "tool": params.get("name")},
            }
        else:
            result = {}
        return {"jsonrpc": "2.0", "id": message.get("id"), "result": result}


class FakeHttpServer:
    """Minimal MCP-over-HTTP harness server for the ``POST /mcp`` contract."""

    def __init__(
        self, *, token: str | None = BEARER, require_token: bool = True
    ) -> None:
        self.token = token
        self.require_token = require_token
        self.seen: list[tuple[str, str, str | None, object]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        authorization = request.headers.get("Authorization")
        body = json.loads(request.content) if request.content else None
        self.seen.append((request.method, request.url.path, authorization, body))
        if self.require_token and authorization != f"Bearer {self.token}":
            return httpx.Response(401, json={"error": "unauthorized"})
        if request.method != "POST" or request.url.path != "/mcp":
            return httpx.Response(404, json={})
        assert isinstance(body, dict)
        if "id" not in body:
            return httpx.Response(202)
        return httpx.Response(200, json=self._dispatch(body))

    @staticmethod
    def _dispatch(body: dict[str, object]) -> dict[str, object]:
        method = body.get("method")
        if method == "initialize":
            result: dict[str, object] = {"protocolVersion": "2024-11-05"}
        elif method == "tools/list":
            result = {
                "tools": [
                    {"name": name, "description": "", "inputSchema": schema}
                    for name, schema in _TOOL_NAMES.items()
                ]
            }
        elif method == "tools/call":
            params = body["params"]
            assert isinstance(params, dict)
            result = {
                "isError": False,
                "content": [{"type": "text", "text": "ok"}],
                "structuredContent": {"jobId": "job-1", "tool": params.get("name")},
            }
        else:
            result = {}
        return {"jsonrpc": "2.0", "id": body.get("id"), "result": result}


class HandlerTransport:
    """Stdio-seam double dispatching against an in-process handler."""

    def __init__(self, handler: FakeHandler) -> None:
        self._handler = handler

    def exchange(self, request: Mapping[str, object]) -> Mapping[str, object]:
        response = self._handler.handle(dict(request))
        assert response is not None
        return response

    def notify(self, request: Mapping[str, object]) -> None:
        self._handler.handle(dict(request))

    def close(self) -> None:
        return None


class FakeModelProvider:
    def invoke(
        self,
        *,
        profile: ModelProfile,
        prompt: object,
        allowed_tools: tuple[str, ...],
    ) -> ModelResponse:
        del profile, prompt, allowed_tools
        return ModelResponse(output="ok")


class FakeRunner:
    def run(self, request: object) -> object:
        del request
        raise AssertionError("the tool dispatcher test never runs a backtest")


def _build_router(
    *,
    policies: Mapping[Role, RolePolicy] | None = None,
    skills: SkillRegistry | None = None,
) -> ModelRouter:
    selected: Mapping[Role, RolePolicy] = policies or {
        "research": harness_mcp_role_policy(
            "research", model_profile="research-profile", version="1.0.0"
        )
    }
    providers: dict[str, ModelProvider] = {"fake": FakeModelProvider()}
    return build_model_router(
        role_policies=selected,
        model_profiles={
            "research-profile": ModelProfile(
                profile_id="research-profile",
                version="1.0.0",
                provider="fake",
                model="research-model",
            )
        },
        providers=providers,
        skills=SkillRegistry() if skills is None else skills,
    )


def _local_provider(
    handler: FakeHandler | None = None,
) -> tuple[HarnessToolProvider, FakeHandler]:
    selected = FakeHandler() if handler is None else handler
    provider = open_harness_tool_provider(
        HarnessMcpTopology(thesis_location="local", harness_location="local"),
        in_process_handler=selected,
    )
    return provider, selected


def _remote_provider(server: FakeHttpServer) -> HarnessToolProvider:
    return open_harness_tool_provider(
        HarnessMcpTopology(
            harness_location="remote",
            harness_url=HARNESS_URL,
            bearer_token=BEARER,
        ),
        http_transport=httpx.MockTransport(server.handler),
    )


def test_build_model_router_registers_harness_skill() -> None:
    skills = SkillRegistry()

    _build_router(skills=skills)

    assert skills.resolve(HARNESS_MCP_SKILL_ID) == harness_mcp_skill()


def test_role_policy_resolves_harness_skill_bundle_and_tools() -> None:
    router = _build_router()

    decision = router.resolve("research")

    assert (
        f"{HARNESS_MCP_SKILL_ID}@{HARNESS_MCP_SKILL_VERSION}"
        in decision.skill_bundle.versions
    )
    assert set(HARNESS_TOOL_NAMES) <= set(decision.allowed_tools)


def test_local_in_process_dispatch_invokes_harness_tool() -> None:
    provider, handler = _local_provider()
    dispatcher = HarnessToolDispatcher(_build_router(), provider)

    result = dispatcher.dispatch(
        "research", "run_backtest", {"config_path": "cfg/bt.yaml"}
    )

    assert result.ok, result.failure
    assert result.structured["tool"] == "run_backtest"
    assert handler.calls[-1] == {
        "name": "run_backtest",
        "arguments": {"config_path": "cfg/bt.yaml"},
    }


def test_stdio_dispatch_invokes_harness_tool() -> None:
    handler = FakeHandler()
    provider = open_harness_tool_provider(
        HarnessMcpTopology(thesis_location="remote", harness_location="local"),
        stdio_transport=HandlerTransport(handler),
    )
    dispatcher = HarnessToolDispatcher(_build_router(), provider)

    result = dispatcher.dispatch(
        "research", HarnessToolCall("get_run", {"run_id": "run-7"})
    )

    assert result.ok, result.failure
    assert handler.calls[-1] == {"name": "get_run", "arguments": {"run_id": "run-7"}}


def test_remote_http_dispatch_invokes_harness_tool() -> None:
    server = FakeHttpServer()
    provider = _remote_provider(server)
    dispatcher = HarnessToolDispatcher(_build_router(), provider)

    result = dispatcher.dispatch(
        "research", "launch_trader", {"config_path": "cfg/trader.yaml"}
    )

    assert result.ok, result.failure
    assert result.structured["jobId"] == "job-1"
    posted = [seen for seen in server.seen if seen[1] == "/mcp"]
    assert all(seen[2] == f"Bearer {BEARER}" for seen in posted)
    payloads = [seen[3] for seen in posted if isinstance(seen[3], dict)]
    call = next(body for body in payloads if body.get("method") == "tools/call")
    assert call["params"] == {
        "name": "launch_trader",
        "arguments": {"config_path": "cfg/trader.yaml"},
    }


def test_dispatch_denies_tool_not_permitted_by_policy() -> None:
    policy = RolePolicy(
        role="research",
        version="1.0.0",
        model_profile="research-profile",
        skills=("harness-mcp",),
        allowed_tools=("get_run",),
    )
    provider, handler = _local_provider()
    dispatcher = HarnessToolDispatcher(
        _build_router(policies={"research": policy}), provider
    )

    result = dispatcher.dispatch("research", "run_backtest", {"config_path": "x"})

    assert result.ok is False
    assert result.failure is not None
    assert result.failure.kind == "denied"
    assert handler.calls == []


def test_dispatch_reports_unknown_tool_as_unknown() -> None:
    provider, handler = _local_provider()
    dispatcher = HarnessToolDispatcher(_build_router(), provider)

    result = dispatcher.dispatch("research", "delete_everything")

    assert result.failure is not None
    assert result.failure.kind == "unknown_tool"
    assert handler.calls == []


def test_dispatch_many_runs_a_model_turn_in_order() -> None:
    provider, handler = _local_provider()
    dispatcher = HarnessToolDispatcher(_build_router(), provider)

    results = dispatcher.dispatch_many(
        "research",
        (
            HarnessToolCall("get_run", {"run_id": "run-1"}),
            HarnessToolCall.from_mapping(
                {"name": "list_artifacts", "arguments": {"run_id": "run-1"}}
            ),
        ),
    )

    assert [result.tool for result in results] == ["get_run", "list_artifacts"]
    assert all(result.ok for result in results)
    assert [call["name"] for call in handler.calls] == ["get_run", "list_artifacts"]


def test_with_harness_mcp_merges_skill_and_tools() -> None:
    base = RolePolicy(
        role="coding",
        version="1.0.0",
        model_profile="research-profile",
        skills=("existing-skill",),
        allowed_tools=("read_workspace",),
    )

    augmented = with_harness_mcp(base)

    keys = {
        (skill if isinstance(skill, SkillRef) else SkillRef.parse(skill)).key
        for skill in augmented.skills
    }
    assert keys == {"existing-skill", f"{HARNESS_MCP_SKILL_ID}@1.0.0"}
    assert "read_workspace" in augmented.allowed_tools
    assert set(HARNESS_TOOL_NAMES) <= set(augmented.allowed_tools)
    assert base.allowed_tools == ("read_workspace",)


def test_workflow_runtime_exposes_policy_scoped_dispatcher() -> None:
    provider, handler = _local_provider()
    runtime = build_workflow_runtime(
        role_policies={
            "research": harness_mcp_role_policy(
                "research", model_profile="research-profile", version="1.0.0"
            )
        },
        model_profiles={
            "research-profile": ModelProfile(
                profile_id="research-profile",
                version="1.0.0",
                provider="fake",
                model="research-model",
            )
        },
        providers={"fake": FakeModelProvider()},
        skills=SkillRegistry(),
        runner=cast(BacktestRunner, FakeRunner()),
        harness_tools=provider,
    )

    assert runtime.tool_dispatcher is not None
    result = runtime.dispatch_tool("research", "get_run", {"run_id": "run-1"})

    assert result.ok, result.failure
    assert handler.calls[-1] == {"name": "get_run", "arguments": {"run_id": "run-1"}}


def test_harness_tool_call_validates_and_serializes() -> None:
    call = HarnessToolCall.from_mapping(
        {"name": "run_backtest", "arguments": {"config_path": "cfg.yaml"}}
    )

    assert call.as_dict() == {
        "name": "run_backtest",
        "arguments": {"config_path": "cfg.yaml"},
    }
    assert HarnessToolCall.from_mapping({"name": "get_run"}).arguments == {}

    with pytest.raises(ValueError, match="name"):
        HarnessToolCall("  ")
    with pytest.raises(ValueError, match="string 'name'"):
        HarnessToolCall.from_mapping({"name": 1})
    with pytest.raises(ValueError, match="must be an object"):
        HarnessToolCall.from_mapping({"name": "get_run", "arguments": "no"})
