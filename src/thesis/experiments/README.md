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
| `mandate.py` | Launch-frozen research partitions and enforcement of phase data windows. |

Hypothesis-development launches declare history and forward lengths explicitly.
Their conservative information and position horizons derive the purge gap;
liquidation duration derives the embargo. The durable launch retains the source
declarations and derived partition. Resume never selects a default partition.
Each partition execution starts with clean strategy/account state and admits no
preboundary inputs or carried positions. Declared horizons constrain research;
they do not statically prove every authored strategy obeys its declared horizon.
