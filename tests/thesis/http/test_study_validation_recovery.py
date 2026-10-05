"""ID-based validation, durable failure receipts, and pinned workflow replay."""

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest
from tests.thesis.workflows.registry.hypothesis_development._support import (
    _discovery_envelope,
    _hypothesis_envelope,
    _legacy_model,
)

from thesis.agent.harness_tools import (
    HarnessToolFailure,
    HarnessToolProvider,
    HarnessToolResult,
    HarnessToolSpec,
)
from thesis.agent.messages import from_record
from thesis.agent.runtime.context import ToolContext
from thesis.agent.runtime.conversation import Conversations
from thesis.agent.runtime.store import PersistenceSubAgentStore
from thesis.chat import ChatService
from thesis.http import serving
from thesis.persistence.git import GitCoordinator
from thesis.providers.openrouter import OpenRouterProvider
from thesis.workflows.core.execution_store import InMemoryExecutionStore
from thesis.workspaces.local import LocalWorkspaceProvider
from thesis.workspaces.manifest import load_workspace_manifest


class Harness(HarnessToolProvider):
    """Both harness ports share the same observable transport fixture."""

    def validation_identity(self) -> str:
        return self.compiler_identity

    def current_server_info(self) -> Mapping[str, object]:
        from thesis.workspaces.programs import WorkspacePrograms

        # Fresh packaged SDK metadata, without an inherited uninitialized client.
        programs = WorkspacePrograms(cast(Any, None))
        version, abi = programs._sdk_metadata(programs.sdk_root)
        return {
            "sdkIdentity": programs._sdk_identity(),
            "sdkVersion": version,
            "sdkAbiVersion": abi,
            "validationIdentity": self.validation_identity(),
        }

    def __init__(self) -> None:
        self.calls: list[tuple[str, Mapping[str, object]]] = []
        self.reject = False
        self.compiler_identity = "test-harness-v1"
        self.pending_backtest = False

    def list_tools(self):
        return tuple(
            HarnessToolSpec(
                name=name,
                description="Inspect run evidence",
                input_schema={"type": "object"},
            )
            for name in (
                "get_job_status",
                "get_run",
                "list_artifacts",
                "read_artifact",
            )
        )

    def call_tool(self, name, arguments=None):
        arguments = arguments or {}
        self.calls.append((name, arguments or {}))
        if name.startswith("validate_"):
            return HarnessToolResult(
                tool=name,
                ok=not self.reject,
                structured={
                    "valid": not self.reject,
                    "diagnostics": [{"message": "unresolved import", "level": "error"}]
                    if self.reject
                    else [],
                },
                failure=HarnessToolFailure(
                    kind="harness",
                    message="unresolved import",
                    deterministic_compiler_diagnostic=True,
                )
                if self.reject
                else None,
            )
        if name == "get_job_status":
            if any(tool == "run_backtest" for tool, _ in self.calls):
                return HarnessToolResult(
                    tool=name,
                    ok=True,
                    structured={"job_id": arguments["job_id"], "state": "complete"},
                )
            return HarnessToolResult(
                tool=name,
                ok=False,
                failure=HarnessToolFailure(
                    kind="tool",
                    message=f"job '{arguments['job_id']}' does not exist",
                ),
            )
        if name == "run_backtest" and self.pending_backtest:
            return HarnessToolResult(
                tool=name,
                ok=True,
                structured={"job_id": "study-run", "state": "running"},
            )
        return HarnessToolResult(
            tool=name,
            ok=True,
            structured={
                "job_id": "study-run",
                "state": "complete",
                "metrics": {"sharpe": 1.0, "fit.baseline": 0.5},
                "artifacts": [],
            },
        )


def setup_workflow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repositories = LocalWorkspaceProvider(tmp_path / "workspaces")
    manifest = load_workspace_manifest("config/thesis/workspace.yaml")
    repositories.create(manifest)
    chat = ChatService()
    harness = Harness()
    callbacks: dict[str, Any] = {}

    def conversations(**kwargs):
        callbacks["registry"] = kwargs["registry_factory"]
        return Conversations(**kwargs, spawn_thread=lambda work: None)

    def control(**kwargs):
        callbacks["validate"] = kwargs["contract_validator"]
        callbacks["apply"] = kwargs["phase_completions"]
        return serving.ControlToolService(**kwargs)

    monkeypatch.setattr(serving, "Conversations", conversations)
    work = []
    stack = serving.build_serving_stack(
        chat_service=chat,
        model_provider=cast(OpenRouterProvider, object()),
        harness_tools=harness,
        workspace_repositories=repositories,
        execution_store=InMemoryExecutionStore(),
        detach=work.append,
        control_factory=control,
    )
    assert stack.workflow_runtime is not None
    workflow = stack.workflow_runtime.build_hypothesis_development(chat=chat)
    wid = manifest.workspace_id
    thread = chat.create_chat(wid, title="Validation", owner_actor_id="operator")
    trigger = chat.create_message(
        wid, thread.chat_id, actor_id="operator", body="Study momentum"
    )
    request = workflow.launch(
        wid,
        thread.chat_id,
        actor_id="operator",
        ideas="Study momentum",
        trigger_message_id=trigger.message_id,
    )
    node = workflow.prepare_phase(request)
    assert node is not None
    workflow.apply_phase_completion(
        request, node, {"target": "model_creation", "discovery": _discovery_envelope()}
    )
    work.pop(0)()
    node = workflow.active_orchestrators[request.execution_id].sub_agent_id
    record = PersistenceSubAgentStore(chat.persistence).get_sub_agent(wid, node)
    assert record is not None
    context = ToolContext(
        workspace_id=wid,
        chat_id=thread.chat_id,
        conversation_id=node,
        sub_agent_id=node,
        actor_id="operator",
        workflow_execution_id=request.execution_id,
        phase_executor=True,
        allowed_tools=record.allowed_tools,
    )
    registry = callbacks["registry"](context)
    legacy = _legacy_model()
    configuration = {
        **cast(dict, legacy["harnessConfiguration"]),
        "subscriptions": [
            {"data_type": "bars", "symbols": ["SPY"], "interval": "1min"}
        ],
    }
    authored = registry.dispatch(
        "author_study",
        {
            "program_id": "momentum",
            "source": legacy["source"],
            "configuration": configuration,
        },
    )
    assert authored.ok
    return (
        repositories,
        stack,
        workflow,
        request,
        record,
        registry,
        harness,
        callbacks,
        work,
    )


def completion_from(receipt: Mapping[str, object]) -> dict[str, object]:
    reference = {
        key: receipt[key]
        for key in ("workspaceId", "kind", "programId", "gitCommit", "version")
    }
    model = _legacy_model()
    return {
        "intentEpoch": 0,
        "target": "falsification_trials",
        "hypothesis": {
            **_hypothesis_envelope(),
            "model": {
                **reference,
                "description": model["description"],
                "outputs": model["outputs"],
                "tests": model["tests"],
            },
        },
    }


def test_author_validate_complete_then_trials_use_pinned_commit(tmp_path, monkeypatch):
    (
        repositories,
        stack,
        workflow,
        request,
        record,
        registry,
        harness,
        callbacks,
        work,
    ) = setup_workflow(tmp_path, monkeypatch)
    checked = registry.dispatch("validate_study", {"program_id": "momentum"})
    assert checked.ok
    completion = completion_from(checked.structured)
    validate_count = len(harness.calls)
    assert callbacks["validate"](request.workspace_id, record, completion) is None
    assert len(harness.calls) == validate_count  # Completion never recompiles.
    callbacks["apply"](request.workspace_id, record, completion)
    original = str(_legacy_model()["source"])
    updated = registry.dispatch(
        "update_study",
        {
            "program_id": "momentum",
            "expected_version": checked.structured["version"],
            "expected": 'publish("sharpe", 1.0)',
            "replacement": 'publish("sharpe", -9.0)',
        },
    )
    assert updated.ok and updated.structured["version"] != checked.structured["version"]
    work.pop(0)()
    assert (
        stack.chat_service.get_execution(
            request.workspace_id, request.execution_id
        ).phase
        == "verdict"
    )
    launch = next(args for name, args in harness.calls if name == "run_study")
    source = launch["config"]["strategy_instances"][0]["parameters"]["source"]
    assert source == original
    assert "-9.0" not in source
    stored = (
        workflow.execution_store.get(request.execution_id).transitions[1].completion
    )
    assert "source" not in stored["hypothesis"]["model"]
    assert "harnessConfiguration" not in stored["hypothesis"]["model"]
    validation = repositories.resolve_path(
        request.workspace_id, "studies/momentum/validation.json", must_exist=True
    )
    assert validation.is_file()
    assert (
        GitCoordinator(repositories.open(request.workspace_id).root).head()
        != checked.structured["gitCommit"]
    )


def test_root_catalog_failure_is_a_durable_notice_without_fake_tool(monkeypatch):
    from thesis.agent.harness_tools import HarnessToolError

    chat = ChatService()
    thread = chat.create_chat("workspace", title="Catalog", owner_actor_id="operator")
    chat.create_message("workspace", thread.chat_id, actor_id="operator", body="Help")
    failure = HarnessToolFailure(
        kind="transport", message="catalog TLS certificate verify failed"
    )

    def fail_registry(**kwargs):
        raise HarnessToolError(failure)

    monkeypatch.setattr(serving, "build_session_registry", fail_registry)
    stack = serving.build_serving_stack(
        chat_service=chat, model_provider=cast(OpenRouterProvider, object())
    )
    assert stack.conversations is not None
    assert stack.conversations.root("workspace", thread.chat_id).run_turn() is None
    canonical = from_record(chat.list_messages("workspace", thread.chat_id)[-1])
    assert canonical.direction == "incoming"
    assert canonical.content.source == "runtime"
    assert canonical.content.result is not None
    assert canonical.content.result["failure"] == failure.as_dict()
    assert canonical.content.correlation is None
    assert canonical.content.requests == ()


def test_failed_validation_requires_patch_and_revalidate_id(tmp_path, monkeypatch):
    _, stack, _, request, record, registry, harness, callbacks, _ = setup_workflow(
        tmp_path, monkeypatch
    )
    harness.reject = True
    checked = registry.dispatch("validate_study", {"program_id": "momentum"})
    assert not checked.ok
    completion = completion_from(checked.structured)
    assert "accepted validation" in callbacks["validate"](
        request.workspace_id, record, completion
    )
    harness.reject = False
    patched = registry.dispatch(
        "update_study",
        {
            "program_id": "momentum",
            "expected_version": checked.structured["version"],
            "expected": 'publish("sharpe", 1.0)',
            "replacement": 'publish("sharpe", 2.0)',
        },
    )
    assert patched.ok
    checked = registry.dispatch("validate_study", {"program_id": "momentum"})
    assert checked.ok
    assert (
        callbacks["validate"](
            request.workspace_id, record, completion_from(checked.structured)
        )
        is None
    )
    messages = stack.chat_service.list_messages(request.workspace_id, request.chat_id)
    finished = [
        message
        for message in messages
        if (message.data or {}).get("tool") == "verify_program_reference"
        and message.tool_result
    ]
    assert len(finished) == 2
    for message in finished:
        canonical = from_record(message)
        assert canonical.direction == "incoming"
        assert canonical.content.source == "runtime"
        assert canonical.content.requests == ()


def test_compiler_change_invalidates_stored_completion_until_id_revalidated(
    tmp_path, monkeypatch
):
    _, _, _, request, record, registry, harness, callbacks, _ = setup_workflow(
        tmp_path, monkeypatch
    )
    checked = registry.dispatch("validate_study", {"program_id": "momentum"})
    assert checked.ok
    completion = completion_from(checked.structured)
    assert callbacks["validate"](request.workspace_id, record, completion) is None
    harness.compiler_identity = "test-harness-v2"
    assert "compiler identity" in callbacks["validate"](
        request.workspace_id, record, completion
    )
    revalidated = registry.dispatch("validate_study", {"program_id": "momentum"})
    assert revalidated.ok
    assert revalidated.structured["version"] == checked.structured["version"]
    assert revalidated.structured["validationIdentity"] == "test-harness-v2"
    assert (
        callbacks["validate"](
            request.workspace_id, record, completion_from(revalidated.structured)
        )
        is None
    )


def test_inline_validation_and_foreign_or_stale_completion_are_rejected(
    tmp_path, monkeypatch
):
    _, _, _, request, record, registry, _, callbacks, _ = setup_workflow(
        tmp_path, monkeypatch
    )
    assert not registry.dispatch("validate_study", {"model": _legacy_model()}).ok
    checked = registry.dispatch("validate_study", {"program_id": "momentum"})
    completion = completion_from(checked.structured)
    helper = replace(record, phase_executor=False)
    assert "owning phase executor" in callbacks["validate"](
        request.workspace_id, helper, completion
    )
    cast(dict, completion["hypothesis"])["model"]["workspaceId"] = "foreign-workspace"
    assert "workspace" in callbacks["validate"](
        request.workspace_id, record, completion
    )
    completion = completion_from(checked.structured)
    # A unique patch changes committed content; stale references never complete.
    update = registry.dispatch(
        "update_study",
        {
            "program_id": "momentum",
            "expected_version": checked.structured["version"],
            "expected": 'publish("sharpe", 1.0)',
            "replacement": 'publish("sharpe", 2.0)',
        },
    )
    assert update.ok
    assert "stale" in callbacks["validate"](request.workspace_id, record, completion)


def test_strategy_backtest_agent_runs_latest_validated_id(tmp_path, monkeypatch):
    from thesis.workspaces.programs import STRATEGY_TEMPLATE, WorkspacePrograms

    (
        repositories,
        stack,
        workflow,
        request,
        record,
        registry,
        harness,
        callbacks,
        work,
    ) = setup_workflow(tmp_path, monkeypatch)
    study = registry.dispatch("validate_study", {"program_id": "momentum"})
    callbacks["apply"](request.workspace_id, record, completion_from(study.structured))
    work.pop(0)()
    verdict_node = workflow.active_orchestrators[request.execution_id].sub_agent_id
    workflow.apply_phase_completion(
        request,
        verdict_node,
        {
            "target": "strategy_design",
            "verdict": {"outcome": "supported", "reason": "accepted"},
        },
    )
    work.pop(0)()
    design_node = workflow.active_orchestrators[request.execution_id].sub_agent_id
    design = PersistenceSubAgentStore(stack.chat_service.persistence).get_sub_agent(
        request.workspace_id, design_node
    )
    assert design is not None
    context = ToolContext(
        workspace_id=request.workspace_id,
        chat_id=request.chat_id,
        conversation_id=design_node,
        sub_agent_id=design_node,
        actor_id="operator",
        workflow_execution_id=request.execution_id,
        phase_executor=True,
        allowed_tools=design.allowed_tools,
    )
    registry = callbacks["registry"](context)
    config = {
        "start": "2024-01-01T00:00:00Z",
        "end": "2024-01-02T00:00:00Z",
        "subscriptions": [
            {"data_type": "bars", "symbols": ["SPY"], "interval": "1min"}
        ],
    }
    assert registry.dispatch(
        "author_strategy",
        {"program_id": "alpha", "source": STRATEGY_TEMPLATE, "configuration": config},
    ).ok
    checked = registry.dispatch("validate_strategy", {"program_id": "alpha"})
    assert checked.ok
    reference = {
        key: checked.structured[key]
        for key in ("workspaceId", "kind", "programId", "gitCommit", "version")
    }
    completion = {
        "strategy": reference,
        "target": "strategy_backtesting",
        "intentEpoch": 0,
    }
    assert callbacks["validate"](request.workspace_id, design, completion) is None
    callbacks["apply"](request.workspace_id, design, completion)
    updated = registry.dispatch(
        "update_strategy",
        {
            "program_id": "alpha",
            "expected_version": reference["version"],
            "expected": "struct AuthoredStrategy;",
            "replacement": "struct AuthoredStrategy; // changed latest",
            "configuration": {**config, "end": "2024-01-03T00:00:00Z"},
        },
    )
    assert updated.ok
    work.pop(0)()
    execution = stack.chat_service.get_execution(
        request.workspace_id, request.execution_id
    )
    assert execution.phase == "strategy_backtesting"
    assert not any(name == "run_backtest" for name, _ in harness.calls)
    backtest_node = workflow.active_orchestrators[request.execution_id].sub_agent_id
    backtest = PersistenceSubAgentStore(stack.chat_service.persistence).get_sub_agent(
        request.workspace_id, backtest_node
    )
    assert backtest is not None and backtest.phase_executor
    backtest_registry = callbacks["registry"](
        replace(
            context,
            conversation_id=backtest_node,
            sub_agent_id=backtest_node,
            allowed_tools=backtest.allowed_tools,
        )
    )
    assert not backtest_registry.dispatch("run_backtest", {"program_id": "alpha"}).ok
    assert not any(name == "run_backtest" for name, _ in harness.calls)
    assert registry.dispatch("validate_strategy", {"program_id": "alpha"}).ok
    harness.pending_backtest = True
    result = backtest_registry.dispatch("run_backtest", {"program_id": "alpha"})
    assert result.ok
    assert result.structured["result"]["state"] == "running"
    assert "metrics" not in result.structured["result"]
    launch = next(
        arguments for name, arguments in harness.calls if name == "run_backtest"
    )
    instance = launch["config"]["strategy_instances"][0]
    latest_source = STRATEGY_TEMPLATE.replace(
        "struct AuthoredStrategy;", "struct AuthoredStrategy; // changed latest"
    )
    assert instance["parameters"]["source"] == latest_source
    assert launch["config"]["end"] == "2024-01-03T00:00:00Z"
    import hashlib

    assert instance["bundle_hash"] == hashlib.sha256(latest_source.encode()).hexdigest()
    assert (
        stack.chat_service.get_execution(
            request.workspace_id, request.execution_id
        ).phase
        == "strategy_backtesting"
    )
    for tool, arguments in (
        ("get_job_status", {"job_id": "study-run"}),
        ("get_run", {"run_id": "study-run"}),
        ("list_artifacts", {"run_id": "study-run"}),
        ("read_artifact", {"run_id": "study-run", "name": "metrics.json"}),
        ("read_sdk_reference", {"topic": "strategy"}),
    ):
        assert backtest_registry.dispatch(tool, arguments).ok
    executed_reference = {
        key: result.structured[key]
        for key in ("workspaceId", "kind", "programId", "gitCommit", "version")
    }
    workflow.apply_phase_completion(
        request,
        backtest_node,
        {
            "target": "optimization",
            "backtest": dict(result.structured),
            "strategy": executed_reference,
        },
    )
    work.pop(0)()
    assert (
        stack.chat_service.get_execution(
            request.workspace_id, request.execution_id
        ).phase
        == "optimization"
    )
    optimization_node = workflow.active_orchestrators[request.execution_id].sub_agent_id
    optimization = PersistenceSubAgentStore(
        stack.chat_service.persistence
    ).get_sub_agent(request.workspace_id, optimization_node)
    assert optimization is not None
    optimization_registry = callbacks["registry"](
        replace(
            context,
            conversation_id=optimization_node,
            sub_agent_id=optimization_node,
            allowed_tools=optimization.allowed_tools,
        )
    )
    assert optimization_registry.dispatch("read_strategy", {"program_id": "alpha"}).ok
    assert optimization_registry.dispatch(
        "read_sdk_reference", {"topic": "strategy"}
    ).ok
    assert optimization_registry.dispatch("run_backtest", {"program_id": "alpha"}).ok
    for tool, arguments in (
        ("get_job_status", {"job_id": "study-run"}),
        ("get_run", {"run_id": "study-run"}),
        ("list_artifacts", {"run_id": "study-run"}),
        ("read_artifact", {"run_id": "study-run", "name": "metrics.json"}),
    ):
        assert optimization_registry.dispatch(tool, arguments).ok
    assert (
        stack.chat_service.get_execution(
            request.workspace_id, request.execution_id
        ).phase
        == "optimization"
    )
    assert (
        WorkspacePrograms(repositories).load(request.workspace_id, "strategy", "alpha")[
            "version"
        ]
        != reference["version"]
    )


@pytest.mark.parametrize("failed_activity", ["started", "finished"])
def test_completion_receipt_writer_failure_propagates(
    tmp_path, monkeypatch, failed_activity
):
    from thesis.agent.runtime.session import SessionPersistenceError

    _, stack, _, request, record, registry, _, callbacks, _ = setup_workflow(
        tmp_path, monkeypatch
    )
    checked = registry.dispatch("validate_study", {"program_id": "momentum"})
    create = stack.chat_service.create_message

    def fail_write(*args, **kwargs):
        if kwargs.get("data", {}).get("toolActivity") == failed_activity:
            raise OSError("receipt writer unavailable")
        return create(*args, **kwargs)

    monkeypatch.setattr(stack.chat_service, "create_message", fail_write)
    with pytest.raises(SessionPersistenceError):
        callbacks["validate"](
            request.workspace_id, record, completion_from(checked.structured)
        )


def test_trials_pin_indicator_dependencies_and_configuration(tmp_path, monkeypatch):
    from thesis.workspaces.programs import INDICATOR_TEMPLATE, WorkspacePrograms

    repositories, _, _, request, record, registry, harness, callbacks, work = (
        setup_workflow(tmp_path, monkeypatch)
    )
    programs = WorkspacePrograms(repositories, harness)
    snapshot = programs.load(request.workspace_id, "study", "momentum")
    configuration = snapshot["configuration"]
    assert registry.dispatch(
        "author_indicator",
        {
            "program_id": "signal",
            "source": INDICATOR_TEMPLATE,
            "configuration": configuration,
        },
    ).ok
    updated = registry.dispatch(
        "update_study",
        {
            "program_id": "momentum",
            "expected_version": snapshot["version"],
            "indicator_ids": ["signal"],
        },
    )
    assert updated.ok
    checked = registry.dispatch("validate_study", {"program_id": "momentum"})
    assert checked.ok
    accepted = programs.load(
        request.workspace_id, "study", "momentum", str(checked.structured["gitCommit"])
    )
    completion = completion_from(checked.structured)
    assert callbacks["validate"](request.workspace_id, record, completion) is None
    callbacks["apply"](request.workspace_id, record, completion)
    indicator = registry.dispatch("read_indicator", {"program_id": "signal"})
    assert registry.dispatch(
        "update_indicator",
        {
            "program_id": "signal",
            "expected_version": indicator.structured["version"],
            "expected": "pub struct AuthoredIndicator",
            "replacement": "// changed dependency\npub struct AuthoredIndicator",
        },
    ).ok
    work.pop(0)()
    launch = next(arguments for name, arguments in harness.calls if name == "run_study")
    assert (
        launch["config"]["strategy_instances"][0]["parameters"]["source"]
        == accepted["source"]
    )
    assert "changed dependency" not in str(accepted["source"])
