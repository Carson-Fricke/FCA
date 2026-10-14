# Fixed workflow graph verification — 2026-10-13

## Delivered scope

The generic runtime accepts fixed named split/join regions, independent branch
loops and nested regions. It reconstructs control from the existing accepted
transition journal, uses existing CAS/ownership and durable dispatch ports, and
provides sequence-ordered active-frontier/waiting-join presentation. There is no
additional scheduler, transaction framework, topology model checker or parallel
implementation for serial workflows.

Hypothesis development and research investigation retain their existing phase
flows, prompts and tool policies. Research still has one critic. The temporary
two-critic activation was removed following the user's scope clarification.
Runtime contract versions are 5.0.0 and 2.0.0 respectively, with JSON extension
protocol 2. These identify changed durable metadata, not scientific behavior.

The current contract requires explicit active frontiers/control sequence and
accepted source attempt/executor identity. Old formats and workflow versions are
rejected. No stored user data was deleted or converted in this task.

## Behavior coverage

`tests/thesis_runtime/test_workflow_graph.py` contains eight focused boundary
checks for independent loops and frozen inputs; racing arrivals and receipt
idempotency/conflict; cancellation and restart; invalid sibling crossings;
interrupted pure joins; accepted-store acknowledgement loss and terminal recovery;
nested repeated regions/stale tool authority; and real phase-completion authority.
The HTTP workflow-definition suite verifies declared split membership and joins.
The rendered UI flow verifies independent critics in a synthetic test workflow,
waiting-join navigation and rejection of a delayed earlier frontier.

Existing serial workflow suites and mandatory delivery/architecture behavior
checks remain part of the full repository run. The full suite caught an extra
serial deterministic scheduling hop; the host now preserves the existing cascade
within its admitted dispatch. Other integration corrections updated obsolete
fixtures to explicit identities, current terminal declarations and existing
knowledge-policy grants; production grants were not changed.

`test_control_completion.py`, which used a fake internal lifecycle, was removed.
Its authority and unavailable-applier cases now execute through real conversations
in `test_phase_completion_requires_real_workflow_authority_and_an_applier` in the
graph behavior suite. A private `_apply_contract` setup in the reference-rebuild
test was replaced with public completion and durable dispatch. No mandated guard
or named delivery behavior suite was removed.

## Verification results

The integration owner ran the full Python suite once, corrected its failures, and
used one permitted final full rerun. Both runs completed below three minutes.
Focused checks verified the corrections before the final run.

| Command | Result |
| --- | --- |
| `uv lock --check` | Passed; 85 packages resolved. |
| `uv run --extra dev ruff check .` | Passed. |
| `uv run --extra dev ty check` | Passed. |
| `uv run --extra dev ruff format --check .` | Final pass identified only one fixture formatting issue; all other 707 files passed. The remaining file was formatted and passed focused Ruff format/lint checks afterward. No third full run. |
| `uv run --extra dev pytest -p no:cacheprovider` | Final: **1,673 passed, 1 skipped**, 125.19 seconds; mandatory delivery guard included. |
| `npm test -- --minWorkers=2 --maxWorkers=2` from `src/ui` | **280 passed, 1 skipped**, 27 test files, 44.43 seconds. |
| `npm run build` from `src/ui` | Passed; static production build generated. |

The Python command was run through a temporary `uv run --extra dev python`
supervisor enforcing a 175-second hard timeout. The final suite itself finished
in 125.19 seconds. The supervisor and source-editing scripts were removed after
verification. The last correction was format-only in
`tests/thesis/knowledge/test_collections.py`; no behavior changed after the final
test run.

The backend skip is the opt-in real Cosmos latency acceptance test requiring
`FCA_LIVE_COSMOS_LATENCY=1`. The UI's existing opt-in wire check was also skipped.
No live deployment, data reset, external-provider run or harness submission was
performed. See the
[implementation contract](../architecture/thesis-fixed-workflow-graph-plan.md)
for deployment cutover and supported topology limits.
