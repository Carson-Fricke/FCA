"""Serving capability isolation and artifact-backed Strategies HTTP reads."""

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from thesis.agent.runtime.context import ToolContext
from thesis.agent.runtime.conversation import Conversations
from thesis.agent.runtime.store import PersistenceSubAgentStore
from thesis.chat import ChatService
from thesis.chat.contracts import SubAgentRecord
from thesis.http import serving
from thesis.http.application import create_app
from thesis.http.workspaces import build_local_workspace_data
from thesis.providers.openrouter import OpenRouterProfile, OpenRouterProvider
from thesis.workspaces.local import LocalWorkspaceProvider
from thesis.workspaces.manifest import load_workspace_manifest


class _Key:
    def key(self) -> str:
        return "test-key"


class _Transport:
    def post(self, *args: object, **kwargs: object) -> tuple[int, str]:
        raise AssertionError("no model invocation during composition")


def test_design_tool_to_files_http_and_completion_isolation(
    tmp_path: Path, monkeypatch: Any
) -> None:
    provider = LocalWorkspaceProvider(tmp_path / "workspaces")
    manifest = load_workspace_manifest("config/thesis/workspace.yaml")
    provider.create(manifest)
    wid = manifest.workspace_id
    chat = ChatService()
    data = build_local_workspace_data(
        workspace_root=provider.root, workspace_repositories=provider, chat_service=chat
    )
    artifacts = data.strategy_artifacts
    assert artifacts is not None
    callbacks: dict[str, Any] = {}

    def conversations(**kwargs: Any) -> Conversations:
        callbacks["registry"] = kwargs["registry_factory"]
        return Conversations(**kwargs)

    def control(**kwargs: Any) -> Any:
        callbacks["validator"] = kwargs["contract_validator"]
        return serving.ControlToolService(**kwargs)

    monkeypatch.setattr(serving, "Conversations", conversations)
    stack = serving.build_serving_stack(
        chat_service=chat,
        model_provider=OpenRouterProvider(
            OpenRouterProfile(profile_id="test", model="test/model"),
            _Key(),
            _Transport(),
        ),
        strategy_artifacts=artifacts,
        control_factory=control,
    )
    store = PersistenceSubAgentStore(chat.persistence)
    record = SubAgentRecord(
        workspace_id=wid,
        created_at=datetime.now(UTC),
        created_by="operator",
        sub_agent_id="design-1",
        chat_id="chat-1",
        role="coding",
        phase="strategy_design",
        workflow_execution_id="exec-1",
        allowed_tools=("author_strategy",),
    )
    store.save_sub_agent(record)

    def context(node: str | None, execution: str = "exec-1") -> ToolContext:
        return ToolContext(
            workspace_id=wid,
            chat_id="chat-1",
            conversation_id=node or "chat-1",
            sub_agent_id=node,
            workflow_execution_id=execution,
            actor_id="agent",
            allowed_tools=("author_strategy",),
        )

    registry = callbacks["registry"](context("design-1"))
    arguments: dict[str, object] = {
        "name": "Momentum",
        "symbol": "SPY",
        "description": "Accepted design",
        "strategy": {
            "strategyId": "momentum",
            "version": "1.0.0",
            "entrypoint": "src/lib.rs",
            "files": {
                "src/lib.rs": "use harness_strategy_sdk::{StrategyProgram, export_strategy_program};\n"
                "struct Momentum;\nimpl StrategyProgram for Momentum {\n"
                "fn new(_: harness_strategy_sdk::serde_json::Value) -> Self { Self }\n}\n"
                "export_strategy_program!(Momentum);\n"
            },
            "tests": {
                "tests/program.rs": "#[test]\nfn smoke() { assert_eq!(1, 1); }\n"
            },
            "harnessConfiguration": {},
            "exitCriteria": [
                {
                    "falsifierId": "f1",
                    "observable": "signal",
                    "condition": "lt",
                    "threshold": 0.0,
                    "description": "Flatten on falsification",
                }
            ],
            "limitations": [],
        },
    }
    result = registry.dispatch("author_strategy", arguments)
    assert result.ok
    receipt = result.structured
    assert isinstance(receipt, Mapping)
    assert callbacks["validator"](wid, record, {"strategy": receipt}) is None
    assert (
        callbacks["validator"](
            wid, record, {"strategy": {**receipt, "bundleSha256": "f" * 64}}
        )
        is not None
    )
    assert callbacks["validator"](wid, record, {"strategy": {}}) is not None
    assert (
        not callbacks["registry"](context(None))
        .dispatch("author_strategy", arguments)
        .ok
    )
    assert (
        not callbacks["registry"](context("design-1", "other-exec"))
        .dispatch("author_strategy", arguments)
        .ok
    )
    helper = SubAgentRecord(
        workspace_id=wid,
        created_at=datetime.now(UTC),
        sub_agent_id="helper",
        created_by="agent",
        chat_id="chat-1",
        role="coding",
        parent_sub_agent_id="design-1",
        workflow_execution_id="exec-1",
        allowed_tools=("author_strategy",),
    )
    store.save_sub_agent(helper)
    assert (
        not callbacks["registry"](context("helper"))
        .dispatch("author_strategy", arguments)
        .ok
    )
    app = create_app(
        bearer_token="test-token", chat_service=stack.chat_service, workspace_data=data
    )
    client = TestClient(app)
    headers = {"Authorization": "Bearer test-token"}
    response = client.get(f"/v1/workspaces/{wid}/strategies", headers=headers)
    assert response.status_code == 200
    assert response.json()["items"][0]["id"] == "momentum"
    assert "impl StrategyProgram" in response.json()["items"][0]["code"]
    assert (
        client.post(
            f"/v1/workspaces/{wid}/strategies",
            headers=headers,
            json={"name": "Phantom", "symbol": "SPY"},
        ).status_code
        == 409
    )
    restarted = build_local_workspace_data(workspace_root=provider.root)
    assert restarted.strategies.list_strategies(wid) == data.strategies.list_strategies(
        wid
    )
