# SDK routing optimization - 2026-10-27

## Scope and current contract

Removed per-event routing-graph construction from the SDK's exported study and
strategy callbacks and its direct routing helpers. A `RoutingPlan` captures the
consumer's canonical requirements, declared dependency tree and identity-to-slot
mapping once, after final placement. Dispatch resets a reusable visited bitmap
instead of allocating a traversal set. Exported programs allocate their plan
automatically; direct `route_quote`, `route_trade` and `route_bar` callers now
pass their previously constructed mutable plan. All repository callers were
updated to that single current contract.

Runtime guards compare declarations against the captured plan instead of
constructing another signature tree. Stable raw declarations use direct
comparison; a canonical-equivalent permutation still passes. Dependency
identity/order, count, output cadence, cycle rejection at construction and
zero-sized dependency rejection remain enforced. Borrowed dependency pointers
are never retained in the plan. Root movement or topology replacement is
rejected rather than dereferencing a cached pointer.

Routing owns the market callback checks. Redundant outer market checks were
removed from the export trampolines; start, fill and stop still have their
before/after lifecycle checks. Callback mutation is rejected before subsequent
parent dispatch, and exported states remain failed after a routing violation.
Shared dependencies still update once per event, with traversal slots reset
between events. Causal indicator sequence advancement and recorded output
behavior are unchanged.

`MarketConsumer::subscriptions()` and `indicators()` still return owned vectors,
so checking those declarations still allocates. This change removes graph
reconstruction; it does not claim allocation-free routing. No quote-snapshot
API is included in this routing change. The user requested it as the next part,
after the concurrently active position/accounting SDK work releases the shared
files.

## Repeated microbenchmark

Used the same original fade strategy source and optimized standalone probe as
the diagnosis. The probe calls its real SDK quote trampoline. Each timing has
five samples of 100,000 callbacks after warm-up. Allocation counts are measured
in a separate 100-callback interval, excluding construction. Background work was
not stopped, and concurrent accounting changes also modify the SDK; these are
observational before/after measurements, not an idle-host binary A/B isolating
only this patch.

| Path | Before median | After median | Before / after allocations |
| --- | ---: | ---: | ---: |
| Original quote method directly | 0.244526 us | 0.261542 us | 2 / 2 |
| Original method through SDK | 9.746506 us | 2.385960 us | 230 / 57 |
| Empty method, same wavelet dependency | 9.456195 us | 2.229701 us | 228 / 55 |
| Empty method, no indicator dependency | 5.065172 us | 1.370097 us | 126 / 27 |

The actual SDK callback is approximately **4.1x faster** in this probe. Requested
allocation bytes fall from 22,682 to 1,250 per actual quote callback. The
performance regression test sets a 64-allocation budget per exported callback
for the matching empty-method/wavelet-dependency shape, rejecting the previous
228-allocation behavior without relying on wall-clock thresholds.

Raw probe source, executable and samples are retained under ignored
`.local/backtest-diagnosis-2026-10-27/`: `routing-probe.rs`, `routing-probe.exe`,
`routing-probe-after.txt`, and the unchanged diagnostic baseline `sdk-probe.txt`.
No new full-window backtest speedup is claimed by this microbenchmark.

## Verification

- `cargo check --manifest-path src/harness/Cargo.toml -p harness-strategy-sdk --tests`
  passed during implementation.
- `cargo test --manifest-path src/harness/Cargo.toml -p harness-strategy-sdk`
  passed: **35 integration tests and one compile-fail documentation test**.
  This includes exported mutation rejection and permanent failure,
  canonical-equivalent ordering, the allocation budget, repeated shared delivery,
  dependency-before-parent rejection, causal sequence behavior and wavelet
  prefix/future-perturbation invariance.
- `cargo test --manifest-path src/harness/Cargo.toml -p harness-strategy-jit --test compile`
  passed: **two tests**, 24.57 seconds including compilation/process setup and
  19.57 seconds test time. This exercises real authored compilation, loading,
  routing, cache reuse, callback failures and current ABI behavior.
- Git whitespace and repository LF checks passed.

Verification used focused suites while the separate accounting chat remained
active. No repository-wide suite was run over its changing files. The running
local harness was not replaced; release SDK probe results are not a claim that
new native/SDK binaries have been deployed. Prior full-suite timeout limitations
remain recorded in the October 26 evidence.
