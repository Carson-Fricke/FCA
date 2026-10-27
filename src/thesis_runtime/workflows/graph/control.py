"""Pure activation replay over selected routes and named input deliveries.

Accepted completions alone establish control state. Fan-out occurrences retain
causal branch coverage through partial convergence, including branch-local
loops and nested fan-out. No activity, storage or conversation is executed here.
"""

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Protocol

from thesis_runtime.workflows.graph.contracts import (
    Destination,
    Node,
    WorkflowSpecError,
)


class AcceptedCompletion(Protocol):
    """An admitted record with a detached, validated JSON output object."""

    @property
    def sequence(self) -> int: ...
    @property
    def from_phase(self) -> str: ...
    @property
    def route(self) -> str: ...
    @property
    def attempt_id(self) -> str: ...
    @property
    def completion(self) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class Position[RecordT: AcceptedCompletion]:
    phase: str
    attempt_id: str
    inputs: Mapping[str, Mapping[str, object]] = field(default_factory=dict)
    history: tuple[RecordT, ...] = ()
    scope: tuple[tuple[str, frozenset[str]], ...] = ()
    source: str | None = None
    dependencies: tuple[int, ...] = ()


@dataclass(slots=True)
class Scope[RecordT: AcceptedCompletion]:
    """One derived fan-out occurrence; it has no paired consumer."""

    parent: Position[RecordT]
    branches: frozenset[str]
    finished: set[str] = field(default_factory=set)


@dataclass(slots=True)
class Pending[RecordT: AcceptedCompletion]:
    required: tuple[str, ...]
    arrivals: dict[str, tuple[Position[RecordT], Mapping[str, object]]] = field(
        default_factory=dict
    )


@dataclass(slots=True)
class Graph[RecordT: AcceptedCompletion]:
    positions: dict[str, Position[RecordT]] = field(default_factory=dict)
    scopes: dict[str, Scope[RecordT]] = field(default_factory=dict)
    pending: dict[str, Pending[RecordT]] = field(default_factory=dict)
    deliveries: dict[tuple[str, str], list[tuple[tuple[str, frozenset[str]], ...]]] = (
        field(default_factory=dict)
    )
    sequence: int = 0


def attempt_identity(execution_id: str, phase: str, sequence: int) -> str:
    return (
        "attempt-"
        + sha256(f"{execution_id}|{phase}|{sequence}".encode()).hexdigest()[:32]
    )


def replay_graph[RecordT: AcceptedCompletion](
    execution_id: str,
    entry: str,
    nodes: Mapping[str, Node],
    history: Sequence[RecordT],
    initial: Mapping[str, object] | None = None,
) -> Graph[RecordT]:
    graph = Graph[RecordT]()
    root_inputs = nodes[entry].inputs
    if len(root_inputs) != 1:
        raise WorkflowSpecError("the entry node requires one workflow input port")
    graph.positions[entry] = Position[RecordT](
        entry,
        attempt_identity(execution_id, entry, 0),
        {root_inputs[0]: deepcopy(dict(initial or {}))},
    )

    def close_scopes() -> None:
        for key in reversed(tuple(graph.scopes)):
            scope = graph.scopes[key]
            children = any(
                key in dict(other.parent.scope)
                for other_key, other in graph.scopes.items()
                if other_key != key
            )
            if scope.finished == set(scope.branches) and not children:
                del graph.scopes[key]

    def merge_scope(
        sources: tuple[Position[RecordT], ...],
    ) -> tuple[tuple[str, frozenset[str]], ...]:
        shared = set.intersection(*(set(dict(source.scope)) for source in sources))
        if not shared:
            raise WorkflowSpecError("inputs belong to different activations")
        # Separate visits to the same nested fan-out must not exchange values.
        for left in sources:
            for right in sources:
                for left_key in set(dict(left.scope)) - set(dict(right.scope)):
                    for right_key in set(dict(right.scope)) - set(dict(left.scope)):
                        if (
                            graph.scopes[left_key].parent.scope
                            == graph.scopes[right_key].parent.scope
                        ):
                            raise WorkflowSpecError(
                                "inputs belong to different fan-out occurrences"
                            )
        coverage: dict[str, set[str]] = {}
        for source in sources:
            for key, branches in source.scope:
                coverage.setdefault(key, set()).update(branches)
        # Fully reunited occurrences disappear, except while a nested occurrence
        # is still open. Partial joins carry their branch coverage onward.
        for key in reversed(tuple(graph.scopes)):
            if key not in coverage or coverage[key] != set(graph.scopes[key].branches):
                continue
            children = any(
                key in dict(other.parent.scope)
                for other_key, other in graph.scopes.items()
                if other_key != key
            )
            if not children:
                del graph.scopes[key]
                del coverage[key]
        return tuple(
            (key, frozenset(coverage[key])) for key in graph.scopes if key in coverage
        )

    def deliver(
        target: Destination,
        source: Position[RecordT],
        output: Mapping[str, object],
        sequence: int,
    ) -> None:
        node = nodes[target.node]
        if target.port not in node.inputs:
            raise WorkflowSpecError(f"undeclared input {target}")
        if target.node in graph.positions:
            raise WorkflowSpecError(f"two live activations at node {target.node!r}")
        for previous in graph.deliveries.get((target.node, target.port), ()):
            for key, branches in source.scope:
                earlier = dict(previous).get(key)
                if earlier is not None and not earlier.intersection(branches):
                    raise WorkflowSpecError(f"conflicting delivery to {target}")
        graph.deliveries.setdefault((target.node, target.port), []).append(source.scope)
        if len(node.inputs) > 1:
            pending = graph.pending.setdefault(
                target.node, Pending[RecordT](node.inputs)
            )
            if target.port in pending.arrivals:
                raise WorkflowSpecError(f"conflicting delivery to {target}")
            pending.arrivals[target.port] = (source, deepcopy(dict(output)))
            if set(pending.arrivals) != set(node.inputs):
                return
            sources = tuple(pending.arrivals[port][0] for port in node.inputs)
            scope = merge_scope(sources)
            common = set.intersection(
                *(set(item.sequence for item in origin.history) for origin in sources)
            )
            history = tuple(
                item for item in sources[0].history if item.sequence in common
            )
            dependencies = {
                item.sequence for origin in sources for item in origin.history
            }
            dependencies.update(
                item for origin in sources for item in origin.dependencies
            )
            position = Position[RecordT](
                target.node,
                attempt_identity(execution_id, target.node, sequence),
                {
                    port: deepcopy(dict(pending.arrivals[port][1]))
                    for port in node.inputs
                },
                history,
                scope,
                source.phase,
                tuple(sorted(dependencies)),
            )
            del graph.pending[target.node]
        else:
            position = Position[RecordT](
                target.node,
                attempt_identity(execution_id, target.node, sequence),
                {target.port: deepcopy(dict(output))},
                source.history,
                source.scope,
                source.phase,
                source.dependencies,
            )
        if node.outcome == "failed":
            graph.positions.clear()
            graph.scopes.clear()
            graph.pending.clear()
        elif node.outcome is not None:
            for key, branches in position.scope:
                graph.scopes[key].finished.update(branches)
            close_scopes()
        graph.positions[target.node] = position

    for accepted in history:
        if accepted.sequence != graph.sequence + 1:
            raise WorkflowSpecError("accepted history is not contiguous")
        source = graph.positions.pop(accepted.from_phase, None)
        if source is None or source.attempt_id != accepted.attempt_id:
            raise WorkflowSpecError(
                "completion has a stale or missing source activation"
            )
        if accepted.route not in nodes[source.phase].routes:
            raise WorkflowSpecError("completion selects an undeclared route")
        graph.sequence = accepted.sequence
        advanced = Position[RecordT](
            source.phase,
            source.attempt_id,
            source.inputs,
            (*source.history, accepted),
            source.scope,
            source.source,
            source.dependencies,
        )
        destinations = nodes[source.phase].routes[accepted.route]
        if len(destinations) > 1:
            key = f"{source.attempt_id}:{accepted.sequence}"
            branches = tuple(str(target) for target in destinations)
            graph.scopes[key] = Scope[RecordT](advanced, frozenset(branches))
            for branch, target in zip(branches, destinations, strict=True):
                scoped = Position[RecordT](
                    advanced.phase,
                    advanced.attempt_id,
                    advanced.inputs,
                    advanced.history,
                    (*advanced.scope, (key, frozenset({branch}))),
                    advanced.source,
                    advanced.dependencies,
                )
                deliver(target, scoped, accepted.completion, accepted.sequence)
                if nodes[target.node].outcome == "failed":
                    break
        else:
            deliver(destinations[0], advanced, accepted.completion, accepted.sequence)
    return graph
