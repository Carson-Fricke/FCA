"""ID-based validation, durable failure receipts, and pinned workflow replay."""

import json
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from tests.thesis.extensions.workflows.hypothesis_development._support import (
    _discovery_envelope,
    _hypothesis_envelope,
    _inline_model,
    _only_active_phase,
    _phase_attempt,
    _sketch_document,
)
from tests.thesis.submissions import MemorySubmissions
from tests.thesis.workspaces._support import MemoryGitFactory, Repositories

from thesis.agent.harness_tools import (
    HarnessToolFailure,
    HarnessToolProvider,
    HarnessToolResult,
    HarnessToolSpec,
)
from thesis.application import composition as serving
from thesis.experiments.mandate import (
    resolve_research_mandate,
    rfc3339,
)
from thesis.research.hypothesis import InMemoryHypothesisStore
from thesis.research.models import InMemoryResearchModelStore
from thesis.workspaces.manifest import load_workspace_manifest
from thesis.workspaces.programs import WorkspacePrograms
from thesis.workspaces.sdk import SdkReferences
from thesis_runtime.adapters.providers.openrouter import OpenRouterProvider
from thesis_runtime.contracts.context import ToolContext
from thesis_runtime.contracts.messages import from_record
from thesis_runtime.contracts.records import ToolJobState
from thesis_runtime.conversations import ChatService
from thesis_runtime.conversations.conversation import Conversations
from thesis_runtime.subagents.store import PersistenceSubAgentStore
from thesis_runtime.workflows.execution_store import InMemoryExecutionStore


class Harness(HarnessToolProvider):
    """Both harness ports share the same observable transport fixture."""

    def validation_identity(self) -> str:
        return self.compiler_identity

    def current_server_info(self) -> Mapping[str, object]:
        from thesis.workspaces.programs import WorkspacePrograms

        # Fresh packaged SDK metadata, without an inherited uninitialized client.
        programs = WorkspacePrograms(
            cast(Any, None), submissions=MemorySubmissions(), sdk=SdkReferences()
        )
        sdk = programs.sdk.matched_metadata()
        return {
            "sdkIdentity": sdk["sdkIdentity"],
            "sdkVersion": sdk["sdkVersion"],
            "sdkAbiVersion": sdk["sdkAbiVersion"],
            "validationIdentity": self.validation_identity(),
        }

    def __init__(self) -> None:
        self.calls: list[tuple[str, Mapping[str, object]]] = []
        self.reject = False
        self.compiler_identity = "test-harness-v1"
        self.pending_backtest = False
        self.refuse_backtest = False

    def list_tools(self):
        return tuple(
            HarnessToolSpec(
                name=name,
                description="Inspect job evidence",
                input_schema={"type": "object"},
            )
            for name in (
                "get_job_status",
                "get_job_result",
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
                    structured={"job_id": arguments["job_id"], "state": "succeeded"},
                )
            return HarnessToolResult(
                tool=name,
                ok=False,
                failure=HarnessToolFailure(
                    kind="tool",
                    message=f"job '{arguments['job_id']}' does not exist",
                ),
            )
        if name == "run_backtest" and self.refuse_backtest:
            return HarnessToolResult(
                tool=name,
                ok=False,
                failure=HarnessToolFailure(
                    kind="harness",
                    message="refused before admission",
                    execution_certainty="not_started",
                ),
            )
        if name == "run_backtest" and self.pending_backtest:
            return HarnessToolResult(
                tool=name,
                ok=True,
                structured={
                    "job_id": self._submitted_job_id(arguments),
                    "state": "running",
                },
            )
        if name == "read_artifact":
            return HarnessToolResult(
                tool=name,
                ok=True,
                structured={
                    "job_id": arguments["job_id"],
                    "name": arguments["name"],
                    "encoding": "utf-8",
                    "content": json.dumps({"sharpe": 1.0, "fit.baseline": 0.5}),
                },
            )
        return HarnessToolResult(
            tool=name,
            ok=True,
            structured={
                "job_id": self._submitted_job_id(arguments),
                "state": "succeeded",
                "metrics": {"sharpe": 1.0, "fit.baseline": 0.5},
                "artifacts": [],
            },
        )

    @staticmethod
    def _submitted_job_id(arguments):
        config = arguments.get("config")
        if isinstance(config, Mapping):
            candidate = config.get("job_id")
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        candidate = arguments.get("job_id")
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
        return "job-study-run"


def setup_workflow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    factory = MemoryGitFactory()
    repositories = Repositories(tmp_path / "workspaces", factory)
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

    class MemoryPrograms(WorkspacePrograms):
        def __init__(self, repository_provider, harness_tools=None, **kwargs):
            super().__init__(
                repository_provider,
                harness_tools,
                git_factory=factory,
                **kwargs,
            )

    monkeypatch.setattr(serving, "Conversations", conversations)
    monkeypatch.setattr(serving, "WorkspacePrograms", MemoryPrograms)
    monkeypatch.setattr(
        serving,
        "_workspace_stores",
        lambda workspace_root, workspace_repositories=None: (
            InMemoryResearchModelStore(),
            InMemoryHypothesisStore(),
        ),
    )
    work = []
    stack = serving.build_serving_stack(
        chat_service=chat,
        model_provider=cast(
            OpenRouterProvider,
            SimpleNamespace(model="test/model", provider_name="openrouter"),
        ),
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
    trigger = chat.send_message(
        wid, thread.chat_id, actor_id="operator", body="Study momentum"
    )
    request = workflow.launch(
        wid,
        thread.chat_id,
        actor_id="operator",
        inputs={
            "idea": "Study momentum",
        },
        trigger_message_id=trigger.message_id,
    )
    node = workflow.prepare_phase(request)
    assert node is not None
    discovery_record = PersistenceSubAgentStore(chat.persistence).get_sub_agent(
        wid, node
    )
    assert discovery_record is not None
    discovery_registry = callbacks["registry"](
        ToolContext(
            workspace_id=wid,
            chat_id=thread.chat_id,
            conversation_id=node,
            sub_agent_id=node,
            actor_id="operator",
            workflow_execution_id=request.execution_id,
            phase_executor=True,
            allowed_tools=discovery_record.allowed_tools,
        )
    )
    setup_context = ToolContext(
        workspace_id=wid,
        chat_id=thread.chat_id,
        conversation_id=node,
        sub_agent_id=node,
        actor_id="operator",
        workflow_execution_id=request.execution_id,
        phase_attempt_id=discovery_record.phase_attempt_id,
        phase_executor=True,
        allowed_tools=discovery_record.allowed_tools,
    )
    assert discovery_registry.dispatch(
        "set_research_window",
        {
            "development": {
                "start": "2021-01-01T00:00:00Z",
                "end": "2026-06-01T00:00:00Z",
            },
            "forward": {"start": "2026-06-01T00:00:00Z", "end": "2026-09-01T00:00:00Z"},
        },
        context=setup_context,
    ).ok
    discovery_registry = callbacks["registry"](setup_context)
    sketch = discovery_registry.dispatch(
        "author_strategy_sketch",
        {
            "sketch_id": "momentum-edge",
            "sketch": _sketch_document(),
        },
        context=ToolContext(
            workspace_id=wid,
            chat_id=thread.chat_id,
            conversation_id=node,
            sub_agent_id=node,
            actor_id="operator",
            workflow_execution_id=request.execution_id,
            phase_executor=True,
            allowed_tools=discovery_record.allowed_tools,
        ),
    )
    assert sketch.ok, sketch.text
    callbacks["sketchReference"] = sketch.structured["reference"]
    discovery = _discovery_envelope()
    discovery["sketchReference"] = callbacks["sketchReference"]
    workflow.apply_phase_completion(
        request,
        node,
        {"target": "model_creation", "discovery": discovery},
        attempt_id=_phase_attempt(workflow, request.workspace_id, node),
    )
    work.pop(0)()
    node = workflow.active_orchestrators[
        (
            request.execution_id,
            _only_active_phase(workflow, request.workspace_id, request.execution_id),
        )
    ].sub_agent_id
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
    legacy = _inline_model()
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
        context=context,
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


def completion_from(
    receipt: Mapping[str, object], sketch_reference: Mapping[str, object]
) -> dict[str, object]:
    reference = {
        key: receipt[key]
        for key in ("workspaceId", "kind", "programId", "gitCommit", "version")
    }
    model = _inline_model()
    return {
        "intentEpoch": 0,
        "target": "falsification_trials",
        "hypothesis": {
            **_hypothesis_envelope(),
            "sketchReference": dict(sketch_reference),
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
    checked = registry.dispatch(
        "validate_study",
        {"program_id": "momentum"},
        context=invocation_context(request, record),
    )
    assert checked.ok
    completion = completion_from(checked.structured, callbacks["sketchReference"])
    validate_count = len(harness.calls)
    assert callbacks["validate"](request.workspace_id, record, completion) is None
    assert len(harness.calls) == validate_count  # Completion never recompiles.
    callbacks["apply"](request.workspace_id, record, completion)
    original = str(_inline_model()["source"])
    updated = registry.dispatch(
        "update_study",
        {
            "program_id": "momentum",
            "expected_version": checked.structured["version"],
            "expected": 'publish("sharpe", 1.0)',
            "replacement": 'publish("sharpe", -9.0)',
        },
        context=invocation_context(request, record),
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
        repositories.store(request.workspace_id).head != checked.structured["gitCommit"]
    )


def test_root_catalog_failure_is_a_durable_notice_without_fake_tool(monkeypatch):
    from thesis.agent.harness_tools import HarnessToolError

    chat = ChatService()
    thread = chat.create_chat("workspace", title="Catalog", owner_actor_id="operator")
    chat.send_message("workspace", thread.chat_id, actor_id="operator", body="Help")
    failure = HarnessToolFailure(
        kind="transport", message="catalog TLS certificate verify failed"
    )

    def fail_registry(**kwargs):
        raise HarnessToolError(failure)

    monkeypatch.setattr(serving, "build_session_registry", fail_registry)
    stack = serving.build_serving_stack(
        chat_service=chat,
        model_provider=cast(
            OpenRouterProvider,
            SimpleNamespace(model="test/model", provider_name="openrouter"),
        ),
    )
    try:
        assert stack.conversations is not None
        assert stack.conversations.root("workspace", thread.chat_id).run_turn() is None
        canonical = from_record(chat.list_messages("workspace", thread.chat_id)[-1])
        assert canonical.direction == "incoming"
        assert canonical.content.source == "runtime"
        assert canonical.content.result is not None
        assert canonical.content.result["failure"] == failure.as_dict()
        assert canonical.content.correlation is None
        assert canonical.content.requests == ()
    finally:
        chat.delete_chat("workspace", thread.chat_id)


def test_failed_validation_requires_patch_and_revalidate_id(tmp_path, monkeypatch):
    _, stack, _, request, record, registry, harness, callbacks, _ = setup_workflow(
        tmp_path, monkeypatch
    )
    harness.reject = True
    checked = registry.dispatch(
        "validate_study",
        {"program_id": "momentum"},
        context=invocation_context(request, record),
    )
    assert not checked.ok
    completion = completion_from(checked.structured, callbacks["sketchReference"])
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
        context=invocation_context(request, record),
    )
    assert patched.ok
    checked = registry.dispatch(
        "validate_study",
        {"program_id": "momentum"},
        context=invocation_context(request, record),
    )
    assert checked.ok
    assert (
        callbacks["validate"](
            request.workspace_id,
            record,
            completion_from(checked.structured, callbacks["sketchReference"]),
        )
        is None
    )
    messages = stack.chat_service.list_messages(request.workspace_id, request.chat_id)
    finished = [
        message
        for message in messages
        if message.tool_result
        and message.tool_result.get("tool") == "verify_program_reference"
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
    checked = registry.dispatch(
        "validate_study",
        {"program_id": "momentum"},
        context=invocation_context(request, record),
    )
    assert checked.ok
    completion = completion_from(checked.structured, callbacks["sketchReference"])
    assert callbacks["validate"](request.workspace_id, record, completion) is None
    harness.compiler_identity = "test-harness-v2"
    assert "compiler identity" in callbacks["validate"](
        request.workspace_id, record, completion
    )
    revalidated = registry.dispatch(
        "validate_study",
        {"program_id": "momentum"},
        context=invocation_context(request, record),
    )
    assert revalidated.ok
    assert revalidated.structured["version"] == checked.structured["version"]
    assert revalidated.structured["validationIdentity"] == "test-harness-v2"
    assert (
        callbacks["validate"](
            request.workspace_id,
            record,
            completion_from(revalidated.structured, callbacks["sketchReference"]),
        )
        is None
    )


def test_inline_validation_and_foreign_or_stale_completion_are_rejected(
    tmp_path, monkeypatch
):
    _, _, _, request, record, registry, _, callbacks, _ = setup_workflow(
        tmp_path, monkeypatch
    )
    assert not registry.dispatch(
        "validate_study",
        {"model": _inline_model()},
        context=invocation_context(request, record),
    ).ok
    checked = registry.dispatch(
        "validate_study",
        {"program_id": "momentum"},
        context=invocation_context(request, record),
    )
    completion = completion_from(checked.structured, callbacks["sketchReference"])
    helper = replace(record, phase_executor=False)
    assert "owning phase executor" in callbacks["validate"](
        request.workspace_id, helper, completion
    )
    cast(dict, completion["hypothesis"])["model"]["workspaceId"] = "foreign-workspace"
    assert "workspace" in callbacks["validate"](
        request.workspace_id, record, completion
    )
    completion = completion_from(checked.structured, callbacks["sketchReference"])
    # A unique patch changes committed content; stale references never complete.
    update = registry.dispatch(
        "update_study",
        {
            "program_id": "momentum",
            "expected_version": checked.structured["version"],
            "expected": 'publish("sharpe", 1.0)',
            "replacement": 'publish("sharpe", 2.0)',
        },
        context=invocation_context(request, record),
    )
    assert update.ok
    assert "stale" in callbacks["validate"](request.workspace_id, record, completion)


def test_strategy_backtest_agent_runs_latest_validated_id(tmp_path, monkeypatch):
    from thesis.workspaces.programs import WorkspacePrograms
    from thesis.workspaces.sdk.templates import STRATEGY_TEMPLATE

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
    study = registry.dispatch(
        "validate_study",
        {"program_id": "momentum"},
        context=invocation_context(request, record),
    )
    callbacks["apply"](
        request.workspace_id,
        record,
        completion_from(study.structured, callbacks["sketchReference"]),
    )
    work.pop(0)()
    verdict_node = workflow.active_orchestrators[
        (
            request.execution_id,
            _only_active_phase(workflow, request.workspace_id, request.execution_id),
        )
    ].sub_agent_id
    workflow.apply_phase_completion(
        request,
        verdict_node,
        {
            "target": "strategy_design",
            "verdict": {
                "outcome": "supported",
                "reason": "accepted",
                "sketchReference": callbacks["sketchReference"],
                "assumptionAssessments": [
                    {
                        "assumptionId": "continuation",
                        "status": "supported",
                        "reason": "Trial survived",
                        "testIds": ["T01"],
                    }
                ],
            },
        },
        attempt_id=_phase_attempt(workflow, request.workspace_id, verdict_node),
    )
    work.pop(0)()
    design_node = workflow.active_orchestrators[
        (
            request.execution_id,
            _only_active_phase(workflow, request.workspace_id, request.execution_id),
        )
    ].sub_agent_id
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
        {
            "program_id": "alpha",
            "source": STRATEGY_TEMPLATE,
            "configuration": config,
            "parameter_declaration": {
                "fast": {
                    "type": "integer",
                    "default": 5,
                    "minimum": 1,
                    "maximum": 20,
                }
            },
        },
        context=context,
    ).ok
    checked = registry.dispatch(
        "validate_strategy", {"program_id": "alpha"}, context=context
    )
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
            "replacement": "struct AuthoredStrategy /* changed latest */;",
            "configuration": {**config, "end": "2024-01-03T00:00:00Z"},
        },
        context=context,
    )
    assert updated.ok
    work.pop(0)()
    execution = stack.chat_service.get_execution(
        request.workspace_id, request.execution_id
    )
    assert execution.phase == "strategy_backtesting"
    assert not any(name == "run_backtest" for name, _ in harness.calls)
    backtest_node = workflow.active_orchestrators[
        (
            request.execution_id,
            _only_active_phase(workflow, request.workspace_id, request.execution_id),
        )
    ].sub_agent_id
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
    assert not backtest_registry.dispatch(
        "run_backtest",
        {"program_id": "alpha"},
        context=replace(
            context,
            conversation_id=backtest_node,
            sub_agent_id=backtest_node,
            allowed_tools=backtest.allowed_tools,
        ),
    ).ok
    assert not any(name == "run_backtest" for name, _ in harness.calls)
    assert registry.dispatch(
        "validate_strategy", {"program_id": "alpha"}, context=context
    ).ok
    harness.pending_backtest = True
    result = backtest_registry.dispatch(
        "run_backtest",
        {"program_id": "alpha"},
        context=replace(
            context,
            conversation_id=backtest_node,
            sub_agent_id=backtest_node,
            allowed_tools=backtest.allowed_tools,
        ),
    )
    assert result.ok
    executed_job_id = cast(str, result.structured["job_id"])
    retained = stack.chat_service.find_tool_job(request.workspace_id, executed_job_id)
    assert retained is not None
    assert retained.attribution["programId"] == "alpha"
    assert retained.attribution["kind"] == "strategy"
    assert retained.state is ToolJobState.RUNNING
    assert retained.submission_certainty == "unknown_outcome"
    assert result.structured["result"]["state"] == "running"
    assert result.pending_jobs == (executed_job_id,)
    assert "metrics" not in result.structured["result"]
    harness.refuse_backtest = True
    refused = backtest_registry.dispatch(
        "run_backtest",
        {"program_id": "alpha"},
        context=replace(
            context,
            conversation_id=backtest_node,
            sub_agent_id=backtest_node,
            allowed_tools=backtest.allowed_tools,
        ),
    )
    harness.refuse_backtest = False
    assert not refused.ok
    refused_launch = [
        arguments for name, arguments in harness.calls if name == "run_backtest"
    ][-1]
    refused_job_id = str(cast(Mapping[str, object], refused_launch["config"])["job_id"])
    refused_job = stack.chat_service.find_tool_job(request.workspace_id, refused_job_id)
    assert refused_job is not None
    assert refused_job.state is ToolJobState.FAILED
    assert refused_job.submission_certainty == "not_started"
    launch = next(
        arguments for name, arguments in harness.calls if name == "run_backtest"
    )
    instance = launch["config"]["strategy_instances"][0]
    latest_source = STRATEGY_TEMPLATE.replace(
        "struct AuthoredStrategy;", "struct AuthoredStrategy /* changed latest */;"
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
        ("get_job_status", {"job_id": "job-study-run"}),
        ("get_job_result", {"job_id": "job-study-run"}),
        ("list_artifacts", {"job_id": "job-study-run"}),
        ("read_artifact", {"job_id": "job-study-run", "name": "metrics.json"}),
        ("read_sdk_reference", {"topic": "strategy"}),
    ):
        assert backtest_registry.dispatch(
            tool,
            arguments,
            context=replace(
                context,
                conversation_id=backtest_node,
                sub_agent_id=backtest_node,
                allowed_tools=backtest.allowed_tools,
            ),
        ).ok
    executed_reference = {
        key: result.structured[key]
        for key in ("workspaceId", "kind", "programId", "gitCommit", "version")
    }
    workflow.apply_phase_completion(
        request,
        backtest_node,
        {
            "target": "optimization",
            "backtest": {
                "jobId": executed_job_id,
                "state": "succeeded",
                "strategy": executed_reference,
            },
        },
        attempt_id=_phase_attempt(workflow, request.workspace_id, backtest_node),
    )
    work.pop(0)()
    assert (
        stack.chat_service.get_execution(
            request.workspace_id, request.execution_id
        ).phase
        == "optimization"
    )
    optimization_node = workflow.active_orchestrators[
        (
            request.execution_id,
            _only_active_phase(workflow, request.workspace_id, request.execution_id),
        )
    ].sub_agent_id
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
    assert optimization_registry.dispatch(
        "read_strategy",
        {"program_id": "alpha"},
        context=replace(
            context,
            conversation_id=optimization_node,
            sub_agent_id=optimization_node,
            allowed_tools=optimization.allowed_tools,
        ),
    ).ok
    assert optimization_registry.dispatch(
        "read_sdk_reference",
        {"topic": "strategy"},
        context=replace(
            context,
            conversation_id=optimization_node,
            sub_agent_id=optimization_node,
            allowed_tools=optimization.allowed_tools,
        ),
    ).ok
    assert optimization_registry.dispatch(
        "run_backtest",
        {"program_id": "alpha"},
        context=replace(
            context,
            conversation_id=optimization_node,
            sub_agent_id=optimization_node,
            allowed_tools=optimization.allowed_tools,
        ),
    ).ok
    for tool, arguments in (
        ("get_job_status", {"job_id": "job-study-run"}),
        ("get_job_result", {"job_id": "job-study-run"}),
        ("list_artifacts", {"job_id": "job-study-run"}),
        ("read_artifact", {"job_id": "job-study-run", "name": "metrics.json"}),
    ):
        assert optimization_registry.dispatch(
            tool,
            arguments,
            context=replace(
                context,
                conversation_id=optimization_node,
                sub_agent_id=optimization_node,
                allowed_tools=optimization.allowed_tools,
            ),
        ).ok
    assert (
        stack.chat_service.get_execution(
            request.workspace_id, request.execution_id
        ).phase
        == "optimization"
    )
    batch = optimization_registry.dispatch(
        "run_backtest_batch",
        {
            "program_id": "alpha",
            "assignments": [{"fast": 5}, {"fast": 10}, {"fast": 20}],
        },
        context=replace(
            context,
            conversation_id=optimization_node,
            sub_agent_id=optimization_node,
            allowed_tools=optimization.allowed_tools,
        ),
    )
    assert batch.ok
    batch_id = cast(str, batch.structured["batchId"])
    points = cast(list[dict[str, object]], batch.structured["points"])
    assert [point["state"] for point in points] == ["running"] * 3
    assert batch.pending_jobs == tuple(point["jobId"] for point in points)
    for point in points:
        job_id = cast(str, point["jobId"])
        assert optimization_registry.dispatch(
            "get_job_status",
            {"job_id": job_id},
            context=replace(
                context,
                conversation_id=optimization_node,
                sub_agent_id=optimization_node,
                allowed_tools=optimization.allowed_tools,
            ),
        ).ok
        assert optimization_registry.dispatch(
            "get_job_result",
            {"job_id": job_id},
            context=replace(
                context,
                conversation_id=optimization_node,
                sub_agent_id=optimization_node,
                allowed_tools=optimization.allowed_tools,
            ),
        ).ok
    evaluation = {
        "strategy": executed_reference,
        "batchId": batch_id,
        "assignments": [
            {
                "parameters": point["parameters"],
                "jobId": point["jobId"],
                "state": "succeeded",
                "metrics": {"sharpe": 1.0, "fit.baseline": 0.5},
                "error": None,
            }
            for point in points
        ],
        "shortlist": [
            {
                "parameters": points[0]["parameters"],
                "jobId": points[0]["jobId"],
                "rationale": (
                    "Best cost-adjusted return with the steadiest neighborhood."
                ),
            }
        ],
        "selectionRule": (
            "Net total return and profit factor after costs, with drawdown and "
            "turnover penalties and parameter diversity."
        ),
        "limitations": "One symbol and one in-sample window.",
    }
    workflow.apply_phase_completion(
        request,
        optimization_node,
        {"target": "null_testing", "optimization": evaluation},
        attempt_id=_phase_attempt(workflow, request.workspace_id, optimization_node),
    )
    work.pop(0)()
    assert (
        stack.chat_service.get_execution(
            request.workspace_id, request.execution_id
        ).phase
        == "null_testing"
    )
    null_node = workflow.active_orchestrators[
        (
            request.execution_id,
            _only_active_phase(workflow, request.workspace_id, request.execution_id),
        )
    ].sub_agent_id
    null_testing = PersistenceSubAgentStore(
        stack.chat_service.persistence
    ).get_sub_agent(request.workspace_id, null_node)
    assert null_testing is not None
    null_registry = callbacks["registry"](
        replace(
            context,
            conversation_id=null_node,
            sub_agent_id=null_node,
            allowed_tools=null_testing.allowed_tools,
        )
    )
    assert null_registry.dispatch(
        "run_backtest",
        {"program_id": "alpha"},
        context=replace(
            context,
            conversation_id=null_node,
            sub_agent_id=null_node,
            allowed_tools=null_testing.allowed_tools,
        ),
    ).ok
    null_batch = null_registry.dispatch(
        "run_null_batch",
        {
            "program_id": "alpha",
            "parameters": {"fast": 5},
            "mode": "block_permutation",
            "block_size": 5,
            "seeds": [1, 2],
        },
        context=replace(
            context,
            conversation_id=null_node,
            sub_agent_id=null_node,
            allowed_tools=null_testing.allowed_tools,
        ),
    )
    assert null_batch.ok
    null_points = cast(list[dict[str, object]], null_batch.structured["points"])
    assert [point["seed"] for point in null_points] == [1, 2]
    assert all(isinstance(point["jobId"], str) for point in null_points)
    observed_run = cast(list[dict[str, object]], batch.structured["points"])[0]["jobId"]
    null_evaluation = {
        "strategy": executed_reference,
        "batchId": null_batch.structured["batchId"],
        "signalAdequacy": {
            "signal": "lagged returns",
            "destroyedDependency": "inter_block_order",
            "preservedProperties": ["marginal_returns", "within_block_dependence"],
            "limitations": "Calendar volatility schedule is not preserved.",
        },
        "evidenceScope": "selection_conditional",
        "failedReplicatePolicy": "conservative_exceedance",
        "targetResolution": 0.5,
        "mode": "block_permutation",
        "blockSize": 5,
        "statistic": "sharpe",
        "direction": "upper",
        "observed": {
            "jobId": observed_run,
            "metrics": {"sharpe": 1.0, "fit.baseline": 0.5},
            "value": 1.0,
        },
        "replicates": [
            {
                "seed": point["seed"],
                "jobId": point["jobId"],
                "state": "succeeded",
                "metrics": {"sharpe": 1.0, "fit.baseline": 0.5},
                "error": None,
            }
            for point in null_points
        ],
        "exceedanceCount": 2,
        "pValue": 1.0,
        "limitations": "Two seeds and one block size on one symbol.",
    }
    workflow.apply_phase_completion(
        request,
        null_node,
        {"target": "variation", "nullTesting": null_evaluation},
        attempt_id=_phase_attempt(workflow, request.workspace_id, null_node),
    )
    work.pop(0)()
    assert (
        stack.chat_service.get_execution(
            request.workspace_id, request.execution_id
        ).phase
        == "variation"
    )
    variation_node = workflow.active_orchestrators[
        (
            request.execution_id,
            _only_active_phase(workflow, request.workspace_id, request.execution_id),
        )
    ].sub_agent_id
    variation_agent = PersistenceSubAgentStore(
        stack.chat_service.persistence
    ).get_sub_agent(request.workspace_id, variation_node)
    assert variation_agent is not None
    variation_registry = callbacks["registry"](
        replace(
            context,
            conversation_id=variation_node,
            sub_agent_id=variation_node,
            allowed_tools=variation_agent.allowed_tools,
        )
    )
    declaration = variation_registry.dispatch(
        "read_strategy",
        {"program_id": "alpha"},
        context=replace(
            context,
            conversation_id=variation_node,
            sub_agent_id=variation_node,
            allowed_tools=variation_agent.allowed_tools,
        ),
    )
    assert declaration.ok
    assert declaration.structured["parameters"] == {
        "fast": {
            "type": "integer",
            "default": 5,
            "minimum": 1,
            "maximum": 20,
        }
    }
    variation_batch = variation_registry.dispatch(
        "run_backtest_batch",
        {
            "program_id": "alpha",
            "assignments": [{"fast": 4}, {"fast": 6}],
        },
        context=replace(
            context,
            conversation_id=variation_node,
            sub_agent_id=variation_node,
            allowed_tools=variation_agent.allowed_tools,
        ),
    )
    assert variation_batch.ok
    variation_points = cast(
        list[dict[str, object]], variation_batch.structured["points"]
    )
    assert [point["parameters"] for point in variation_points] == [
        {"fast": 4},
        {"fast": 6},
    ]
    assert all(isinstance(point["jobId"], str) for point in variation_points)
    for point in variation_points:
        job_id = cast(str, point["jobId"])
        assert variation_registry.dispatch(
            "get_job_status",
            {"job_id": job_id},
            context=replace(
                context,
                conversation_id=variation_node,
                sub_agent_id=variation_node,
                allowed_tools=variation_agent.allowed_tools,
            ),
        ).ok
        assert variation_registry.dispatch(
            "get_job_result",
            {"job_id": job_id},
            context=replace(
                context,
                conversation_id=variation_node,
                sub_agent_id=variation_node,
                allowed_tools=variation_agent.allowed_tools,
            ),
        ).ok
    variation = {
        "strategy": executed_reference,
        "batchId": variation_batch.structured["batchId"],
        "center": {
            "parameters": {"fast": 5},
            "jobId": observed_run,
            "state": "succeeded",
            "metrics": {"sharpe": 1.0, "fit.baseline": 0.5},
            "error": None,
        },
        "points": [
            {
                "parameters": point["parameters"],
                "jobId": point["jobId"],
                "state": "succeeded",
                "metrics": {"sharpe": 1.0, "fit.baseline": 0.5},
                "error": None,
            }
            for point in variation_points
        ],
        "method": "one_at_a_time",
        "interactions": [],
        "parameters": ["fast"],
        "stability": "Fast lookback is a plateau across the tested neighborhood.",
        "limitations": "One parameter at a time on one symbol and one window.",
    }
    workflow.apply_phase_completion(
        request,
        variation_node,
        {"target": "forward_validation", "variation": variation},
        attempt_id=_phase_attempt(workflow, request.workspace_id, variation_node),
    )
    work.pop(0)()
    forward_execution = stack.chat_service.get_execution(
        request.workspace_id, request.execution_id
    )
    assert forward_execution.phase == "forward_validation"
    forward_node = workflow.active_orchestrators[
        (
            request.execution_id,
            _only_active_phase(workflow, request.workspace_id, request.execution_id),
        )
    ].sub_agent_id
    forward_agent = PersistenceSubAgentStore(
        stack.chat_service.persistence
    ).get_sub_agent(request.workspace_id, forward_node)
    assert forward_agent is not None
    forward_registry = callbacks["registry"](
        replace(
            context,
            conversation_id=forward_node,
            sub_agent_id=forward_node,
            allowed_tools=forward_agent.allowed_tools,
        )
    )
    forward_start = len(harness.calls)
    center_run = forward_registry.dispatch(
        "run_backtest",
        {"program_id": "alpha", "parameters": {"fast": 5}},
        context=replace(
            context,
            conversation_id=forward_node,
            sub_agent_id=forward_node,
            allowed_tools=forward_agent.allowed_tools,
        ),
    )
    assert center_run.ok
    forward_observed_run = cast(str, center_run.structured["job_id"])
    forward_null_batch = forward_registry.dispatch(
        "run_null_batch",
        {
            "program_id": "alpha",
            "parameters": {"fast": 5},
            "mode": "block_permutation",
            "block_size": 5,
            "seeds": [1, 2],
        },
        context=replace(
            context,
            conversation_id=forward_node,
            sub_agent_id=forward_node,
            allowed_tools=forward_agent.allowed_tools,
        ),
    )
    assert forward_null_batch.ok
    forward_null_points = cast(
        list[dict[str, object]], forward_null_batch.structured["points"]
    )
    assert all(isinstance(point["jobId"], str) for point in forward_null_points)
    forward_variation_batch = forward_registry.dispatch(
        "run_backtest_batch",
        {
            "program_id": "alpha",
            "assignments": [{"fast": 4}, {"fast": 6}],
        },
        context=replace(
            context,
            conversation_id=forward_node,
            sub_agent_id=forward_node,
            allowed_tools=forward_agent.allowed_tools,
        ),
    )
    assert forward_variation_batch.ok
    forward_points = cast(
        list[dict[str, object]], forward_variation_batch.structured["points"]
    )
    assert [point["parameters"] for point in forward_points] == [
        {"fast": 4},
        {"fast": 6},
    ]
    for tool, arguments in (
        ("get_job_status", {"job_id": forward_observed_run}),
        ("get_job_result", {"job_id": forward_observed_run}),
    ):
        assert forward_registry.dispatch(
            tool,
            arguments,
            context=replace(
                context,
                conversation_id=forward_node,
                sub_agent_id=forward_node,
                allowed_tools=forward_agent.allowed_tools,
            ),
        ).ok
    for point in forward_null_points:
        job_id = cast(str, point["jobId"])
        assert forward_registry.dispatch(
            "get_job_status",
            {"job_id": job_id},
            context=replace(
                context,
                conversation_id=forward_node,
                sub_agent_id=forward_node,
                allowed_tools=forward_agent.allowed_tools,
            ),
        ).ok
        assert forward_registry.dispatch(
            "get_job_result",
            {"job_id": job_id},
            context=replace(
                context,
                conversation_id=forward_node,
                sub_agent_id=forward_node,
                allowed_tools=forward_agent.allowed_tools,
            ),
        ).ok
    for point in forward_points:
        job_id = cast(str, point["jobId"])
        assert forward_registry.dispatch(
            "get_job_status",
            {"job_id": job_id},
            context=replace(
                context,
                conversation_id=forward_node,
                sub_agent_id=forward_node,
                allowed_tools=forward_agent.allowed_tools,
            ),
        ).ok
        assert forward_registry.dispatch(
            "get_job_result",
            {"job_id": job_id},
            context=replace(
                context,
                conversation_id=forward_node,
                sub_agent_id=forward_node,
                allowed_tools=forward_agent.allowed_tools,
            ),
        ).ok

    mandate = resolve_research_mandate(
        workflow.execution_store,
        workspace_id=request.workspace_id,
        execution_id=request.execution_id,
    )
    assert mandate is not None
    forward_window = {
        "start": rfc3339(mandate.forward_window.start_ns),
        "end": rfc3339(mandate.forward_window.end_ns),
    }
    forward_metrics = {"sharpe": 1.0, "fit.baseline": 0.5}
    forward_evaluation = {
        "strategy": executed_reference,
        "window": forward_window,
        "statistic": "sharpe",
        "direction": "upper",
        "observed": {
            "jobId": forward_observed_run,
            "metrics": forward_metrics,
            "value": 1.0,
        },
        "null": {
            "signalAdequacy": {
                "signal": "lagged returns",
                "destroyedDependency": "inter_block_order",
                "preservedProperties": ["marginal_returns", "within_block_dependence"],
                "limitations": "Calendar volatility schedule is not preserved.",
            },
            "evidenceScope": "selection_conditional",
            "failedReplicatePolicy": "conservative_exceedance",
            "targetResolution": 0.5,
            "mode": "block_permutation",
            "batchId": forward_null_batch.structured["batchId"],
            "blockSize": 5,
            "replicates": [
                {
                    "seed": point["seed"],
                    "jobId": point["jobId"],
                    "state": "succeeded",
                    "metrics": forward_metrics,
                    "error": None,
                }
                for point in forward_null_points
            ],
            "exceedanceCount": 2,
            "pValue": 1.0,
        },
        "variation": {
            "batchId": forward_variation_batch.structured["batchId"],
            "center": {
                "parameters": {"fast": 5},
                "jobId": forward_observed_run,
                "state": "succeeded",
                "metrics": forward_metrics,
                "error": None,
            },
            "points": [
                {
                    "parameters": point["parameters"],
                    "jobId": point["jobId"],
                    "state": "succeeded",
                    "metrics": forward_metrics,
                    "error": None,
                }
                for point in forward_points
            ],
            "method": "one_at_a_time",
            "interactions": [],
            "parameters": ["fast"],
            "stability": (
                "Fast lookback holds on the forward window across the tested "
                "neighborhood."
            ),
        },
        "comparison": (
            "Development and forward both measured Sharpe 1.0 with no "
            "exceedance; the neighborhood stability held on the protected "
            "window."
        ),
        "limitations": "One protected window, two seeds, one parameter.",
    }
    workflow.apply_phase_completion(
        request,
        forward_node,
        {"target": "completed", "forwardValidation": forward_evaluation},
        attempt_id=_phase_attempt(workflow, request.workspace_id, forward_node),
    )
    execution = stack.chat_service.get_execution(
        request.workspace_id, request.execution_id
    )
    assert execution.phase == "completed"
    assert execution.state.value == "completed"
    transitions = workflow.execution_store.get(request.execution_id).transitions
    assert transitions[-2].completion["variation"]["stability"] == (
        "Fast lookback is a plateau across the tested neighborhood."
    )
    assert transitions[-2].completion["variation"]["parameters"] == ["fast"]
    stored = transitions[-1]
    assert stored.completion["forwardValidation"]["window"] == forward_window
    assert stored.completion["forwardValidation"]["statistic"] == "sharpe"
    outputs = [
        event
        for event in workflow.observe(request.workspace_id, request.execution_id).events
        if event.type.value == "sub_agent.output"
        and event.payload.get("phase") == "variation"
    ]
    assert outputs
    artifact = outputs[-1].payload.get("artifact")
    assert isinstance(artifact, Mapping)
    assert artifact["type"] == "variation"
    assert artifact["parameters"] == ["fast"]
    assert artifact["stability"] == (
        "Fast lookback is a plateau across the tested neighborhood."
    )
    artifact_center = artifact["center"]
    assert isinstance(artifact_center, Mapping)
    assert artifact_center["state"] == "succeeded"
    # The forward phase policy rewrote the protected window on every submitted
    # forward run: the center, both null replicates, and both variation points.
    submitted = [
        cast(dict[str, object], arguments["config"])
        for name, arguments in harness.calls[forward_start:]
        if name == "run_backtest"
    ]
    assert len(submitted) == 5
    for run_document in submitted:
        assert run_document["start"] == forward_window["start"]
        assert run_document["end"] == forward_window["end"]
    assert (
        WorkspacePrograms(
            repositories, git_factory=repositories.git_factory, sdk=SdkReferences()
        ).load(request.workspace_id, "strategy", "alpha")["version"]
        != reference["version"]
    )


@pytest.mark.parametrize("failed_activity", ["started", "finished"])
def test_completion_receipt_writer_failure_propagates(
    tmp_path, monkeypatch, failed_activity
):
    from thesis_runtime.tools.registry import SessionPersistenceError

    _, stack, _, request, record, registry, _, callbacks, _ = setup_workflow(
        tmp_path, monkeypatch
    )
    checked = registry.dispatch(
        "validate_study",
        {"program_id": "momentum"},
        context=invocation_context(request, record),
    )
    create = stack.chat_service.create_message

    def fail_write(*args, **kwargs):
        data = kwargs.get("data") or {}
        if data.get("toolActivity") == failed_activity:
            raise OSError("receipt writer unavailable")
        session = data.get("sessionMessage")
        result = (
            session.get("content", {}).get("result")
            if isinstance(session, dict) and isinstance(session.get("content"), dict)
            else None
        )
        if failed_activity == "finished" and isinstance(result, dict):
            if "toolCallId" in result:
                raise OSError("receipt writer unavailable")
        return create(*args, **kwargs)

    monkeypatch.setattr(stack.chat_service, "create_message", fail_write)
    with pytest.raises(SessionPersistenceError):
        callbacks["validate"](
            request.workspace_id,
            record,
            completion_from(checked.structured, callbacks["sketchReference"]),
        )


def test_trials_pin_indicator_dependencies_and_configuration(tmp_path, monkeypatch):
    from thesis.workspaces.programs import WorkspacePrograms
    from thesis.workspaces.sdk.templates import INDICATOR_TEMPLATE

    repositories, _, _, request, record, registry, harness, callbacks, work = (
        setup_workflow(tmp_path, monkeypatch)
    )
    programs = WorkspacePrograms(
        repositories,
        harness,
        git_factory=repositories.git_factory,
        sdk=SdkReferences(harness),
    )
    snapshot = programs.load(request.workspace_id, "study", "momentum")
    configuration = snapshot["configuration"]
    assert registry.dispatch(
        "author_indicator",
        {
            "program_id": "signal",
            "source": INDICATOR_TEMPLATE,
            "configuration": configuration,
        },
        context=invocation_context(request, record),
    ).ok
    updated = registry.dispatch(
        "update_study",
        {
            "program_id": "momentum",
            "expected_version": snapshot["version"],
            "indicator_ids": ["signal"],
        },
        context=invocation_context(request, record),
    )
    assert updated.ok
    checked = registry.dispatch(
        "validate_study",
        {"program_id": "momentum"},
        context=invocation_context(request, record),
    )
    assert checked.ok
    accepted = programs.load(
        request.workspace_id, "study", "momentum", str(checked.structured["gitCommit"])
    )
    completion = completion_from(checked.structured, callbacks["sketchReference"])
    assert callbacks["validate"](request.workspace_id, record, completion) is None
    callbacks["apply"](request.workspace_id, record, completion)
    indicator = registry.dispatch(
        "read_indicator",
        {"program_id": "signal"},
        context=invocation_context(request, record),
    )
    assert registry.dispatch(
        "update_indicator",
        {
            "program_id": "signal",
            "expected_version": indicator.structured["version"],
            "expected": "pub struct AuthoredIndicator",
            "replacement": "// changed dependency\npub struct AuthoredIndicator",
        },
        context=invocation_context(request, record),
    ).ok
    work.pop(0)()
    launch = next(arguments for name, arguments in harness.calls if name == "run_study")
    assert (
        launch["config"]["strategy_instances"][0]["parameters"]["source"]
        == accepted["source"]
    )
    assert "changed dependency" not in str(accepted["source"])


def invocation_context(request, record):
    return ToolContext(
        workspace_id=request.workspace_id,
        chat_id=request.chat_id,
        conversation_id=record.sub_agent_id,
        sub_agent_id=record.sub_agent_id,
        actor_id=record.created_by,
        workflow_execution_id=request.execution_id,
        phase_attempt_id=record.phase_attempt_id,
        phase_executor=record.phase_executor,
        allowed_tools=record.allowed_tools,
        intent_epoch=request.intent_epoch,
    )
