"""Coverage for the generic workflow engine under ``thesis.workflows.core``."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime

import pytest

from thesis.workflows.core.builder import WorkflowBuilder
from thesis.workflows.core.machine import TransitionMachine, TransitionTable
from thesis.workflows.core.phase import (
    DeterministicPhase,
    OneShotPhase,
    WorkflowSpecError,
)
from thesis.workflows.core.registry import (
    UnknownWorkflowError,
    WorkflowRegistration,
    WorkflowRegistry,
)
from thesis.workflows.core.spec import TransitionRequest, WorkflowContext
from thesis.workflows.core.workflow import WorkflowRequest


@dataclass(frozen=True, slots=True)
class _State:
    phase: str
    intent_epoch: int = 0
    transitions: tuple[object, ...] = ()


@dataclass(frozen=True, slots=True)
class _Step:
    sequence: int
    source: str
    target: str
    at: datetime
    intent_epoch: int
    reason: str


def _now() -> datetime:
    return datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


def _contract(workflow: object, request: object, state: _State, context: object):
    del workflow, request, context
    return {"target": "working"}


def _seed(workflow: object, request: object, state: object, context: object) -> str:
    del workflow, request, state, context
    return "do the phase"


def _builder() -> WorkflowBuilder[object, WorkflowRequest, _State]:
    builder = WorkflowBuilder[object, WorkflowRequest, _State]()
    builder.workflow("demo", version="1.0.0", title="Demo")
    builder.phase(DeterministicPhase(phase="requested", contract=_contract))
    builder.phase(OneShotPhase(phase="working", seed=_seed))
    builder.phase(OneShotPhase(phase="done", terminal=True))
    builder.transitions(
        {
            "requested": ("working",),
            "working": ("done",),
            "done": (),
        }
    )
    return builder


def test_builder_assembles_a_validated_spec() -> None:
    spec = _builder().build().spec

    assert spec.workflow_id == "demo"
    assert spec.phase_names == ("requested", "working", "done")
    assert spec.terminal_phases == ("done",)
    assert isinstance(spec.phase_specs["requested"], DeterministicPhase)
    assert spec.phase_specs["requested"].contract is _contract
    assert isinstance(spec.phase_specs["working"], OneShotPhase)
    assert spec.transitions["working"] == frozenset({"done"})
    assert spec.transitions["done"] == frozenset()


def test_a_deterministic_phase_is_unrepresentable_without_a_contract() -> None:
    with pytest.raises(TypeError, match="contract"):
        DeterministicPhase(phase="requested")  # ty: ignore[missing-argument]


def test_builder_rejects_an_undeclared_transition_phase() -> None:
    builder = _builder()
    builder.transition("working", "elsewhere")

    with pytest.raises(WorkflowSpecError, match="not a phase"):
        builder.build()


def test_builder_requires_a_terminal_phase() -> None:
    builder = WorkflowBuilder[object, WorkflowRequest, _State]()
    builder.workflow("demo", version="1.0.0", title="Demo")
    builder.phase(OneShotPhase(phase="requested", seed=_seed))

    with pytest.raises(WorkflowSpecError, match="terminal phase"):
        builder.build()


def test_builder_rejects_piping_outside_the_transition_table() -> None:
    builder = _builder()
    builder.piping("requested", "done", lambda state: state)

    with pytest.raises(WorkflowSpecError, match="not a declared transition"):
        builder.build()


def test_registry_resolves_a_registered_workflow_and_rejects_unknown() -> None:
    workflow = _builder().build()

    def execute(
        context: WorkflowContext,
        /,
        *,
        ideas: str,
        investigation_id: str | None = None,
    ) -> Mapping[str, object]:
        del context, investigation_id
        return {"executionId": ideas}

    registry = WorkflowRegistry()
    registration = WorkflowRegistration(
        execute=execute,
        workflow_factory=lambda: workflow,
    )
    registry.register(registration)

    assert registry.resolve("demo") is registration
    assert registry.resolve("demo").workflow is workflow
    assert registry.contains("demo")
    assert registry.ids() == ("demo",)
    with pytest.raises(UnknownWorkflowError, match="unknown workflow: 'nope'"):
        registry.resolve("nope")
    with pytest.raises(ValueError, match="already registered"):
        registry.register(registration)


def test_machine_stamps_the_request_epoch_and_sequence() -> None:
    table = TransitionTable(
        {"requested": frozenset({"working"}), "working": frozenset()},
        error=lambda source, target: ValueError(f"{source}->{target}"),
    )
    machine = TransitionMachine[str, _Step, _State](
        table,
        record_of=lambda sequence, source, target, at, epoch, reason: _Step(
            sequence, source, target, at, epoch, reason
        ),
        rebuild=lambda state, target, records: replace(
            state, phase=target, transitions=records
        ),
    )
    state = _State(phase="requested", intent_epoch=4)
    request = TransitionRequest(target_phase="working", intent_epoch=4)

    advanced = machine.apply(state, request, reason="go", at=_now())

    assert advanced.phase == "working"
    assert advanced.transitions == (_Step(1, "requested", "working", _now(), 4, "go"),)

    with pytest.raises(ValueError, match="working->requested"):
        machine.apply(
            advanced,
            TransitionRequest(target_phase="requested", intent_epoch=4),
            reason="back",
            at=_now(),
        )


class _Outcome:
    def __init__(self, phase: str) -> None:
        self.phase = phase


def test_turn_driver_removed_with_the_agent_first_inversion() -> None:
    """The engine must not turn agents: no turn driver exists to test."""
    with pytest.raises(ImportError):
        __import__("thesis.workflows.core.runner")
