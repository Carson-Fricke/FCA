# Workflow graph engine

Pure named-input and route execution. This package imports no conversation,
agent, tool, provider, persistence, clock or scheduling implementation. Hosts
bind activities and commit accepted completions outside this engine.

- `__init__.py`: Public graph replay and topology validation.
- `contracts.py`: Nodes, named input destinations, fixed route/output completions
  and definition/conflict errors.
- `topology.py`: Validates route endpoints, required input connections and reachable
  activity nodes.
- `control.py`: Replays accepted completions into activation-scoped input maps,
  causal histories, concurrent frontiers and pending input deliveries.

Every activity produces one JSON output object and selects one declared route.
A route can deliver that output to several node.port destinations. Required ports
are assembled into a fixed named map before activation. Alternative producers may
feed one port; concurrent producers require distinct ports. Conflicting deliveries
are rejected rather than selected by arrival time.

Fan-out scopes are derived execution bookkeeping, not authored split/join phases.
Scopes have no paired consumer and preserve partial convergence, branch-local
loops, nested fan-out, accepted-arrival recovery and
sibling history isolation. An execution permits one live activation per node;
revisits get new attempt identities and cannot consume previous arrivals.
