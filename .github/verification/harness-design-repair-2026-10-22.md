# Harness design repair — 2026-10-22

Status: complete. All requested repair areas are implemented; integration findings were resolved with focused reruns under the repository’s two-full-run limit. The verification history below distinguishes full-suite results from corrective runs.

## Scope and baseline

Implements the seven repair areas and concrete defects identified in chat `01a161a6-d1ab-7e90-8ed1-43762e43ed6e`. The starting tree contained unrelated uncommitted work. Structural measurements compare source hashes/line counts captured at task start, not HEAD. Unrelated Python/UI work was retained.

The delivery includes the pre-existing causal-valuation foundation incorporated
into the replay/research decomposition, including its metric support and behavior
tests. Independent SDK partial-fill/example changes and Python/UI/PostgreSQL work
remain outside this commit. The integration results below were measured in the
combined working tree; they do not imply those unrelated changes are included.

Before pushing on October 23, the staged index was exported to an isolated
snapshot without the unrelated working files. Its five configuration-parity
tests, four existing SDK-accounting tests and package-boundary guard all passed.
Staged whitespace checks passed, and file hashes confirmed staging preserved the
working files. These focused checks verify the separated delivery scope; they
are additional evidence, not another full-suite run.

## Instruction change

The organization instructions now require five ordered phases: establish behavior and invariants; trace implementation and blast radius; assign responsibility/state/dependency ownership; implement through those boundaries; remove superseded paths and update documentation. Existing verification instructions remain authoritative; no sixth duplicate verification phase was added.

Source documentation now requires concise local responsibility contracts for substantive changed modules and meaningful interfaces. Contracts describe actual ownership, excluded authority and failure semantics. They cannot be broadened merely to excuse mixed responsibility. File length, comments and import checks are explicitly insufficient evidence of cohesion. Ports need current consumers; one implementation can be appropriate.

## Repair scorecard

| Review area | Implemented outcome | Evidence to assess it |
| --- | --- | --- |
| Execution ownership | Neutral execution crate owns admission, lifecycle, fencing and fleet rules; host owns Azure/process composition. | Parsed dependency guard; HTTP admission, interruption and duplicate-claim behavior. |
| Store authority | Separate admission, queries, artifact reads, publication and retention. Opaque owned handle is created only by a successful claim and rejects other job IDs. One lifecycle serves conditional memory/filesystem/cloud adapters. | Storage ownership tests, immutable admitted input/creation identity tests, terminal-write and partial-publication tests. Metadata inventory does not fetch payloads; file publication streams. |
| Trading policy | Both books call one checked ledger transition; both touch matchers call one activation/price policy; fee formulas and scalar notional caps are shared across exact-decimal representations. | Atomic overflow regressions at both book boundaries; replay stop/stop-limit, risk reduction/recheck and halt-scope behavior; live cap rounding regression; fee overflow/contract-integrality cases. |
| Replay inversion | Replay consumes supplied program instances, re-iterable inputs and scratch allocation/capacity. Host owns acquisition/cursors, environment settings and telemetry cadence. | Streamed/in-memory dispatch and valuation equality; compressed history/clone preservation; dependency guard excludes concrete lake/JIT clients from backtest. |
| JIT scope and lifetime | Research owns configuration/job orchestration/acquisition/publication. JIT owns compilation/loading/adaptation. Vtable references borrow loaded owners; raw state is private; unjustified unsafe Send was deleted. | Rust visibility/lifetime checking and retained real compilation/ABI/provenance behavior suites. |
| Market and tool ports | Acquisition, snapshots, reference values and option resolution are typed capabilities. Native option follow-up uses injected ports. SDK-reference reading has its own read-only port. MCP runtime handlers retain only their operation capability sets. | Native option retained-ID/mismatch tests; MCP authentication, acknowledgement mismatch, SDK identity and validation tests. |
| Current contracts and cleanup | Strict shared configuration decoding; immutable rounding/model state and read-only validated configuration; canonical imports and explicit exports. Removed alternate store implementations, old artifact-prefix helper, unported runners and replay subprocess fallbacks. | Shared configuration contract tests; compiler checks; no compatibility facade retaining removed stores. Architecture and local inventories updated. |

## Structural measurements

These counts describe decomposition only. They are not quality scores.

| Original hub | Baseline lines | Current disposition |
| --- | ---: | --- |
| `backtest/src/backtest/engine_core.rs` | 2614 | 792 lines. Replay coordination; subscription planning, event ordering, venue state, risk projection, outcomes and identity have owners. |
| `backtest/src/backtest/job.rs` | 1777 | Removed; research owns configuration, acquisition, replay composition, ledger reconstruction and publication. |
| `mcp/src/tools.rs` | 2018 | Removed; composition, catalog, ports, launches, queries, validation, transport, templates and arguments are separate. |
| `live/src/supervision.rs` | 1739 | Removed; lifecycle coordinator plus configuration, clocks, feeds, journal, snapshots and process composition. |
| `mcp/src/store.rs` | 1056 | Removed; one execution lifecycle with conditional storage adapters and segregated authority. |

The replay coordinator is now 792 lines; the supervision coordinator is 607. MCP composition is 115 lines, with 2022 lines across its operation, declaration and discovery modules. Responsibilities moved with their dependencies; runtime MCP handlers do not receive the original broad context.

## Verification

All commands ran from the repository root through the required tools. Python source was unchanged after its successful lint/format/type checks. Rust checks used the workspace manifests and locked dependency graph. No tests were deleted or weakened to obtain these results.

| Check | Measured result |
| --- | --- |
| `uv run --extra dev ruff check .` | Passed. |
| `uv run --extra dev ruff format --check .` | Passed. |
| `uv run --extra dev ty check` | Passed. |
| `uv lock --check` | Passed. |
| Both Rust workspace format checks | Passed. |
| Both Rust workspace Clippy checks, all targets, `-D warnings` | Passed; focused live Clippy also passed after the last allocator correction. |
| Full Python suite, final permitted run | 1,691 passed, one existing live-Cosmos test skipped, five PostgreSQL setup errors; pytest runtime 125.94 s, command wall time 127.81 s. No assertion failures. |
| PostgreSQL corrective run | All five tests passed against an isolated PostgreSQL instance: 5.87 s pytest runtime, 10.58 s command wall time. Server was stopped afterward. No source/test changes were needed. |
| Harness full final run, `--no-fail-fast` | 352 passed, one failed across 105 targets, command wall time 152.58 s. The remaining failure required preserving the existing `job-` plus 32-hex ID form; the allocator now uses UUID randomness while retaining that form. |
| Live corrective run | All five `job_identity` tests passed after that narrow fix; strict live Clippy passed. No third full suite was run. |
| Lake full suite | All 133 cases passed; command wall time 117.97 s includes compilation. Strict workspace Clippy also passed. |
| Mandatory delivery architecture guard | Retained and passed in the final Python run and the separate 77-case artifact/delivery isolation run. |

Final case accounting is **1,696 Python cases and 486 Rust cases verified**, with one pre-existing Cosmos test skipped. This combines the full runs with the explicitly described corrective targets; it is not a claim that the last full commands both exited zero. The Python full execution and its five corrected prerequisites together took 131.81 seconds of reported pytest runtime (138.39 seconds of command wall time), below the three-minute test budget; dependency/server setup is separate.

The first Python attempt hit its 180-second hard deadline during concurrent native compilation. Following the timeout policy, the last active file and mandatory delivery guard were isolated with a 60-second hard timeout: 77 passed in 39.07 seconds. Native compilation finished before the permitted final Python run. The first harness run stopped on an obsolete MCP artifact-URI expectation; that fixture was corrected for the single content-addressed store and its seven-case suite passed. The final harness run then exposed the ID-format regression described above, resolved only through its focused target. Full-run limits were respected.

Docker was initially stopped. Starting it exposed inaccessible stale runtime sockets; automatic approval review blocked the proposed Docker shutdown/socket cleanup and supplied no more specific reason. No factory reset or socket cleanup occurred. A separate temporary PostgreSQL 17 binary instance was obtained through the [publisher’s binary archive page](https://www.enterprisedb.com/download-postgresql-binaries?lang=en), bound only to loopback, and supplied through `THESIS_TEST_POSTGRES_DSN`. The fixture created/dropped its own database. The server was stopped successfully; no application database or system service configuration was changed. Archive SHA-256: `80379b2c04d51c30225532e0ae04509899141e9957ed096fe749d7fd9df8f82f`.

The installed MSVC/Windows SDK library paths were supplied only to verification processes; no machine-wide build settings were changed. Docker images, volumes and application data were retained.

## How successful was the instruction change?

All seven original repair areas now have concrete implementations and an evidence path. This is observable implementation coverage, not proof that the instructions alone prevented defects. During this task, an initial MCP extraction still passed the broad context to split modules; reviewing it against the new ownership rule exposed that weakness, and composition now distributes narrow capability sets. Regression tests also exposed numerical rounding and obsolete-fixture assumptions that comments/import checks could not catch.

The result supports using the instructions as a review/implementation guide alongside executable behavior and architecture checks. It does not establish a future failure-rate reduction: this was one explicitly directed repair, with no untreated comparison or blind reviewer. To measure prevention, record mixed-responsibility, missing-boundary and abandoned-interface findings on future comparable changes, including reviewer corrections and added interface cost. Do not substitute lower line counts or comment presence for that review.

Risks to watch are stale or overbroad responsibility comments, extraction that preserves god-context coupling, interfaces added without current consumers, and forcing different numeric representations into a lossy universal model. The instructions address these risks; continued enforcement still requires review and executable invariants.
