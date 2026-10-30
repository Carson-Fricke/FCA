# Proportionate research review — 2026-10-30

## Scope and cause

This change addresses excessive research review looping only. Parameter handling,
job-ledger redesign, Windows artifact publication, queue capacity and completion
wake policy are outside this iteration.

Adjudicate previously could request only proposal, investigation or completion.
It described narrow verification in prose but could neither repair a program nor
route directly to a critic. The delegated repair phase selected review again;
there was no structured assignment carrying the intended target and scope.
Resolved objections could also be raised again without identifying new evidence,
and discovery-owned decisions had no explicit escalation disposition.

## Current implementation

- `research-investigation@6.0.0` adds direct adjudicate routes to the existing
  three-specialist fan-out and focused critic. The generic graph engine is unchanged.
- Adjudicate has the existing branch author/validate/run capabilities, development
  window, submission retention and execution budget. It records bounded corrections
  through original issue IDs and immutable job/artifact references. No new job
  service or waiting behavior was introduced.
- Continuing adjudication assigns `review.target`, `scope` and `issueIds`, including
  after delegated repair. Direct review targets must match that assignment;
  completion uses `review=null`. Unknown issue references fail visibly.
- Construction prompts follow the assigned review, broadening with reasons only
  for material changes. Adjudicate is prompted to repair bounded defects itself,
  delegate substantial work and obtain independent verification for consequential
  changes. Execution before critique and scientific sufficiency remain model decisions.
- Accepted state retains adjudicator repair references and identifies the current
  product through `reviewHandoff`. Later adjudication does not erase earlier repairs.
- Reopening a settled original issue requires explicit `reopenReason` containing
  new counterevidence. Collection retains simultaneous objections even when another
  reviewer reports verification; collection does not vote or select a verdict.
- `escalate` carries discovery-owned decisions out of local repair assignments and
  into the existing canonical branch outcome. No separate notification path exists.

No new phase, module, scientific gate, review-round cap or runtime dependency was
added. The package README and runtime architecture describe the current contract.

## Verification

The composed workflow behavior suite covers direct focused/full review after an
adjudicator edits and executes an existing program; independent immutable-source
reads; full three-port collection; preservation of repairs and escalations through
completion; invalid/missing review assignments; repeated settled objections and
explicit reopening; and scoped review after delegated repair. Adjudicator study
and strategy submissions also exercise retained identity through lost/mismatched
acknowledgements and recovery without replay, under the existing branch budget.

- `uv lock --check`: passed.
- `uv run --extra dev ruff check .`: passed.
- `uv run --extra dev ruff format --check .`: passed (877 files).
- `uv run --extra dev ty check`: passed.
- `uv run --extra dev pytest -p no:cacheprovider`: **1817 passed, 1 skipped**
  in **129.98 seconds**. Includes mandatory delivery architecture and behavior suites.
  The skip is the opt-in real Cosmos latency acceptance. Two existing Starlette
  deprecation warnings were reported.
- `node scripts/ci/line-endings.mjs`: passed; repository text is LF.
- Scoped `git diff --check`: passed.

These scripted model tests establish workflow and interface behavior, not the
quality of future model decisions or strategy profitability. A fresh model run is
needed to assess review selection in practice.

## Local activation and cutover

Restarted only the affected Thesis service with
`powershell -NoProfile -ExecutionPolicy Bypass -File scripts/local/start-stack.ps1 -Servers thesis -Foreground`.
Worker log: `.local/logs/restart-20261030-112643.log`.

An authenticated read of the running workflow definition confirmed version 6.0.0
and all five adjudicate routes. Thesis and harness health endpoints passed.
No native or UI source changed in this iteration, so no native/UI rebuild was needed.

New executions use the current contract. The existing version checks reject old
execution resumption; no historical decoder or conversion was added. Historical
conversation records remain available as audit evidence for the investigation;
no old execution was automatically resumed, reinterpreted or answered.
