"""The builder and spec contract for agent-first workflows."""

from __future__ import annotations

from typing import Any

import pytest

from thesis.workflows.core.builder import WorkflowBuilder
from thesis.workflows.core.phase import (
    ConversationPhase,
    DeterministicPhase,
    OneShotPhase,
    WorkflowSpecError,
)
from thesis.workflows.core.spec import HandoffContext


def _seed(workflow: object, request: object, state: object, context: object) -> str:
    del workflow, request, state, context
    return "do the phase"


def _builder() -> WorkflowBuilder[Any, Any, Any]:
    builder = WorkflowBuilder()
    builder.workflow("demo", version="1.0.0", title="Demo")
    builder.phase(OneShotPhase(phase="start", seed=_seed))
    builder.phase(OneShotPhase(phase="done", seed=_seed, terminal=True))
    builder.transition("start", "done")
    return builder


def test_builder_accepts_a_seed_on_an_agent_phase() -> None:
    spec = _builder().build().spec
    assert spec.phase_names == ("start", "done")
    start = spec.phase_specs["start"]
    assert isinstance(start, OneShotPhase)
    assert start.seed is _seed
    assert spec.terminal_phases == ("done",)


def test_a_deterministic_phase_is_unrepresentable_without_a_contract() -> None:
    with pytest.raises(TypeError, match="contract"):
        DeterministicPhase(phase="intake")  # ty: ignore[missing-argument]


def test_builder_rejects_on_complete_that_is_not_a_declared_transition() -> None:
    builder = WorkflowBuilder()
    builder.workflow("demo", version="1.0.0", title="Demo")
    builder.phase(OneShotPhase(phase="start", seed=_seed, on_complete="elsewhere"))
    builder.phase(OneShotPhase(phase="done", seed=_seed, terminal=True))
    builder.transition("start", "done")
    with pytest.raises(WorkflowSpecError, match="on_complete"):
        builder.build()


def test_declared_contract_surface_is_exposed_by_the_spec() -> None:
    builder = WorkflowBuilder()
    builder.workflow("demo", version="1.0.0", title="Demo")
    builder.phase(
        ConversationPhase(
            phase="start",
            seed=_seed,
            on_complete="done",
            required=("answer",),
        )
    )
    builder.phase(OneShotPhase(phase="done", seed=_seed, terminal=True))
    builder.transition("start", "done")
    spec = builder.build().spec
    assert spec.on_complete == {"start": "done"}
    assert spec.required == {"start": ("answer",)}


def test_handoff_context_carries_the_accepted_contract() -> None:
    context = HandoffContext(
        source="start",
        target="done",
        completion={"answer": 42},
        trigger_message_id="message-1",
        sub_agent_id="subagent-1",
    )
    assert context.source == "start"
    assert context.target == "done"
    assert context.completion["answer"] == 42
    assert context.trigger_message_id == "message-1"
    assert context.sub_agent_id == "subagent-1"


def _deterministic_executor(
    workflow: object, request: object, state: object, context: object
) -> dict[str, object]:
    del workflow, request, state, context
    return {"target": "done", "answer": 42}


def test_deterministic_phase_declares_its_contract() -> None:
    builder = WorkflowBuilder()
    builder.workflow("demo", version="1.0.0", title="Demo")
    builder.phase(DeterministicPhase(phase="intake", contract=_deterministic_executor))
    builder.phase(OneShotPhase(phase="done", seed=_seed, terminal=True))
    builder.transition("intake", "done")
    spec = builder.build().spec

    intake = spec.phase_specs["intake"]
    assert isinstance(intake, DeterministicPhase)
    assert intake.contract is _deterministic_executor
