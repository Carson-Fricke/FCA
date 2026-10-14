# Production study presentation — 2026-10-12

## Delivered

- One production study card owns execution status and compact research tabs;
  author/validation receipts remain compact. Failure/cancellation retains evidence.
- Shared `EvidencePanel`, `ReturnedData`, semantic interpretation and registered
  mapping policies replace demo-only implementations. The demo uses `StudyToolCard`.
- Independent SDK `DatasetSemantics` outputs declare row meaning, grain, roles,
  ordering and preferred views without requiring an exhibit. The host validates
  complete-output grain and agreement with saved exhibit bindings.
- Returned data supports exact tables, searchable metrics, compatible custom
  mappings, source inspection, null dimension filters and retained selections
  across research tabs. Duplicate cells require explicit slices, not aggregation.
- Data loads lazily with the existing 5,000-row/approximately-2-MB budget.
  Coverage and filtering scope are explicit; exports retain complete artifacts.
- SDK fixtures, authoring guidance, local READMEs and architecture docs updated.

No canonical delivery, provider projection, submission, job allocation, or execution
retry path changed. No study was submitted for manual verification.

## Passing feature checks

| Command | Result |
| --- | --- |
| `npm run build` in `src/ui` | Passed |
| `npx playwright test e2e/study-result.spec.ts e2e/research.spec.ts --timeout=30000` in `src/ui` | 5 passed, 19.5 s |
| `npm run test -- src/tests/research-contracts.test.ts src/tests/harness-execution-card.test.ts` in `src/ui` | 10 passed |
| `cargo test --manifest-path src/harness/Cargo.toml -p harness-strategy-sdk --tests` | 23 passed |
| `uv run --extra dev pytest -p no:cacheprovider tests/thesis/http/test_exploratory_outputs.py` | 5 passed |
| `uv run --extra dev pytest -p no:cacheprovider tests/thesis_runtime/test_delivery_architecture.py` | 61 passed after final source edits |
| Focused Ruff lint/format and ty for `tests/thesis/http/test_exploratory_outputs.py` | Passed |
| `uv lock --check` | Passed |
| `uv run --extra dev ruff check .` | Passed |

Browser checks cover in-place completion, failure/cancellation, all nine study
families, keyboard tabs, mobile containment, source inspection, absent descriptors,
metrics, ambiguous heatmap cells and explicit slices, retained raw choices,
missing values, and honest 5,000-of-5,100 preview/export behavior.

Harness and Thesis were rebuilt/restarted with `scripts/local/start-stack.ps1`;
ports 8001 and 8000 returned healthy. Manual read-only inspection of current live
discovery history confirmed compact Returned data cards with zero separate harness
status cards and zero metric tile grids. Older results without descriptors retain
tables; no descriptors were fabricated for historical jobs.

Screenshots: `.local/study-production-live.jpg` and
`.local/study-production-preview.jpg` (ignored local artifacts).

## Wider integration findings outside changed feature code

- Full Python suite: **1,675 passed, 2 failed, 1 skipped**, 294.98 seconds.
  This exceeds the repository's three-minute budget. The attempted outer 180-second
  subprocess timeout did not stop its spawned pytest process; it subsequently
  completed. The unchanged full suite was not rerun.
- Both failures are in `tests/thesis_runtime/test_session_input_snapshot.py`:
  `test_tool_outcome_keeps_one_large_payload_and_explicit_correlation` and
  `test_compaction_preserves_distinct_tool_text_failure_evidence_and_pure_projection`.
  They expect the provider-supplied message ID rather than the invocation message ID
  retained by the current streaming runtime. These runtime files were not changed.
- The separately run required runtime suites (delivery architecture, arrival
  reconciliation, input snapshot and provider adapter) reported 130 passed and the
  same two failures. The architecture guard itself passes.
- Repository Ruff format check: one unrelated file needs formatting,
  `tests/thesis_runtime/adapters/providers/test_openrouter_transport.py`.
- Repository ty: four unrelated diagnostics in
  `tests/thesis/http/test_model_output_streaming.py`,
  `tests/thesis/knowledge/test_collections.py`, and
  `tests/thesis/knowledge/test_wiring.py`; optional invocation payload typing and
  provider fakes lacking `stream`.

Those unrelated runtime/provider tests and formatting were left untouched.
Full logs are under `.local/study-production-*.log`.
