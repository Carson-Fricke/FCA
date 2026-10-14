# Discovery research loop verification — 2026-10-11

## Scope and refactor review

Reviewed the working-tree runtime extraction, extension composition, current
delivery contract and its existing verification record. This task changes FCA
prompting/retrieval content in `thesis`, not `thesis_runtime`, topology, delivery,
job admission or execution. Existing parallel refactor edits are retained.

- Discovery now develops provisional trading logic through a recurring
  construction/uncertainty/investigation/interpretation/revision loop. Before an
  investigation it explains the decision contrasting outcomes would inform.
- Prompt and role skill cover causal representation inspection, scoped evidence
  interpretation, bounded refinement, status questions, new directions and
  dispatched work. Formal model-creation guidance is explicitly phase-scoped;
  the role skill bundle version is 1.7.0.
- Three construction cards include a hypothetical worked example with operator
  steering. Study-design, continuation/reversal, cost/delay and executable-tool-bag
  cards link them and qualify overly broad screening/stopping conclusions.
- Card versions, retrieval dates and body hashes were updated. READMEs and the
  runtime architecture's discovery guidance paragraph were updated in place.
- Corpus/service tests exercise integrity, title retrieval, contextual discovery
  queries, returned-reference reads and complete delivery within 6,000 characters.
  No prompt-substring assertions or engine research-loop enforcement were added.
- `.github/runbooks/thesis-discovery-evaluation.md` defines manual actual-model
  cases and an observable assessment rubric. It is not completed evaluation evidence.

## Checks

All Python checks ran through `uv` from the repository root.

- `uv lock --check`: passed.
- `uv run --extra dev ruff check .`: passed.
- `uv run --extra dev ruff format --check .`: passed, 663 files.
- `uv run --extra dev ty check`: passed.
- Focused knowledge/discovery/handoff/delivery-architecture run: 195 passed,
  one retrieval failure in 12.95 seconds. The new episode-card title was buried
  by the offline search baseline. Its title and prompt search phrase were made
  more specific; no search algorithm or test ranking threshold was changed.
- `uv run --extra dev pytest -p no:cacheprovider tests/thesis/knowledge/test_library.py`:
  all three final corpus/retrieval tests passed in 2.17 seconds, including the
  added contextual search/read behavior test. Focused Ruff and ty also passed.
- One full `uv run --extra dev pytest -p no:cacheprovider` was launched under a
  subprocess hard timeout of 180 seconds. It collected 1,635 tests and reached
  approximately 90%, including the delivery architecture guard and arrival
  reconciliation suite, without reported assertion failures before timing out.
  This is **not a full-suite pass** and does not meet the repository time ceiling.
  The unchanged full suite was not rerun.
- Isolated the active suite with a 30-second hard timeout:
  `uv run --extra dev pytest -p no:cacheprovider tests/thesis_runtime/test_initial_intake.py --durations=3`:
  seven passed in 5.55 seconds. No isolated hang was observed.
- Checked the remaining runtime suites under a 60-second hard timeout:
  `uv run --extra dev pytest -p no:cacheprovider tests/thesis_runtime/test_lifecycle_acceptance_guard.py tests/thesis_runtime/test_package_boundaries.py tests/thesis_runtime/test_session_delivery.py tests/thesis_runtime/test_session_input_snapshot.py tests/thesis_runtime/test_session_live_activity.py tests/thesis_runtime/test_session_projection.py tests/thesis_runtime/test_turn_retirement.py tests/thesis_runtime/test_unified_conversation.py tests/thesis_runtime/test_wire_schemas.py`:
  131 passed in 2.73 seconds. This covers remaining tests separately, not an
  uninterrupted full-suite or three-minute-budget verification.

## Limits

No local services were restarted, no research execution was launched and no actual
model-behavior evaluation was performed. New discovery conversations receive the
new bootstrap after the backend loads the changed source; existing phase transcripts
are not reseeded by this edit. Prompts, knowledge cards and deterministic tests do
not prove that a model will comply. The manual evaluation remains to be run against
the intended model and permitted research data. Unrelated expensive tests were not
pruned to make this prompting task pass the integration time ceiling. Historical
verification records were not rewritten.
