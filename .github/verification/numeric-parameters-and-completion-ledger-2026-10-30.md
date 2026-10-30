# Numeric parameters and completion ledger verification

## Implemented scope

- Research composition recursively converts internal `$decimal` wrappers into
  JSON numbers before the authored program is constructed. Arbitrary-precision
  serde JSON support in research and the SDK preserves decimal text across the
  ABI. Integer and string types remain distinct; malformed decimal wrappers fail.
- Construction completion accepts selected annotations of retained branch jobs.
  Duplicate and unknown/not-dispatched references identify the offending IDs.
  Runtime-retained jobs remain authoritative for execution counts, budget,
  readable job history and the job IDs published to discovery. Models need not
  echo that complete ledger. Existing immutable submission verification remains.
- Shared construction guidance explains the annotation contract.
- Review-loop policy and persistence implementation were not changed. Existing
  operator pauses were not dismissed or answered on the operator's behalf.

## Verification

- Full Python suite: **1809 passed, 1 skipped in 130.05 seconds**. Includes
  required delivery architecture, interruption, acknowledgement and admission
  behavior suites; the optional live Cosmos latency test remains skipped.
- Research workflow behavior suite: **35 passed**. The new regression completes
  a phase with omitted job annotations while its execution ledger retains the job.
- Real native configuration/authored-program integration test passed. The loaded
  constructor checks `2.0`, `1.2`, integer `2`, nested `1.3`, and a 28-digit
  fractional value, then publishes a callback metric. This exercises actual
  configuration decoding and the SDK ABI rather than only receipt serialization.
- Configuration crate tests with arbitrary-precision JSON enabled: **8 passed**,
  including pinned configuration hashes.
- Clippy passed for research and strategy SDK, all targets. Repository Ruff lint,
  format, ty, whitespace and LF checks passed.

Artifact persistence investigation and the proposed Windows replacement change
are recorded separately in `artifact-persistence-investigation-2026-10-30.md`.
