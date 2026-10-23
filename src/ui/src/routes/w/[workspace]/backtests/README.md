# Backtest executions

- `+page.svelte` displays paginated retained executions, actual harness state,
  evidence availability, queue/execution timing and window. It filters by
  strategy and locally by execution state, and retries observational read failures.
- `[id]/` inspects a retained job independently of report materialization.

This surface cannot automatically resubmit an execution.
