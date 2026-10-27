# Provider continuation and message simplification — 2026-10-24

## Current behavior

There are two logical message representations: canonical `SessionMessage` and
native `ProviderMessage`. `ModelCompletion` wraps the native message with
accounting and attribution. The durable `ChatMessage` remains a storage/UI
envelope; its presentation fields do not drive provider replay.

The provider boundary retains raw scalar reasoning and structured continuation
separately from normalized display text. Consecutive compatible readable
fragments merge; encrypted and unknown blocks remain separate and ordered,
including repeated IDs. Signatures, empty fields and opaque payloads survive
tool rejection, subsequent tool turns and persistence hydration. Stream indices
are transport coordinates and are removed from completed blocks.

Live execution and replay share the pure projection, family preparation and wire
encoder. Audit dictionaries are no longer decoded back into native messages.
Serving composition binds continuation to the configured provider/model.
Incoming data cannot inject native continuation, and interrupted or incomplete
call groups do not replay executable state. The canonical inbox and incoming
writer are unchanged.

## Focused verification

- Provider suites, HTTP continuation behavior and the required delivery
  architecture guard: **176 passed**.
- Repaired provider-identity fixtures and root persistence behavior:
  **17 passed**; focused Ruff and ty passed after formatting.
- The HTTP behavior uses the real conversation, provider transport and Cosmos
  persistence adapters with fake external HTTP/storage boundaries. It compares
  audited messages with actual provider request messages, reopens persistence,
  and verifies origin mismatch and outdated-state rejection.
- No live model request or paid provider qualification was performed.

## Frozen integration

The first integration found missing provider identity in two test fixtures and
missing type narrowing in a test assertion. These were corrected before the
single permitted final integration rerun.

- `uv lock --check`: passed.
- `uv run --extra dev ruff check .`: passed.
- `uv run --extra dev ruff format --check .`: passed, 821 files formatted.
- `uv run --extra dev ty check`: passed.
- `uv run --extra dev pytest -p no:cacheprovider --durations=10`:
  **1,710 passed, 1 opt-in live Cosmos test skipped, 162.09 seconds**.
  This includes the mandatory delivery architecture guard.
- Repository LF normalization and Git whitespace checks passed.

## Cutover

Previously attributed completed model messages without a captured continuation
marker are outdated for resume. Their missing provider state cannot be recovered
from normalized UI reasoning. Affected development sessions require fresh
context at deployment. No existing session data was deleted or converted, and
no compatibility decoder was added.
