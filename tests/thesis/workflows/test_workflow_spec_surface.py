"""The builder and spec contract for agent-first workflows."""

from __future__ import annotations

from typing import Any

import pytest

from thesis.workflows.core.builder import WorkflowBuilder
from thesis.workflows.core.phase import WorkflowSpecError
from thesis.workflows.core.spec import HandoffContext


def _seed(workflow: object, request: object, state: object, context: object) -> str:
    del workflow, request, state, context
    return "do the phase"


def _builder() -> WorkflowBuilder[Any, Any, Any]:
    builder = WorkflowBuilder()
    builder.workflow("demo", version="1.0.0", title="Demo")
    builder.phase("start", seed=_seed)
    builder.phase("done", seed=_seed, terminal=True)
    builder.transition("start", "done")
    return builder


def test_builder_accepts_a_seed_instead_of_an_executor() -> None:
    spec = _builder().build().spec
    assert spec.phase_names == ("start", "done")
    assert "start" in spec.seeds
    assert spec.terminal_phases == ("done",)


def test_builder_rejects_a_phase_with_neither_seed_nor_executor() -> None:
    builder = WorkflowBuilder()
    builder.workflow("demo", version="1.0.0", title="Demo")
    builder.phase("start")
    builder.phase("done", seed=_seed, terminal=True)
    builder.transition("start", "done")
    with pytest.raises(WorkflowSpecError, match="no executor or seed"):
        builder.build()


def test_builder_rejects_on_complete_that_is_not_a_declared_transition() -> None:
    builder = WorkflowBuilder()
    builder.workflow("demo", version="1.0.0", title="Demo")
    builder.phase("start", seed=_seed, on_complete="elsewhere")
    builder.phase("done", seed=_seed, terminal=True)
    builder.transition("start", "done")
    with pytest.raises(WorkflowSpecError, match="on_complete"):
        builder.build()


def test_declared_contract_surface_is_exposed_by_the_spec() -> None:
    builder = WorkflowBuilder()
    builder.workflow("demo", version="1.0.0", title="Demo")
    builder.phase(
        "start",
        seed=_seed,
        on_complete="done",
        required=("answer",),
    )
    builder.phase("done", seed=_seed, terminal=True)
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
    workflow: object, request: object, state: object
) -> dict[str, object]:
    del workflow, request, state
    return {"target": "done", "answer": 42}


def test_deterministic_phase_declares_its_executor_and_kind() -> None:
    from thesis.workflows.core.phase import PhaseKind

    builder = WorkflowBuilder()
    builder.workflow("demo", version="1.0.0", title="Demo")
    builder.phase(
        "intake",
        _deterministic_executor,
        kind=PhaseKind.DETERMINISTIC,
    )
    builder.phase("done", seed=_seed, terminal=True)
    builder.transition("intake", "done")
    spec = builder.build().spec

    assert spec.phase_specs["intake"].kind is PhaseKind.DETERMINISTIC
    assert spec.executors["intake"] is _deterministic_executor
    assert spec.phase_kinds["intake"] is PhaseKind.DETERMINISTIC


def test_builder_rejects_a_deterministic_phase_without_an_executor() -> None:
    from thesis.workflows.core.phase import PhaseKind

    builder = WorkflowBuilder()
    builder.workflow("demo", version="1.0.0", title="Demo")
    builder.phase(
        "intake",
        seed=_seed,
        kind=PhaseKind.DETERMINISTIC,
    )
    builder.phase("done", seed=_seed, terminal=True)
    builder.transition("intake", "done")
    with pytest.raises(WorkflowSpecError, match="requires an executor"):
        builder.build()
