# Workflow routes and named inputs

This is the current execution contract. Workflows declare activity nodes, required
input ports, named routes and terminal outcomes. There are no split/join phases or
parallel blocks. The generic graph engine has no conversation, agent, provider,
tool, persistence or scheduling dependencies.

## Definition and completion

Each route is a set of explicit `node.port` destinations. Connections are declared
once in the route table. A completion has one selected `route` and one JSON-object
`output`. Only destinations on that route receive its output. Forward branches and
loopbacks use the same rule; stored results on unselected routes supply no inputs.
The definition endpoint publishes `inputs` and `routes` alongside node identities
and terminal outcomes. The UI derives edges from destinations and never invents
control flow.

A node starts only when every required port has one value. Its input is always a
map from the declared port names to accepted output objects. Concurrent aggregation
uses separate required ports. Alternative paths feed the same port. Duplicate
conflicting producers are rejected; arrival order never chooses a winner.

A route with several destinations creates concurrent eligibility. There is no
activity for starting branches. A multi-input consumer provides synchronization,
regardless of whether its implementation is deterministic, agent-backed or terminal.
Domain reduction is ordinary activity work, separate from input synchronization.

## Activations and replay

Every visit has a stable attempt identity scoped to the execution and causal
history. A workflow permits one live activation per node. Fan-out occurrences
retain causal branch coverage through partial convergence; no paired join is inferred. Branch-local
loops and nested fan-out preserve sibling isolation and cannot reuse arrivals from
an earlier occurrence. A conditional return to a prior node creates a new attempt.
Unfinished concurrent branches prevent successful workflow finalization.

The execution document is the only accepted history. A replay folds explicit source
attempt, selected route and output into the frontier, required input maps and pending
deliveries. The engine neither executes activities nor interprets domain outputs.
`pendingInputs` events project required and arrived ports for UI display; they are
not execution authority or a second inbox.

## Host ownership

`workflows/graph/` owns pure definitions, topology validation and activation replay.
`workflows/workflow.py` is the conversation-facing host: it binds activities,
validates trusted executor/intent identity, commits by execution-store revision CAS,
and publishes lifecycle projections outside acceptance. `phase.py` describes host
activity bindings, not generic engine activity types.

The host translates its domain phase-completion contract into a selected route and
opaque output. Domain extensions receive the fixed named input map and their scoped
accepted products. Agents own their turns, operator input and completion requests.
The graph never sees a conversation or drives a model. Incoming messages and tool
results retain the existing canonical writer and provider projection.

Durable work is retained per activation. Only deterministic bindings explicitly
marked `replay_safe` may be recomputed after interruption. Unknown-outcome mutations
are reconciled by retained identities, never replayed automatically.

## Current-contract cutover

Hypothesis development is `6.0.0`; research investigation is `4.0.0`. Accepted history
requires explicit `route` and `output` fields as well as attempt/executor identity.
Old definitions, split metadata and old history are rejected. There is no decoder,
version-selected implementation or read-time conversion for the previous format.
Existing obsolete development executions require an explicit reset in their owning
environment. This source change performs no local or Azure data deletion.

## Verification

- `tests/thesis_runtime/test_workflow_ports.py`: pure forward/backward routing,
  alternative producers, required ports, activation freshness and conflicts.
- `tests/thesis_runtime/test_workflow_graph.py`: real conversation completion,
  concurrent arrivals, nested fan-out, loopbacks, cancellation, restart and lost
  acknowledgement recovery through the authoritative store.
- `tests/thesis_runtime/test_package_boundaries.py`: mandatory graph modules and
  prohibition on conversation, agent, provider, tool and storage imports.
- `tests/thesis_runtime/test_delivery_architecture.py`: the existing canonical
  incoming writer, shared projection and durability guards remain mandatory.
- `tests/thesis/http/test_workflow_routes.py`: current topology and version rejection.
- Research workflow behavior and UI workflow graph/live/card tests cover current
  product composition and pending-input presentation.
