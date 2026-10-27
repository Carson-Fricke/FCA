"""Pure activation/ordering contracts at the public graph execution boundary.

These tests pin routing and input identity independently of any activity host;
the conversation boundary suite covers persistence, fencing and recovery.
"""

from dataclasses import dataclass

import pytest

from thesis_runtime.workflows.graph import replay_graph, validate_topology
from thesis_runtime.workflows.graph.contracts import (
    ActivityCompletion,
    Destination,
    Node,
    WorkflowSpecError,
)


@dataclass(frozen=True)
class Receipt:
    sequence: int
    from_phase: str
    route: str
    attempt_id: str
    completion: dict[str, object]


def node(name, inputs=("input",), **routes):
    return Node(
        name,
        inputs,
        {
            route: tuple(Destination.parse(value) for value in targets)
            for route, targets in routes.items()
        },
    )


def accept(nodes, history, source, route, output):
    before = replay_graph("execution", "start", nodes, history)
    completion = ActivityCompletion(route, output)
    receipt = Receipt(
        len(history) + 1,
        source,
        completion.route,
        before.positions[source].attempt_id,
        dict(completion.output),
    )
    return (*history, receipt), replay_graph(
        "execution", "start", nodes, (*history, receipt)
    )


def test_conditional_forward_and_backward_routes_share_one_port_contract():
    nodes = {
        "start": node("start", ready=("review.input",)),
        "review": node(
            "review",
            revise=("start.input",),
            fast=("fast.request",),
            thorough=("thorough.request",),
        ),
        "fast": node("fast", ("request",), done=("publish.result",)),
        "thorough": node("thorough", ("request",), done=("publish.result",)),
        "publish": Node("publish", ("result",), {}, "completed"),
    }
    validate_topology(nodes, "start")
    history, graph = accept(nodes, (), "start", "ready", {"draft": 1})
    old_attempt = graph.positions["review"].attempt_id
    history, graph = accept(nodes, history, "review", "revise", {"feedback": "repair"})
    assert graph.positions["start"].inputs == {"input": {"feedback": "repair"}}
    history, graph = accept(nodes, history, "start", "ready", {"draft": 2})
    assert graph.positions["review"].attempt_id != old_attempt
    history, graph = accept(nodes, history, "review", "fast", {"draft": 2})
    assert tuple(graph.positions) == ("fast",)
    history, graph = accept(nodes, history, "fast", "done", {"accepted": 2})
    assert graph.positions["publish"].inputs == {"result": {"accepted": 2}}
    stale = Receipt(len(history) + 1, "review", "revise", old_attempt, {})
    with pytest.raises(WorkflowSpecError, match="stale"):
        replay_graph("execution", "start", nodes, (*history, stale))


def test_fanout_collects_required_ports_and_keeps_branch_loop_inputs_fresh():
    nodes = {
        "start": node("start", inspect=("left.input", "right.input")),
        "left": node("left", retry=("repair.input",), done=("publish.left",)),
        "repair": node("repair", done=("left.input",)),
        "right": node("right", done=("publish.right",)),
        "publish": Node("publish", ("left", "right"), {}, "completed"),
    }
    validate_topology(nodes, "start")
    history, graph = accept(nodes, (), "start", "inspect", {"revision": 1})
    assert graph.positions["left"].inputs == graph.positions["right"].inputs
    history, graph = accept(nodes, history, "right", "done", {"right": "accepted"})
    assert "publish" not in graph.positions
    history, graph = accept(nodes, history, "left", "retry", {"revision": 2})
    history, graph = accept(nodes, history, "repair", "done", {"revision": 3})
    assert graph.positions["left"].inputs == {"input": {"revision": 3}}
    history, graph = accept(nodes, history, "left", "done", {"left": "accepted"})
    assert graph.positions["publish"].inputs == {
        "left": {"left": "accepted"},
        "right": {"right": "accepted"},
    }
    assert replay_graph("execution", "start", nodes, history) == graph


def test_conflicting_active_producers_do_not_silently_choose_a_port_value():
    nodes = {
        "start": node("start", both=("left.input", "right.input")),
        "left": node("left", done=("publish.result",)),
        "right": node("right", done=("publish.result",)),
        "publish": Node("publish", ("result",), {}, "completed"),
    }
    history, _ = accept(nodes, (), "start", "both", {})
    history, _ = accept(nodes, history, "left", "done", {"value": 1})
    with pytest.raises(WorkflowSpecError, match="two live activations"):
        accept(nodes, history, "right", "done", {"value": 2})


def test_undeclared_routes_and_unconnected_ports_are_rejected():
    nodes = {
        "start": node("start", done=("publish.left",)),
        "publish": Node("publish", ("left", "right"), {}, "completed"),
    }
    # A terminal consumer has required ports too when it is reachable.
    with pytest.raises(WorkflowSpecError, match="unconnected"):
        validate_topology(nodes, "start")
    nodes = {
        "start": node("start", done=("publish.input",)),
        "publish": Node("publish", ("input",), {}, "completed"),
    }
    with pytest.raises(WorkflowSpecError, match="undeclared route"):
        accept(nodes, (), "start", "unknown", {})


def test_partial_convergence_needs_no_paired_join_declaration():
    nodes = {
        "start": node("start", inspect=("a.input", "b.input", "c.input")),
        "a": node("a", done=("pair.a",)),
        "b": node("b", done=("pair.b",)),
        "c": node("c", done=("publish.c",)),
        "pair": node("pair", ("a", "b"), done=("publish.pair",)),
        "publish": Node("publish", ("pair", "c"), {}, "completed"),
    }
    validate_topology(nodes, "start")
    history, _ = accept(nodes, (), "start", "inspect", {})
    history, _ = accept(nodes, history, "a", "done", {"a": 1})
    history, graph = accept(nodes, history, "b", "done", {"b": 2})
    assert set(graph.positions) == {"pair", "c"}
    assert graph.positions["pair"].inputs == {"a": {"a": 1}, "b": {"b": 2}}
    history, _ = accept(nodes, history, "pair", "done", {"sum": 3})
    _, graph = accept(nodes, history, "c", "done", {"c": 4})
    assert graph.positions["publish"].inputs == {
        "pair": {"sum": 3},
        "c": {"c": 4},
    }
    assert graph.scopes == {} and graph.pending == {}
