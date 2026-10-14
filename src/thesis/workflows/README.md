# thesis/workflows

FCA harness runners, research-model execution and local composition used by workflow extensions. The generic engine is thesis_runtime/workflows and concrete workflows are extensions.

- `__init__.py`: FCA research execution adapters; concrete workflows live in extensions.
- `backtests.py`: Execution request/result records and injected backtest runner port.
- `local.py`: Local workflow-runtime composition for the no-Azure Thesis stack.
- `research_models.py`: Execute workspace research models over deterministic harness replay.
