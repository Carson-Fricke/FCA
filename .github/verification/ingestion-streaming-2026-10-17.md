# Bounded streaming ingestion and balanced finite workers

Application source: `6440c40221a86c50b4e211f351789ec189ad1be2` (including
`ba3551a`). Raw operator evidence is retained under
`.local/verification/ingestion-streaming-final-2026-10-17/`; the prior measurements
are under `.local/verification/ingestion-two-month-2026-10-17/`.

## Implementation and ownership

- Providers decode bounded JSON pages directly into typed records, preserving
  decimal tokens, using compact strings, and parsing each timestamp once. Page
  reservations stay alive until the consumer releases the records.
- Ingestion projects pages into bounded sorted Arrow runs, merges with bounded
  fan-in, and incrementally writes deterministic Parquet row groups. A complete
  trading session no longer has to fit in the session RAM reservation.
- Storage owns sealed, file-backed staging, streamed hashing/uploads, and atomic
  publication. Local unfinished writes live in a reserved physical namespace;
  they have no logical object address and never appear in object listings.
- The job runtime owns balanced finite worker lanes, concurrent dispatch,
  immutable admission, and retained job identity. Each worker consumes several
  disjoint chunks, reusing connections and credentials before exiting.
- A background permit actor prefetches conservative fleet reservations without
  holding its cache mutex over an RPC or sleep. Final durable metrics separate
  cache waits, authority RPCs, zero-grant retry delays, CAS backoff, and local pacing.
- Local admission retains a budget derived from 75% of available workstation
  memory and divides it among workers. Wire, decoded-page, CPU, session, scratch,
  and publication limits remain independent. The response ceiling is configurable
  and defaults to 4 MiB; actual single-response maxima are recorded.

Local and Azure share these algorithms. Storage, identity, and finite-process/ACA
execution are composition adapters. The main Azure API remains 1 vCPU / 2 GiB;
each finite ACA ingestion execution remains 4 vCPU / 8 GiB. No ACI workers or
HTTP self-calls were added.

## Measurement procedure

Each window includes its final date: November 1–December 31, 2025 (41 sessions)
and June 1–July 31, 2026 (43 sessions). Datasets are MSFT trades, quotes, and
one-minute bars. Only these symbol/date/dataset coverage entries were reset;
immutable objects and unrelated data were retained. Every submission used a
durably recorded fresh ID, with no replay of failed or unknown submissions.

Local and Azure acquisitions run sequentially with the shared Alpaca credentials.
The rolling 10,000-attempt/minute authority remains enforced. End-to-end times
include admission, finite-worker startup, acquisition, publication, and terminal
observation. Two-second status polling imposes an observation floor on short bar
jobs. Stage times overlap and cannot be added to obtain wall time; reported HTTP
bytes are decompressed response bytes, not compressed network traffic.

These are single real-provider runs, not statistically controlled repeated
trials. The workstation is a Ryzen 5900HX with 16 logical CPUs and 15.4 GiB RAM,
also running development applications. The final local admission selected four
workers from a retained 2,425 MiB host budget; the baseline selected two workers under its
old nominal host budget. This comparison measures the complete implementation,
including the new scheduling and admission policy.

## November–December comparison

| Dataset | Rows | Local before | Local after | Local throughput gain |
| --- | ---: | ---: | ---: | ---: |
| Trades | 22,166,607 | 152.391 s | 54.828 s | 2.78× |
| Quotes | 12,171,284 | 80.187 s | 30.625 s | 2.62× |
| Bars | 15,630 | 2.031 s | 2.172 s | 0.94×; observation floor |

All 123 final local partitions match the prior canonical records value for value.
Parquet hashes may differ because incremental encoding changes row-group layout.

## June–July completion, including July 30

| Dataset | Rows | Local end-to-end | Local rows/s |
| --- | ---: | ---: | ---: |
| Trades | 33,826,076 | 71.000 s | 476,424 |
| Quotes | 19,387,308 | 52.391 s | 370,050 |
| Bars | 16,770 | 2.031 s | 8,257 |

The previous trade attempt failed after 240.547 s, publishing 42 of 43 sessions
and 31,762,125 rows. It hit the whole-session normalized-input memory ceiling on
July 30. The new run completes all 43 sessions. July 30 publishes 2,063,951
trades, 948,959 quotes, and 390 bars. The largest local worker peak RSS across
these profiles is approximately 160 MiB.

## Reader checks

The native reader uses a fresh client disk cache and never acquires data in these
checks. On December 3, local cold reads are 108.5 ms trades, 53.6 ms quotes, and
3.9 ms bars; repeat reads are below 0.1 ms. On July 30, cold reads are 391.7 ms,
106.0 ms, and 4.1 ms respectively. The large trade partition exceeds the 128 MiB
decoded per-entry RAM-cache threshold; repeat reads stream from the disk cache
in 263.9–269.8 ms. This preserves the reader's bounded memory behavior.

## Verification and cutover

[Source CI](https://github.com/Carson-Fricke/FCA/actions/runs/39083935654)
passed both complete Rust workspaces: formatting, Clippy, and workspace tests,
plus Bicep validation. The local Python suite passed 1,665 tests with one existing
live-Cosmos skip in 162.78 s. Ruff, ty, the required delivery/architecture guards,
and focused interruption/acknowledgement/duplicate-admission/publication behavior
checks passed. Cold native compilation and one stale worker artifact prevented
the earlier Windows full-workspace attempts from completing cleanly; final
focused checks and the complete Linux CI runs verify the final source.

The first streaming profile exposed a local storage race: adjacent temporary
manifest files were visible in logical listings while another lane discovered
coverage. The fix belongs to storage namespace isolation, with a concurrent
byte/file replacement behavior test; catalog readers retain strict validation.
The failed identity remains terminal, and the final measurements use new IDs.

Before the control-contract cutover, old jobs were audited as terminal and their
executions cleaned. Local obsolete control directories were reversibly archived.
Azure's old scheduled writer and API revision were stopped before archiving and
conditionally deleting five root jobs and their partition records. Market-data
objects, current manifests, contracts, listings, the request ledger, and unrelated
application records were preserved. No compatibility reader or lazy conversion
was introduced.

## Azure rollout and matching profiles

[Main deployment and runtime smoke](https://github.com/Carson-Fricke/FCA/actions/runs/39083935766)
and [Thesis deployment](https://github.com/Carson-Fricke/FCA/actions/runs/39083935792)
passed. Registry retention also passed. The healthy API revision is
`fca-ml-dev-quant-api--0000082`.

- Harness image: `sha256:2406968fb75c2efc2d2ac305df5e314fbce5b7da93edde5d991e98a5c8f21381`.
- Worker/dispatcher image: `sha256:97829dc034be03f757841fddb768774ccf889b488bf0e5cf2afc18bc65e4b8e7`.

The dependency-changing image build took approximately 31 minutes: lake dependency
cook 310 s, harness dependency cook 1,267 s (including bundled DuckDB), and final
harness source build 127 s. The separately published finite image reused the
already-built layers. This is a cold dependency-graph rebuild measurement, not a
claim about source-only cache-hit timing or ingestion startup.

| November–December dataset | Azure before | Azure after | Throughput gain | Final local rows/s | Final Azure rows/s |
| --- | ---: | ---: | ---: | ---: | ---: |
| Trades | 143.640 s | 66.031 s | 2.18× | 404,294 | 335,700 |
| Quotes | 113.078 s | 68.015 s | 1.66× | 397,430 | 178,950 |
| Bars | 39.281 s | 35.282 s | 1.11× | 7,196 | 443 |

| June–July dataset | Local total | Azure total | Local rows/s | Azure rows/s |
| --- | ---: | ---: | ---: | ---: |
| Trades | 71.000 s | 63.657 s | 476,424 | 531,380 |
| Quotes | 52.391 s | 69.922 s | 370,050 | 277,271 |
| Bars | 2.031 s | 41.328 s | 8,257 | 406 |

All six Azure submissions completed. All **252** published daily partitions
across both windows match local status, row count, Parquet size, SHA-256, and
content-addressed path; local file bytes were independently hashed. July 30's
2,063,951 trades, 948,959 quotes, and 390 bars match exactly. Final local and Azure
control audits contain no active or uncleaned profiling execution. The read-only
ARM observer attributed all 48 finite ACA executions.

## Startup, throughput, and remaining limits

An active-fleet span runs from the earliest process start to the latest worker
finish. It includes startup skew between workers; it is not pure processing time.

| Azure dataset/window | First worker startup | Worker start skew | Longest worker | Active-fleet span |
| --- | ---: | ---: | ---: | ---: |
| Nov–Dec trades | 24.8 s | 6.7 s | 37.7 s | 37.9 s |
| Nov–Dec quotes | 16.5 s | 36.6 s | 14.4 s | 48.5 s |
| Nov–Dec bars | 18.6 s | 10.4 s | 4.8 s | 15.2 s |
| Jun–Jul trades | 18.8 s | 20.7 s | 34.0 s | 39.8 s |
| Jun–Jul quotes | 24.5 s | 28.4 s | 18.4 s | 42.0 s |
| Jun–Jul bars | 25.2 s | 7.4 s | 4.5 s | 11.9 s |

- **ACA startup and skew explain much of the remaining Azure disadvantage.**
  For November–December quotes, each worker finishes its work within 14.4 s, but
  worker starts span 36.6 s. Static balanced lanes cannot transfer a late worker's
  chunks to an already-started worker. A future leased shared-chunk queue could
  reduce both startup-tail and uneven-volume tails while preserving immutable
  root admission. That would be a separate job-ownership contract change.
- **Provider latency, page sequencing, and admission still matter.** For the
  larger trade window, the active-fleet decoded HTTP rate is 82.7 MB/s on Azure
  versus 47.5 MB/s locally. Azure averages an equivalent 9,147 attempts/minute
  during that 39.8 s span; this is not a measured rolling-minute maximum. More
  containers cannot bypass the 10,000/minute authority or physical bandwidth.
- **The authority is no longer one serialized network mutex.** Azure authority
  RPC service totals are about 1.3–2.2 s per tick dataset across eight workers.
  The larger trade run records 16.0 s of aggregate zero-grant retry delay and
  410.2 s of overlapping waiter time. A zero grant can also result from the
  backward-clock guard; these counters do not identify that split. Local tick
  datasets record no zero-grant retry delay.
- **Local spill/merge and CPU work remain visible.** The larger local trade
  case uses 229.2 process CPU-seconds versus Azure's 149.5. Arrow projection
  service time is 14.8 s locally and is a modest portion of the work; JSON,
  normalization, sorting, and spill/merge work cost more. Service timers overlap
  and include scheduling effects, so they are not interchangeable with CPU time.
- **Memory remains bounded.** Peak worker RSS is 160 MiB locally and 479 MiB on
  Azure across these runs. The largest HTTP body is 1.12 MiB, below the 4 MiB
  ceiling. Conservative wire admission still creates queueing; reducing safety
  margins further requires evidence beyond these single-symbol profiles.

Azure Monitor's one-minute job network series returned no usable Rx/Tx samples
for these short executions. The measurements therefore do not establish that
either environment reached its physical wire-bandwidth ceiling. No such claim
is inferred from decompressed body counters.

## Azure reads

The same workstation's fresh-cache HTTP reads of Azure take 1.343 s trades,
0.374 s quotes, and 0.185 s bars for December 3; July 30 takes 2.003 s, 0.502 s,
and 0.209 s. These include the workstation-to-Azure path. Comparable baseline
December 3 timings were 1.158 s, 0.336 s, and 0.227 s: read performance is broadly
similar, with no claimed across-the-board speedup.

An additional fresh-cache native probe inside the actual 1-vCPU/2-GiB API
container isolates the shared reader from that external HTTP path:

| Session | Trades cold / repeat | Quotes cold / repeat | Bars cold / repeat |
| --- | ---: | ---: | ---: |
| December 3 | 301 / 89–94 ms | 218 / 44–45 ms | 163 ms / under 0.1 ms |
| July 30 | 651 / 235–237 ms | 260 / 83 ms | 158 ms / under 0.1 ms |

Azure's API deliberately has a 128 MiB total RAM cache and 1 GiB disk cache;
the per-entry RAM threshold is 32 MiB, versus the workstation default's 128 MiB.
These tick partitions therefore stream repeat reads from disk in the API probe.
All three reads of every dataset were checked against committed row counts.

The first manual native exec bypassed the Bicep entrypoint's configured-identity
selector and could not authenticate. Reusing the production selector resolved
the probe without a code change. An early external July 30 probe also preceded
quote/bar publication; it was archived, then rerun after completion with a fresh
cache and explicit coverage/row-count assertions. Neither incomplete probe is
used in the final read timings above.
