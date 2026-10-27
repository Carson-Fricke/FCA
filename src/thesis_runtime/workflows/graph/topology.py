"""Validate named-port wiring and derive convergence from data dependencies.

This module knows no executors or conversations. Routes are the only edge
declaration; alternatives may feed one port, concurrency feeds distinct ports.
"""

from collections import deque
from collections.abc import Mapping

from thesis_runtime.workflows.graph.contracts import (
    Node,
    WorkflowSpecError,
)


def distances(nodes: Mapping[str, Node], entry: str) -> dict[str, int]:
    result: dict[str, int] = {}
    pending = deque([(entry, 0)])
    while pending:
        current, distance = pending.popleft()
        if current in result:
            continue
        result[current] = distance
        for destinations in nodes[current].routes.values():
            pending.extend((target.node, distance + 1) for target in destinations)
    return result


def validate_topology(nodes: Mapping[str, Node], entry: str) -> None:
    """Reject invalid endpoints, missing required ports and unreachable nodes."""
    if entry not in nodes:
        raise WorkflowSpecError("entry must name a declared node")
    if len(nodes[entry].inputs) != 1:
        raise WorkflowSpecError("entry requires one workflow input port")
    supplied: dict[str, set[str]] = {name: set() for name in nodes}
    for name, node in nodes.items():
        if name != node.name:
            raise WorkflowSpecError("node keys must match their names")
        for destinations in node.routes.values():
            for target in destinations:
                if (
                    target.node not in nodes
                    or target.port not in nodes[target.node].inputs
                ):
                    raise WorkflowSpecError(f"undeclared destination {target}")
                supplied[target.node].add(target.port)
    reachable = distances(nodes, entry)
    supplied[entry].update(nodes[entry].inputs)
    for name, node in nodes.items():
        missing = set(node.inputs) - supplied[name]
        if missing and (node.outcome is None or name in reachable):
            raise WorkflowSpecError(
                f"node {name!r} has unconnected inputs: {sorted(missing)}"
            )
    missing_nodes = {
        name for name, node in nodes.items() if node.outcome is None
    } - set(reachable)
    if missing_nodes:
        raise WorkflowSpecError(f"unreachable graph nodes: {sorted(missing_nodes)}")
