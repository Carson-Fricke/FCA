# Supplemental Listings Dataset Decision

This record logs whether the conditional supplemental listings dataset
(T136) is triggered. The task is explicitly conditional: it must be justified
by evidence from the delisted-history coverage sweep or a selected strategy
scope, not implemented by default.

## Trigger Evidence Reviewed

| Source | Observation |
| --- | --- |
| `.github/research/alpaca-delisted-coverage.md` (T133 sweep memo) | Recommendation is "GO, **provisionally**: retain/add supplemental listings data." The memo states the sweep "does not establish listing identity or lifecycle dates by itself" and leaves a review step: compare each `empty` result and the historical dates for each `records` result against a listings source before deciding whether the supplemental source can be narrowed or removed. |
| `.github/research/alpaca-delisted-coverage.json` | Sample of 22 records, 8 empty, 0 errors, `recommendation: "go"`. The probe reports `go` whenever any symbol is empty or errored, which is a low bar and not a source decision. |
| `config/security_master/` | Contains only `.gitkeep`: no selected source, no schema, no sample payload to reconcile against. |
| `src/harness/data/listings_import.py` | Does not exist. |

The consumer today is the lake listings table behind `ListingsCatalogPort`
(`src/harness/data/security_master.py`), fed by `InMemoryListingRepository` /
the Azure Table catalog. No supplemental exchange, SEC, constituent-history, or
paid reference has been selected, and no reconciliation fixture exists.

## Decision

Ingestion is **not triggered this wave**. The provisional sweep result is not
sufficient evidence to choose a source, size a schema, or reconcile row counts,
so implementing an importer now would be speculative. This record is the
T136 deliverable: the task now reflects the evidence instead of remaining an
unconditional implementation task.

The conditional acceptance items (row-count reconciliation, delisting-return
settlement, focused tests) do not apply until a source is selected.

## Re-Trigger Criteria

Re-open this decision when all of the following exist:

1. A selected listings source (exchange, SEC, constituent-history, or paid
   reference) with documented licensing.
2. A sample payload committed under `config/security_master/` with a typed
   schema.
3. The T133 review step completed against that source, showing gaps the current
   listing-episode data cannot cover.

## If Triggered

The importer belongs lake-side, not in `harness.data`: it should be
`src/lake/reference/listings_import.py` (mirroring the reference-config
pattern) writing through `ListingsCatalogPort`, because listings identity and
lifecycle dates are a lake concern and `harness.data.security_master` now has a
Rust port that must stay in parity. The declared `src/harness/data/` path in
T136 predates that boundary and should be corrected when the work is picked up.

## References

- `.github/architecture/repository.md` (`harness.data`, market-lake reference
  ingestion)
- `src/lake/tools/delisted_probe.py`, `.github/research/alpaca-delisted-coverage.json`
- `src/harness/data/security_master.py`, `src/harness/data/listing_events.py`
