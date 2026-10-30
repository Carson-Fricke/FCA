# Research experiment boundaries

This package owns immutable experiment declarations, dependency interval math,
exposure accounting and evidence qualification. It does not execute harness jobs,
choose trading strategies or schedule agents.

| File | Purpose |
| --- | --- |
| `__init__.py` | Public experiment records and pure operations. |
| `contracts.py` | Experiment, partition, stopping-rule and look-budget records. |
| `grades.py` | Evidence grades and one-way exposure downgrades. |
| `intervals.py` | Declared dependency windows and deterministic overlap audits. |
| `ledger.py` | Durable experiment exposure records. |
| `mandate.py` | Explicit workflow-owned dataset windows, their execution-time resolver contract, and enforcement of phase data access. |

Hypothesis-development starts from the operator's idea. Discovery declares
explicit development and sealed forward windows before empirical work. The
execution's `research_window` artifact freezes those dates; resume reads it
unchanged. Signal lookbacks, warm-up, label purges and close-out exclusions belong
to the research design inside the windows, rather than moving dataset boundaries.
Each partition execution starts with clean strategy/account state and admits no
preboundary inputs or carried positions.
