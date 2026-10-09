# Research conversation launch RCA

Scope: restore local model responses, launch hypothesis development from the
MSFT intraday request, and verify that discovery executes real studies.
The operator requested no automated tests; none were added or run for this fix.

## Causes and corrections

- The research cleanup removed the unused `strategy_artifacts` argument from
  `build_serving_stack`, but the local launcher still passed it. The startup
  log recorded `TypeError: build_serving_stack() got an unexpected keyword
  argument 'strategy_artifacts'`. Removed the obsolete caller argument in master.
- The research checkout subsequently started with its own environment file,
  which lacked `OPENROUTER_API_KEY`. Its delivery-only composition durably
  accepted the operator message without scheduling model execution. Restarted
  from master's configured environment instead of changing that offline contract.
- Restarting only the API/UI exposed a harness bearer mismatch: the harness
  still ran from the research checkout with its separate credentials. Restarted
  lake, harness, API and UI together from master, retaining the Cosmos service.
- A live model response then requested routine partition-day confirmation and
  misinterpreted the forward holdout as a signal scoring horizon. Updated the
  root instructions and launch-tool description: the agent declares defensible
  explicit spans from the request and freezes them before launch. Forward data
  remains sealed; there is no implicit partition default or operator gate.

The sole remaining tracked edit in the research checkout matched master's
launcher fix byte-for-byte. Saved its ignored environment under
`.local/retired-research-worktree/.env`, then removed that exact registered Git
worktree. Its branch commits remain available. All application server paths
were verified under `C:/Users/csfri/source/repos/FCA`.

## Live evidence

The originally linked chat had been deleted by the time verification ran.
Submitted the exact original MSFT prompt to the new `MSFT intraday discovery`
conversation, `chat-a4a22715dc304e90886f5c271062399e`.

- Workflow: `hypothesis-development`, version `2.3.0`.
- Execution: `execution-c83391a8b8e446bea82686b9208c3dce`, running in `discovery`.
- Executor: `subagent-a87c291307e444678cbe709b14f5864d`, running.
- Explicit launch partition: history 120, forward 20, information horizon 10,
  position horizon 5, liquidation 2 calendar days.
- Discovery posted its plan, retrieved directional-change/conditioning and data
  caveat cards, read SDK signatures, and authored fresh studies inside development.
- Baseline job `job-b8405728d1174cfc8a8cef96ec40ecfe`: harness receipt
  `succeeded`, 15,600 bars, 15 artifacts, July 6 through August 28, 2026.
- Overshoot job `job-aa98e982c5bc4e659d53f81d14ea73c2`: harness receipt
  `succeeded`, 15,600 bars, 15 artifacts over the same development slice.
- A third study was authored, validated and submitted. No unsuccessful tool
  receipts appeared in the observed discovery transcript at this point.

This verifies launch and real discovery execution, not alpha or completion of
the research pipeline. The workflow remains running for the operator.
Focused Ruff lint and formatting and Git whitespace checks passed.
