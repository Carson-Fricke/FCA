"""Serving capability isolation and committed strategy reference completion."""

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from tests.thesis.http.test_study_validation_recovery import Harness

from thesis.agent.runtime.context import ToolContext
from thesis.agent.runtime.conversation import Conversations
from thesis.agent.runtime.store import PersistenceSubAgentStore
from thesis.chat import ChatService
from thesis.chat.contracts import CompletionMode, SubAgentRecord, WorkflowDefinition
from thesis.http import serving
from thesis.http.application import create_app
from thesis.http.workspaces import build_local_workspace_data
from thesis.providers.openrouter import OpenRouterProfile, OpenRouterProvider
from thesis.workspaces.local import LocalWorkspaceProvider
from thesis.workspaces.manifest import load_workspace_manifest
from thesis.workspaces.programs import STRATEGY_TEMPLATE, WorkspacePrograms


class _Key:
    def key(self) -> str:
        return "test-key"


class _Transport:
    def post(self, *args: object, **kwargs: object) -> tuple[int, str]:
        raise AssertionError("no model invocation during composition")


@pytest.mark.slow
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
        callbacks["apply"] = kwargs["phase_completions"]
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
        workspace_repositories=provider,
        control_factory=control,
    )
    store = PersistenceSubAgentStore(chat.persistence)
    assert stack.workflow_runtime is None
    thread = chat.create_chat(wid, title="Strategy design", owner_actor_id="operator")
    execution = chat.launch_workflow(
        wid,
        thread.chat_id,
        workflow=WorkflowDefinition(
            workflow_id="hypothesis-development",
            version="1.0.0",
            title="Strategy design",
            phases=("strategy_design",),
        ),
        actor_id="operator",
        body="Design a strategy",
    )
    record = SubAgentRecord(
        workspace_id=wid,
        created_at=datetime.now(UTC),
        created_by="operator",
        sub_agent_id="design-1",
        chat_id=thread.chat_id,
        role="coding",
        phase="strategy_design",
        workflow_execution_id=execution.execution_id,
        phase_executor=True,
        phase_attempt_id="strategy-design-attempt",
        completion_mode=CompletionMode.EXPLICIT_COMPLETION,
        allowed_tools=("author_strategy", "validate_strategy"),
    )
    store.save_sub_agent(record)

    def context(
        node: str | None,
        execution_id: str = execution.execution_id,
        *,
        phase_executor: bool = True,
    ) -> ToolContext:
        return ToolContext(
            workspace_id=wid,
            chat_id=thread.chat_id,
            conversation_id=node or thread.chat_id,
            sub_agent_id=node,
            workflow_execution_id=execution_id,
            phase_executor=phase_executor,
            actor_id="agent",
            allowed_tools=("author_strategy", "validate_strategy"),
        )

    registry = callbacks["registry"](context("design-1"))
    arguments: dict[str, object] = {
        "program_id": "momentum",
        "source": STRATEGY_TEMPLATE,
        "configuration": {
            "start": "2024-01-01T00:00:00Z",
            "end": "2024-01-02T00:00:00Z",
            "subscriptions": [
                {"data_type": "bars", "symbols": ["SPY"], "interval": "1min"}
            ],
        },
    }
    result = registry.dispatch("author_strategy", arguments)
    assert result.ok
    assert "source" not in result.structured
    checked = WorkspacePrograms(provider, Harness()).validate(
        wid, "strategy", "momentum"
    )
    receipt = {
        key: checked[key]
        for key in ("workspaceId", "kind", "programId", "gitCommit", "version")
    }
    assert isinstance(receipt, Mapping)
    assert "compiler identity" in callbacks["validator"](
        wid, record, {"strategy": receipt, "intentEpoch": execution.intent_epoch}
    )  # A model-only stack cannot attest to the current serving compiler.
    from thesis.workflows.registry.hypothesis_development.session_services import (
        HypothesisSessionServices,
    )

    configured = HypothesisSessionServices(
        chat=chat,
        store=store,
        harness_tools=None,
        programs=WorkspacePrograms(provider, Harness()),
        activity=lambda context, tool, arguments, execute: execute(),
    )
    assert (
        configured.validate_completion(
            wid, record, {"strategy": receipt, "intentEpoch": execution.intent_epoch}
        )
        is None
    )
    assert (
        callbacks["apply"](
            wid, record, {"strategy": receipt, "intentEpoch": execution.intent_epoch}
        )
        is None
    )
    assert (
        callbacks["validator"](
            wid,
            record,
            {
                "strategy": {**receipt, "version": "f" * 64},
                "intentEpoch": execution.intent_epoch,
            },
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
    wrong_chat = ToolContext(
        workspace_id=wid,
        chat_id="other-chat",
        conversation_id="design-1",
        sub_agent_id="design-1",
        workflow_execution_id=execution.execution_id,
        phase_executor=True,
        actor_id="agent",
        allowed_tools=("author_strategy",),
    )
    assert (
        not callbacks["registry"](wrong_chat).dispatch("author_strategy", arguments).ok
    )
    helper = SubAgentRecord(
        workspace_id=wid,
        created_at=datetime.now(UTC),
        sub_agent_id="helper",
        created_by="agent",
        chat_id=thread.chat_id,
        role="coding",
        parent_sub_agent_id="design-1",
        workflow_execution_id=execution.execution_id,
        phase_executor=False,
        completion_mode=CompletionMode.EXPLICIT_COMPLETION,
        allowed_tools=("author_strategy",),
    )
    store.save_sub_agent(helper)
    assert (
        not callbacks["registry"](context("helper", phase_executor=False))
        .dispatch("author_strategy", arguments)
        .ok
    )
    assert (
        callbacks["validator"](
            wid, helper, {"strategy": receipt, "intentEpoch": execution.intent_epoch}
        )
        is not None
    )
    app = create_app(
        bearer_token="test-token", chat_service=stack.chat_service, workspace_data=data
    )
    client = TestClient(app)
    headers = {"Authorization": "Bearer test-token"}
    response = client.get(f"/v1/workspaces/{wid}/strategies", headers=headers)
    assert response.status_code == 200
    # The legacy Strategies catalog must not become a second program truth.
    assert response.json()["items"] == []
    loaded = WorkspacePrograms(provider).load(
        wid, "strategy", "momentum", str(receipt["gitCommit"])
    )
    assert loaded["source"] == STRATEGY_TEMPLATE
    assert loaded["validation"] is None  # Validation receipt names the source commit.
    latest_validation = WorkspacePrograms(provider).load(wid, "strategy", "momentum")[
        "validation"
    ]
    assert (
        isinstance(latest_validation, Mapping) and latest_validation["accepted"] is True
    )
    assert (
        client.post(
            f"/v1/workspaces/{wid}/strategies",
            headers=headers,
            json={"name": "Phantom", "symbol": "SPY"},
        ).status_code
        == 422
    )
    restarted = build_local_workspace_data(workspace_root=provider.root)
    assert restarted.strategies.list_strategies(wid) == data.strategies.list_strategies(
        wid
    )
