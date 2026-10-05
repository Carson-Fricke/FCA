# Screener Rust-Only Migration

<!-- markdownlint-disable MD013 -->

## Status

Frozen 2026-09-28. This is the design of record for the screener migration and
delivers N4 of `rust-native-authoring-plan.md` ("Screener filters become
Rust-authored programs or the feature pauses until ported"). Work lands on
branch `rust/screener-migration`; the base is `f0efb64`.

## Operator decisions (2026-09-28)

1. **P0 deletes the legacy Python screener path in full.** Nothing is kept,
   marked legacy, aliased, or stubbed. The feature is dark until P5.
2. **Ensure is stratified by data type.** One request per
   `(asset_class, data_type, symbol, window[, interval])` for `trade`, `quote`,
   and `bars`. The caller fans out; the lake resolves fetch-or-cache per type as
   fast as possible.
3. **`bars` is a lake data type**, fetched from the provider bars endpoints
   when available and derived from trades otherwise. Standard OHLCV with
   standard session offsets, exact decimals. The build convention is fixed, not
   under iteration.
4. **`interval` is a partition dimension now.**
   `date=<session>/asset_class=<class>/data_type=bars/interval=<token>/SYMBOL.parquet`.
   Tokens pinned: `1min`, `1d` (`1s`, `1h` reserved).
5. **The harness never reads lake files.** It calls lake routes only. The
   Arrow IPC streaming read route lands as part of this migration, not as a
   follow-on.
6. **DuckDB stays lake-internal** (`lake-query`). The harness never links it.
7. **Screeners are full Rust programs**, JIT compiled. No interpreter mode; the
   UI generates source from the filter builder.
8. **Thesis owns screener definitions** as content-addressed workspace
   artifacts. The harness stays workspace-agnostic: source + params + bundle
   hash travel per run.

## Hard rules (no divergence)

- **Delete, do not deprecate.** No shims, compatibility aliases, dual code
  paths, feature flags, or temporary fallbacks.
- **No test fudging.** Do not weaken assertions, skip tests, mock around a
  missing implementation, or add an API solely so a test passes.
- **Subagents test only their own change** with focused checks. The
  coordinator runs repository-wide gates once, after edits freeze.
- **Integration-owned paths:** `.github/architecture/**`, `.github/workflows/**`,
  `.github/instructions/**`. Subagents never edit them.
- **Determinism:** no wall clock, unseeded randomness, or direct network access
  in simulation code.
- **Rust conventions:** tests live under `tests/` (unit tests via `#[path]`),
  never `#[cfg(test)]` in `src/**/*.rs`. Python gates use `ruff` and `ty` only.
- **Architecture contract:** the lake owns storage; the harness depends on the
  lake's HTTP contract, never its layout. Schemas and value objects may be
  shared through `lake-contracts`; file readers are a lake concern.

## Target shape

```text
UI (Rust editor + filter builder, source generator)
        │ authored Rust source + params
        ▼
Thesis  ── definitions/authorization/artifact import only
        │ MCP run_screener (harness-hosted)
        ▼
Harness ── JIT compile, ScreeningProgram, reads lake by route
        │ HTTP: ensure / coverage / streaming Arrow IPC / reference
        ▼
Market lake ── fetch (provider-first), derive bars, store, classify coverage
```

## Phases

### P0 — Delete the legacy screener path in totality

- Thesis: `src/thesis/screening/**`, `src/thesis/http/screeners.py`,
  `src/thesis/http/screening.py`, screener wiring in
  `src/thesis/http/application.py` and `src/thesis/http/workspaces.py`,
  `tests/thesis/screening/**`, screener sections of
  `tests/thesis/http/test_data_routes.py`.
- UI: `src/ui/src/lib/components/screener/**`, the screener route, nav /
  command-palette / top-bar entries, `api.ts` and `types.ts` screener sections,
  screener fixtures, screener tests.
- No replacement surface. Deleted code is not referenced from docs before P6;
  code must not reference it at all.

### P1 — Contracts (implemented and green)

Contracts are code: types, schemas, models, and their focused tests. Behavior
(ensure, derivation, streaming, execution) is P2/P3.

- **Lake contracts:** `DataType::Bars`; interval partition segment and path
  builder; bars Arrow schema; interval token validation; ensure request model
  with `interval`; bar provenance (`source = provider | derived`,
  `source_revision`); streaming read request/response frames; reference route
  request/response models.
- **Harness contracts:** `shell: screen` config model; authoring
  `ScreeningProgram` trait + vtable + entry macro + ABI version and capability
  gate; screening context surface; MCP `run_screener` /
  `get_screener_status` / `screener_config_template` schemas; result artifact
  schema (`screen.json`).
- **Thesis contracts:** definition document (source, params, universe, as_of,
  lookback, `bundle_hash`), run-import record, error taxonomy (422/503), tested
  against a fake MCP client.
- **Architecture test:** the harness contains no path-based lake access.

### P2 — Lake bars and streaming reads

- Bars ensure: provider-first through the existing provider lane and request
  governor; derived-from-trades fallback, which ensures trade coverage for the
  same window first (one stable job id, resumable, repairable, T350 terminality
  rules). Coverage is per `(session, interval)`, and a covered window returns
  immediately.
- Provenance and repair: each bar partition records source and source revision.
  A trade repair invalidates derived bars; provider bars replace derived bars
  when they later become available.
- Streaming Arrow IPC read route over the lake's own storage; chunked,
  timestamp-ordered, bounded buffers.
- Byte-parity oracle for bars against the pinned convention.

### P3 — Harness lake client and screening jobs

- One async Rust lake client (ports the Python `lakeclient` facade, folds in
  `LakeEnsureClient`): ensure, coverage, jobs, streaming read, reference, bars.
- Retire direct reads: `--data-dir`, `.local/market-lake`,
  `HARNESS_DATA_DIR`/`LAKE_LOCAL_DATA_DIR`, `marketdata` path readers,
  `strategy-jit` `&Path` data roots. Replay streams through the route.
- `ScreeningProgram` execution behind the JIT seam; `screen-job`; HTTP
  `POST /v1/screeners/jobs`, `GET /v1/screeners/jobs/{id}`; MCP tools;
  artifacts in the existing RunStore. Ensure bars (which ensures trades) before
  a run; fail loud when the lake cannot serve the window.

### P4 — Frontend Rust authoring

- Rust editor replacing the Python editor; filter builder generates SDK source
  or `shell: screen` parameters; server compiler diagnostics surfaced inline;
  provenance display (bundle hash, compiler output, run id).

### P5 — Thesis as a thin client

- Definitions persist as workspace artifacts (source + params + hash); runs are
  submitted through MCP and imported as workspace results, mirroring
  `thesis.backtests`. Harness unavailable fails closed. No `lake`,
  `lakeclient`, or `harness.*` imports anywhere in Thesis; enforced by test.

### P6 — Docs and closeout

- Update `repository.md`, `rust-native-authoring-plan.md`,
  `fca-thesis.md` and `thesis-ui-design.md`, and record the decision in Git.

## Frozen contract notes

- Bar provenance: `source=provider` must replace `source=derived` when provider
  bars become available for the same window; coverage reflects the replacement.
- Screening result ordering: primary metric descending, symbol ascending on
  ties; identical inputs produce byte-identical results.
- Screening program capability: no order surface by construction; no file,
  network, or clock access.
- MCP naming follows the existing catalog: `run_screener` returns a run id and
  state; status follows `get_screener_status`; artifacts use the existing
  `get_run` / `list_artifacts` / `read_artifact` tools.
