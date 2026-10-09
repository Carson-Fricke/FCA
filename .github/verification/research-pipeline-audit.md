# Research pipeline integration audit

Scope: research through forward evaluation, reports, and indicator sequences.
Paper trading, funding, activation, and deployment remain separate.

## Direction and implementation

The earlier research branch already broadened the knowledge library with
directional-change events, perceptually important points, waiting-time models,
and autoencoder anomaly detection. Discovery proposes a strategy thesis and
falsification studies test its assumptions. Strategy design, backtesting/RCA,
candidate selection, null testing, variation, and forward evaluation have
accepted phase contracts and handoffs. These provide building blocks rather
than requiring a particular strategy family; their availability does not prove
that a model will discover alpha.

The integration audit found incomplete execution provenance, scientific caps
of sixteen assignments/replicates, implicit partition defaults, unsupported
null preservation claims, incomplete report comparisons, and equity sampled
only at fills. The owning implementations now address these gaps:

| Plan | Implemented boundary |
| --- | --- |
| Step 3 | The frozen design declares the experiment; durable submission records exposure and intended input before dispatch; the harness retains immutable accepted input and results; completion validates those records before accepting the agent's assessment. One retained `job_id` follows the execution throughout. |
| Step 4 | The harness owns fee-allocated closed trades and financial statistics. Marked equity includes open positions and unused capital. Headline PnL is net of execution fees; closed-trade PnL is separately named. Python imports these quantities and reasons without rebuilding a FIFO ledger. |
| Step 5 | Launch partitions and information/position dependencies are explicit. Individual and block permutations have distinct supported dependency claims. Seed plans freeze before dispatch; sufficient replicates for declared resolution run in bounded chunks. Failed replicates conservatively count as exceedances. Forward parameter vectors and variation experiments freeze before forward exposure. |
| Step 6 | Development and forward reports project accepted evidence using shared backend parameter identities. Marginal return/profit-factor plots, declared joint experiments, state/reason handling, matched comparisons, job/artifact drill-down, and complete export use backend-produced fixtures. |
| Indicators | Bar inputs produce completed value sequences, including warmup states. Trade/quote indicators aggregate output observations into causal OHLC windows at explicit nanosecond cadence. Open windows remain distinct from completed history. |

## Consolidation and cleanup

Shared research evidence functions own scalar parameter identity, document
hashing, and finite numeric views. Harness result extraction is shared by local
and HTTP execution. The unused native metrics implementation was deleted.
Implicit partition helpers, obsolete scientific cap constants, redundant
validation loops, and prompt-prose tests were removed. Report-side TypeScript
parameter canonicalization was removed in favor of backend identities.

The removed `test_*_prompt.py` tests asserted prose, which does not qualify under
the testing strategy. Their corresponding behaviors are exercised through the
discovery, candidate, null, variation, forward, research-protocol, and evidence
chain contract suites. The removed SDK-signature/prose assertions likewise did
not qualify; real SDK compilation and indicator sequence behavior are covered
in Rust integration tests. Study-validator coverage moved from a source-module
include to `api/tests/study_validation.rs` using the public validator boundary.

## Explicit scientific limits

Null evidence is selection-conditional. Search-adjusted significance is not
implemented or claimed. Neither supported permutation method preserves the
calendar placement of volatility or cross-symbol dependence; methods requiring
those properties must not claim support. Declared information and position
horizons constrain partitions but cannot statically prove arbitrary strategy
code respects them. A different frozen design evaluated on previously exposed
forward data is identified as adaptive holdout reuse in the exported report.

Indicator histories retain all completed observations in memory. Research
execution is bounded by chunk size rather than a scientific ceiling; the
caller must continue the declared seed plan using the returned offset and
reconcile unknown outcomes by retained job identity.

## Step 7: migration deletion inventory

Steps 1 and 2 were confirmed complete by the operator. The following Step 7
inventory was verified by source references, crate dependencies, product
composition, and retained behavior tests. Exports alone were not treated as
evidence of a consumer. Locations of removed code refer to its pre-cleanup
version on the research branch.

| Removed family | Evidence and body size |
| --- | --- |
| `src/harness/native` | No crate depends on it; its claimed Python loader is absent. Removed the remaining 20 Rust files, 6,987 lines, plus its manifest/workspace membership. Together with the earlier duplicate metrics deletion, the whole retired crate contained 7,678 Rust lines. The active SDK/JIT ABI remains. |
| `backtest/backtest/engine.rs`, obsolete `BacktestResult`, fill selectors and resolver | Product entrypoints execute the authored native engine. The old trait-driven engine had only test consumers. Removed 456 engine lines, 117 result-model lines, 112 selection-helper lines and a 24-line resolver. Null behavior tests now execute the active engine. Live `FillModelOptions` remains. |
| `scripts/find_dead_harness.py` | A 222-line scanner for nonexistent Python harness packages and an obsolete Rust path; no invocation outside its own usage text. |
| `thesis/integrations/harness.py` and `HarnessBacktestRunner` | A 566-line dormant HTTP adapter family plus its old workflow fallback. Both local and serving product composition already inject MCP runners. Removed adapters, aliases, fallback, its test-only fake port and facade exports; runtime now requires an explicit runner. |
| Typed harness tool argument wrappers | Approximately 159 lines of dataclasses and convenience wrappers had no product callers. Tests now exercise canonical `call_tool` dispatch, preserving interruption and acknowledgement behavior checks. SDK-reference and metadata ports with active consumers remain. |
| `thesis/research/lineage.py` | A 381-line subsystem with only facade exports and no repository consumers/tests. Execution revision/evidence ownership remains in the active workflow and research records. |
| `thesis/workflows/evidence.py` | A 236-line obsolete exploratory report builder with only facade exports. Removed its duplicate report/metric envelope; accepted phase evidence and report projections replace it. |
| Inert workflow plumbing and replay compatibility | Removed unused loader/formatter/transition helpers, abandoned state fields, discarded factory/validator/journal dependencies, unused authoring injection, retired phase-name aliases, inline-source replay and partial evidence restoration. Immutable hypothesis manifests replace flat mutable page fallback. Wire decoders accept canonical field names. |
| Report leftovers | Removed unused `getWorkflowExecution`, unused report summary, duplicate snake/camel statistics and repeated UI normalization. The drawdown chart title uses canonical statistics and reasons rather than a second computed headline. |

The Rust/scanner deletion group alone removed **7,918 code lines** in this
cleanup pass, excluding the earlier metrics removal and the Python/UI groups.
The lockfile pruned orphan dependencies without upgrading dependency versions.

### Dormant code retained for an owning decision

These are not counted as replacement-complete migration deletions:

| Remaining area | Evidence and disposition |
| --- | --- |
| `src/harness/backtest/src/indicators` (about 1,862 lines) and `signal` (about 978) | Numerical kernels are unconsumed by product execution outside their subtree/tests. Authored programs receive the SDK, which does not expose these kernels. This is a capability-access gap; deleting the library would discard building blocks rather than remove a replaced execution path. |
| `src/thesis/experiments/contracts.py` experiment/deviation subsystem (about 280 lines) | Only facade exports consume `ExperimentContract`/deviation functions. Its intended causal-identification and release-budget features have no complete replacement. `Partition`, `StoppingRule` and contract errors are actively used and must remain. |
| `runtime/recovery.rs` (163 lines), `go_live.rs` (117), `shadow.rs` (566), inert `PaperOptions` settings | Dormant or test-only live/deployment capabilities. They require an owning deployment change, which is outside this research request. |
| Workflow index's latest-execution lookup | Reachable UI still lists one latest execution per chat. This is an execution-listing product gap, not unreachable code; changing the index is separate from research evidence reports. |
| Orphan knowledge/evidence components and health helpers | No UI consumers, but pre-existing and unrelated to the research migration. They are excluded from this cleanup rather than silently bundled into it. |

The research model runner still republishes committed study sources into
model-store artifacts and transient execution bundles. That is a live path,
not dead code; further consolidation must preserve its execution/publication
boundary. No remaining Python or Rust research sweep executor, or separate
`run_id` execution plumbing, was found.
