"""Committed reference acceptance, immutable execution, and historical replay."""

from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from thesis.agent.runtime.store import PersistenceSubAgentStore
from thesis.research.models import ResearchModel, ResearchModelError
from thesis.workflows.registry.hypothesis_development.errors import (
    FalsificationComposeError,
)
from thesis.workflows.registry.hypothesis_development.phases.trials import (
    _draft_from_contract,
)
from thesis.workflows.registry.hypothesis_development.rebuild import rebuild_state
from thesis.workflows.registry.hypothesis_development.session_services import (
    HypothesisSessionServices,
)
from thesis.workflows.research_models import (
    StudyProgramReference,
    resolve_study_reference,
)

from ._support import (
    WORKSPACE,
    FakePrograms,
    _discovery_envelope,
    _hypothesis_envelope,
    _inline_model,
    _kit,
    _model,
    _node_record,
    _phase_node_id,
    _request,
)


def test_resolution_uses_accepted_commit_even_when_latest_changes() -> None:
    calls = []

    class Programs(FakePrograms):
        def load(self, workspace_id, kind, program_id, commit=None):
            calls.append(commit)
            snapshot = super().load(workspace_id, kind, program_id, commit)
            if commit is None:
                snapshot.update(version="e" * 64, source="changed latest")
            return snapshot

    model = resolve_study_reference(
        Programs(), WORKSPACE, StudyProgramReference.from_document(_model())
    )
    assert calls == ["a" * 40]
    assert model.source == _inline_model()["source"]
    assert model.version == "b" * 64
    with pytest.raises(ResearchModelError, match="another workspace"):
        resolve_study_reference(
            Programs(), "other", StudyProgramReference.from_document(_model())
        )


@pytest.mark.parametrize(
    "field",
    [
        "version",
        "gitCommit",
        "workspaceId",
        "sdkIdentity",
        "validationIdentity",
        "sourceSha256",
        "files",
        "accepted",
    ],
)
def test_completion_rejects_stale_or_foreign_validation(
    tmp_path: Path, field: str
) -> None:
    kit = _kit(tmp_path, payloads=[], results=[])
    request = _request(kit)
    node = kit.workflow.prepare_phase(request)
    assert node is not None
    kit.workflow.apply_phase_completion(
        request, node, {"target": "model_creation", "discovery": _discovery_envelope()}
    )
    record = _node_record(kit, _phase_node_id(kit, request, "model_creation"))

    class Programs(FakePrograms):
        def load(self, workspace_id, kind, program_id, commit=None):
            snapshot = super().load(workspace_id, kind, program_id, commit)
            snapshot["validation"][field] = False if field == "accepted" else "wrong"
            return snapshot

    service = HypothesisSessionServices(
        chat=kit.chat,
        store=PersistenceSubAgentStore(kit.chat.persistence),
        workflow=lambda: kit.workflow,
        harness_tools=None,
        programs=Programs(),
        activity=lambda context, tool, arguments, execute: execute(),
    )
    assert service.validate_completion(
        WORKSPACE, record, {"hypothesis": _hypothesis_envelope(), "intentEpoch": 0}
    )


def test_strategy_uses_same_committed_receipt_verifier(tmp_path: Path) -> None:
    kit = _kit(tmp_path, payloads=[], results=[])
    request = _request(kit)
    node = kit.workflow.prepare_phase(request)
    assert node is not None
    record = replace(_node_record(kit, node), phase="strategy_design")
    store = PersistenceSubAgentStore(kit.chat.persistence)
    store.save_sub_agent(record)
    service = HypothesisSessionServices(
        chat=kit.chat,
        store=store,
        harness_tools=None,
        programs=FakePrograms(),
        activity=lambda context, tool, arguments, execute: execute(),
    )
    reference = {
        key: value
        for key, value in _model().items()
        if key in {"workspaceId", "kind", "programId", "gitCommit", "version"}
    }
    reference["kind"] = "strategy"
    assert (
        service.validate_completion(
            WORKSPACE, record, {"strategy": reference, "intentEpoch": 0}
        )
        is None
    )
    reference["workspaceId"] = "foreign"
    assert "workspace" in (
        service.validate_completion(
            WORKSPACE, record, {"strategy": reference, "intentEpoch": 0}
        )
        or ""
    )


@pytest.mark.parametrize("kind", ["study", "strategy"])
@pytest.mark.parametrize("current_identity", [None, "", " ", "compiler-new"])
def test_compiler_only_change_requires_revalidation(
    tmp_path: Path,
    kind: str,
    current_identity: str | None,
) -> None:
    kit = _kit(tmp_path, payloads=[], results=[])
    request = _request(kit)
    node = kit.workflow.prepare_phase(request)
    assert node is not None
    record = replace(
        _node_record(kit, node),
        phase="model_creation" if kind == "study" else "strategy_design",
    )
    store = PersistenceSubAgentStore(kit.chat.persistence)
    store.save_sub_agent(record)

    class Programs(FakePrograms):
        current: str | None = "compiler-current"
        validated = "compiler-current"

        def load(self, workspace_id, kind, program_id, commit=None):
            snapshot = super().load(workspace_id, kind, program_id, commit)
            snapshot["validationIdentity"] = self.current
            snapshot["validation"]["validationIdentity"] = self.validated
            return snapshot

    programs = Programs()
    service = HypothesisSessionServices(
        chat=kit.chat,
        store=store,
        harness_tools=None,
        programs=programs,
        activity=lambda context, tool, arguments, execute: execute(),
    )
    completion = (
        {"hypothesis": _hypothesis_envelope(), "intentEpoch": 0}
        if kind == "study"
        else {
            "strategy": {
                key: value
                for key, value in _model().items()
                if key in {"workspaceId", "kind", "programId", "gitCommit", "version"}
            },
            "intentEpoch": 0,
        }
    )
    if kind == "strategy":
        strategy = completion["strategy"]
        assert isinstance(strategy, dict)
        strategy["kind"] = kind
    assert service.validate_completion(WORKSPACE, record, completion) is None
    programs.current = current_identity
    assert "compiler identity" in (
        service.validate_completion(WORKSPACE, record, completion) or ""
    )
    programs.current = programs.validated = "compiler-new"
    assert service.validate_completion(WORKSPACE, record, completion) is None


def test_reference_rebuild_is_io_free_and_historical_inline_is_replay_only(
    tmp_path: Path,
) -> None:
    kit = _kit(tmp_path, payloads=[], results=[])
    request = _request(kit)
    node = kit.workflow.prepare_phase(request)
    assert node is not None
    kit.workflow.apply_phase_completion(
        request, node, {"target": "model_creation", "discovery": _discovery_envelope()}
    )
    state = kit.workflow._reconstruct_state(request)
    kit.workflow._apply_contract(
        request,
        state,
        "model_creation",
        {"target": "falsification_trials", "hypothesis": _hypothesis_envelope()},
    )
    transitions = kit.execution_store.get(request.execution_id).transitions
    reference_state = rebuild_state(WORKSPACE, request.execution_id, transitions)
    assert reference_state.draft is not None
    assert isinstance(reference_state.draft.model, StudyProgramReference)
    historical = {**_hypothesis_envelope(), "model": _inline_model()}
    old_transition = replace(
        transitions[-1],
        from_phase="hypothesis_creation",
        completion={"hypothesis": historical},
    )
    historical_state = rebuild_state(
        WORKSPACE, request.execution_id, [replace(old_transition, sequence=1)]
    )
    assert historical_state.draft is not None
    assert isinstance(historical_state.draft.model, ResearchModel)
    assert historical_state.draft.model.source == str(_inline_model()["source"]).strip()
    with pytest.raises(FalsificationComposeError):
        _draft_from_contract(historical)


def test_shared_sdk_guidance_is_in_every_authoring_prompt() -> None:
    from thesis.workflows.registry.hypothesis_development.prompts.discovery import (
        _discovery_turn_prompt,
    )
    from thesis.workflows.registry.hypothesis_development.prompts.model_creation import (
        _model_creation_instructions,
    )
    from thesis.workflows.registry.hypothesis_development.prompts.sdk import (
        SDK_AUTHORING_GUIDANCE,
    )
    from thesis.workflows.registry.hypothesis_development.prompts.strategy_design import (
        _strategy_design_instructions,
    )

    for prompt in (
        _discovery_turn_prompt([]),
        _model_creation_instructions(),
        _strategy_design_instructions(),
    ):
        assert SDK_AUTHORING_GUIDANCE in prompt
        assert 'validate_study {"model"' not in prompt
        assert "template='basic'" in prompt
        assert "publish_study/publish_strategy/publish_indicator" in prompt
        assert "get_job_status while queued/running" in prompt
        assert "never to discovery or model creation" in prompt


def test_every_program_tool_named_in_an_authoring_seed_is_allowed_in_that_phase() -> (
    None
):
    import re

    from thesis.workflows.registry.hypothesis_development.prompts.discovery import (
        _discovery_turn_prompt,
    )
    from thesis.workflows.registry.hypothesis_development.prompts.model_creation import (
        _model_creation_instructions,
    )
    from thesis.workflows.registry.hypothesis_development.prompts.strategy_design import (
        _strategy_design_instructions,
    )
    from thesis.workflows.registry.hypothesis_development.spec import phase_declarations

    pattern = re.compile(
        r"\b(?:author|update|read|validate|publish|list)_"
        r"(?:study|strategy|indicator|studies|strategies|indicators)\b"
    )
    declarations = {item.spec.phase: item for item in phase_declarations()}
    prompts = {
        "discovery": _discovery_turn_prompt([]),
        "model_creation": _model_creation_instructions(),
        "strategy_design": _strategy_design_instructions(),
    }
    for phase, prompt in prompts.items():
        named = set(pattern.findall(prompt))
        assert named, phase
        allowed = set(declarations[phase].tools)
        assert named <= allowed, (phase, sorted(named - allowed))


def test_strategy_backtesting_remains_agent_backed_with_id_tools() -> None:
    from thesis.workflows.core.phase import OneShotPhase
    from thesis.workflows.registry.hypothesis_development.spec import phase_declarations

    declaration = next(
        item
        for item in phase_declarations()
        if item.spec.phase == "strategy_backtesting"
    )
    assert isinstance(declaration.spec, OneShotPhase)
    assert "read_strategy" in declaration.tools
    assert "run_backtest" in declaration.tools
    assert declaration.orchestrator is not None


def test_trials_executor_does_not_decode_unaccepted_inline_contract(
    tmp_path: Path,
) -> None:
    from thesis.workflows.core.phase import HandoffContext
    from thesis.workflows.registry.hypothesis_development.phases.trials import (
        run_falsification_trials,
    )
    from thesis.workflows.registry.hypothesis_development.services import (
        DevelopmentEngine,
    )

    kit = _kit(tmp_path, payloads=[], results=[])
    request = _request(kit)
    state = kit.workflow._reconstruct_state(request)
    inline = {**_hypothesis_envelope(), "model": _inline_model()}
    with pytest.raises(FalsificationComposeError):
        run_falsification_trials(
            cast(DevelopmentEngine, kit.workflow),
            request,
            state,
            HandoffContext(
                source="model_creation",
                target="falsification_trials",
                completion={"hypothesis": inline},
            ),
        )
    assert kit.runner.requests == []
