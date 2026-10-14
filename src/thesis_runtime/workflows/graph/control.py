"""One pure control projection for serial paths, splits, loops and joins."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any

from thesis_runtime.workflows.execution_store import StoreTransition
from thesis_runtime.workflows.phase import (
    PhaseSpec,
    SplitPhase,
    TerminalPhase,
    WorkflowSpecError,
)


@dataclass(frozen=True, slots=True)
class Position:
    """One sequential branch's current attempt and frozen causal inputs."""

    phase: str
    attempt_id: str
    history: tuple[StoreTransition, ...] = ()
    scope: tuple[tuple[str, str], ...] = ()
    source: str | None = None
    completion: Mapping[str, object] = field(default_factory=dict)
    dependencies: tuple[int, ...] = ()


@dataclass(slots=True)
class Region:
    """One split occurrence, retaining accepted branch arrivals until its join."""

    parent: Position
    split: SplitPhase[Any, Any, Any]
    arrivals: dict[str, StoreTransition] = field(default_factory=dict)
    evidence: set[int] = field(default_factory=set)


@dataclass(slots=True)
class Graph:
    """Transient control state reconstructed exclusively from accepted history."""

    positions: dict[str, Position] = field(default_factory=dict)
    regions: dict[str, Region] = field(default_factory=dict)
    sequence: int = 0


def attempt_identity(execution_id: str, phase: str, sequence: int) -> str:
    """Bounded stable identity for a node activation, including nested splits."""
    return (
        "attempt-"
        + sha256(f"{execution_id}|{phase}|{sequence}".encode()).hexdigest()[:32]
    )


def replay_graph(
    execution_id: str,
    entry: str,
    phases: Mapping[str, PhaseSpec[Any, Any, Any]],
    edges: Mapping[str, frozenset[str]],
    history: Sequence[StoreTransition],
) -> Graph:
    """Project only accepted history; never use presentation events as authority."""
    graph = Graph()

    def enter(position: Position, sequence: int) -> None:
        spec = phases[position.phase]
        if isinstance(spec, SplitPhase):
            if position.attempt_id in graph.regions:
                raise WorkflowSpecError("split occurrence is already open")
            graph.regions[position.attempt_id] = Region(position, spec)
            for branch, target in spec.branches.items():
                enter(
                    Position(
                        target,
                        attempt_identity(execution_id, target, sequence),
                        position.history,
                        (*position.scope, (position.attempt_id, branch)),
                        position.source,
                        position.completion,
                        position.dependencies,
                    ),
                    sequence,
                )
        else:
            if position.phase in graph.positions:
                raise WorkflowSpecError("two live attempts at the same node")
            graph.positions[position.phase] = position

    enter(Position(entry, attempt_identity(execution_id, entry, 0)), 0)
    for transition in history:
        if transition.sequence != graph.sequence + 1:
            raise WorkflowSpecError("accepted graph history is not contiguous")
        graph.sequence = transition.sequence
        source = graph.positions.pop(transition.from_phase, None)
        if source is None or source.attempt_id != transition.attempt_id:
            raise WorkflowSpecError(
                "accepted transition has a stale or missing source attempt"
            )
        if transition.to_phase not in edges[source.phase]:
            raise WorkflowSpecError("accepted transition has an undeclared target")
        scoped = (*source.history, transition)
        target = transition.to_phase
        attempt = attempt_identity(execution_id, target, transition.sequence)
        if source.scope:
            region_id, branch = source.scope[-1]
            region = graph.regions[region_id]
            if target == region.split.join:
                if branch in region.arrivals:
                    raise WorkflowSpecError("duplicate join arrival")
                region.arrivals[branch] = transition
                region.evidence.update(item.sequence for item in scoped)
                region.evidence.update(source.dependencies)
                if len(region.arrivals) == len(region.split.branches):
                    graph.regions.pop(region_id)
                    enter(
                        Position(
                            target,
                            attempt,
                            region.parent.history,
                            region.parent.scope,
                            region.split.phase,
                            {
                                "splitAttemptId": region_id,
                                "subject": dict(region.parent.completion),
                                "branches": {
                                    name: dict(region.arrivals[name].completion)
                                    for name in region.split.branches
                                },
                                "branchAttempts": {
                                    name: region.arrivals[name].attempt_id
                                    for name in region.split.branches
                                },
                                "branchHandoffs": {
                                    name: region.arrivals[name].sequence
                                    for name in region.split.branches
                                },
                            },
                            tuple(sorted(region.evidence)),
                        ),
                        transition.sequence,
                    )
                continue
        terminal = phases[target]
        if isinstance(terminal, TerminalPhase) and terminal.outcome == "failed":
            graph.positions.clear()
            graph.regions.clear()
        enter(
            Position(
                target,
                attempt,
                scoped,
                source.scope,
                source.phase,
                transition.completion,
                source.dependencies,
            ),
            transition.sequence,
        )
    return graph
