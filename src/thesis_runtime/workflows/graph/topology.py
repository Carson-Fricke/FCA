"""Validate structured fixed graphs, including independent nested loops."""

from collections.abc import Mapping
from typing import Any

from thesis_runtime.workflows.phase import (
    DeterministicPhase,
    PhaseSpec,
    SplitPhase,
    TerminalPhase,
    WorkflowSpecError,
)


def validate_topology(
    phases: Mapping[str, PhaseSpec[Any, Any, Any]],
    edges: Mapping[str, frozenset[str]],
    entry: str,
) -> None:
    """Reject scope crossings and missing joins before any execution starts."""
    if (
        entry not in phases
        or set(edges) != set(phases)
        or any(target not in phases for targets in edges.values() for target in targets)
    ):
        raise WorkflowSpecError("every graph node and edge must be declared")
    for name, phase in phases.items():
        if bool(edges[name]) == isinstance(phase, TerminalPhase):
            raise WorkflowSpecError("only terminal nodes may have no successors")
    paired: dict[str, str] = {}
    for name, phase in phases.items():
        if isinstance(phase, SplitPhase):
            join = phases.get(phase.join)
            if (
                not isinstance(join, DeterministicPhase)
                or len(edges.get(phase.join, ())) != 1
            ):
                raise WorkflowSpecError(
                    f"split {name!r} requires a deterministic join with one successor"
                )
            if phase.join in paired:
                raise WorkflowSpecError("a join cannot belong to two splits")
            paired[phase.join] = name
            if edges.get(name) != frozenset(phase.branches.values()):
                raise WorkflowSpecError("split edges must equal its declared branches")
    owners: dict[str, tuple[tuple[str, str], ...]] = {}
    visited: set[tuple[str, tuple[tuple[str, str], ...]]] = set()

    def walk(node: str, stop: str | None, scope: tuple[tuple[str, str], ...]) -> None:
        if node == stop:
            return
        phase = phases[node]
        if isinstance(phase, TerminalPhase) and phase.outcome == "failed":
            return
        if node in paired:
            raise WorkflowSpecError(f"branch bypasses or crosses join {node!r}")
        if node in owners and owners[node] != scope:
            raise WorkflowSpecError(f"node {node!r} is shared across branch scopes")
        owners[node] = scope
        key = (node, scope)
        if key in visited:
            return
        visited.add(key)
        if isinstance(phase, TerminalPhase):
            if scope:
                raise WorkflowSpecError(
                    "a branch cannot bypass its join with a successful terminal"
                )
            return
        if isinstance(phase, SplitPhase):
            for branch, target in phase.branches.items():
                walk(target, phase.join, (*scope, (node, branch)))
            if phase.join in owners and owners[phase.join] != scope:
                raise WorkflowSpecError("join is shared across branch scopes")
            owners[phase.join] = scope
            for target in edges[phase.join]:
                walk(target, stop, scope)
        else:
            for target in edges[node]:
                walk(target, stop, scope)

    def reaches_join(current: str, stop: str, seen: set[str]) -> bool:
        if current == stop:
            return True
        if current in seen:
            return False
        seen.add(current)
        return any(reaches_join(target, stop, seen) for target in edges[current])

    walk(entry, None, ())
    for node, owner in owners.items():
        if not owner:
            continue
        split = phases[owner[-1][0]]
        assert isinstance(split, SplitPhase)
        if not reaches_join(node, split.join, set()):
            raise WorkflowSpecError(f"branch node {node!r} has no path to its join")
    missing = (
        set(phases)
        - set(owners)
        - {name for name, phase in phases.items() if isinstance(phase, TerminalPhase)}
    )
    if missing:
        raise WorkflowSpecError(
            f"unreachable graph nodes: {', '.join(sorted(missing))}"
        )
