# Backtest discovery and published evidence

This package owns workspace execution discovery and report projections. Harness
job records remain the execution authority. It does not submit, replay, or wait
for jobs, and never rebuilds financial statistics from chart points.

- `__init__.py` exports the report coordinator and artifact importer.
- `catalog.py` discovers retained submissions, neutral runtime tool-job references,
  and existing reports. It observes bounded pages, retains the last truthful
  observation on read failure, and inspects exact accepted inputs and evidence.
- `records.py` defines report records and their add-if-absent persistence port.
- `coordinator.py` imports a terminal success on request or tool-result observation.
- `run_import.py` maps canonical published metrics, closed trades, and equity into
  report records, preserving missing values and their reasons.
- `artifacts.py` reads immutable artifacts through MCP or their published URL.
  Report previews require a declared Parquet size no larger than 16 MiB.

HTTP adapters consume these records and services. This package has no dependency
on `thesis.http`; session delivery remains in `thesis_runtime`.
