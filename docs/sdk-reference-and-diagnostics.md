# SDK reference and diagnostic contract

The serving SDK source is authoritative. `read_sdk_reference` returns its SDK,
ABI and compiler identity. Packaged templates may be used only when their SDK
identity matches. Source examples are compiled by the SDK doctests and native
authored-program suites; a successful compile alone establishes no trading result.

`thesis.workspaces.sdk` owns source identity, serving reference validation and
template rendering behind the `AuthoringSdk` capability. Application composition
injects it into `WorkspacePrograms` independently of the execution client. The
SDK owner receives only a metadata/source port and local SDK root; program Git
state, submission retention and workflow control remain with their existing
owners. Shared program value and failure rules live in `program_contracts.py`.

## Reading

Document and source reads use `max_tokens`, never a character page size. The
default is 16,384 tokens; the maximum is 65,536 and the minimum is 32. Normal
documents and SDK topics are returned whole. For larger documents, submit the
returned `nextArguments` unchanged until it is null. `complete` means this
response contains the entire selection, not merely its last page.

The fixed read-budget tokenizer is `cl100k_base`, shared by Python tiktoken and
Rust tiktoken-rs. `tokenCount` includes JSON string escaping; transport framing,
reference metadata and provider-specific context accounting are separate.
`tokenOffset` addresses tokens in the original selection, not a retokenized suffix.
Page boundaries preserve UTF-8 exactly. Large reads target three quarters of the
budget so the last page can absorb its tail up to the full limit.

Source line ranges remain available for later targeted inspection. Omit
`end_line` to select the rest of the source; normal reads have no implicit 200-line
or 400-line limit. Program continuation checks its source hash; changed source
must be reread or selected by immutable `job_id`. Research state continuation
similarly rejects changed content; positive handoff IDs select immutable products.
Knowledge references already bind their source snapshot. Job artifacts are immutable.

Containers prefetch tokenizer assets during the build and copy the cache into
the runtime image. Local installations initialize the same verified tiktoken
cache on their first read. There is no character-based fallback tokenizer.

## SDK operations, units and failures

| Surface / reference topic | Contract and diagnostic ownership |
| --- | --- |
| `consumer` / subscriptions | Symbols are exact nonempty, unpadded strings. Bars declare their cadence; quote/trade subscriptions do not. Multiple bar cadences coexist. Dependencies must remain stable, nonzero-sized and acyclic. Tick indicators declare positive output fidelity. SDK routing detects declaration/topology changes. |
| `events` | Prices and quantities are exact decimals. Times are epoch nanoseconds. Bar availability is at completion; missing symbols are explicit. Borrowed event strings live for the callback only. |
| `program` / traits | `new` receives configured JSON; market callbacks belong to `MarketConsumer`. Strategies alone receive an order sink. `on_stop` publishes metrics/outputs, not causal capture. Export macros contain panics and retain their diagnostic; embedded NULs are escaped without discarding the message. |
| `execution` / orders | `place` quantities are positive units, side supplies direction; targets use signed inventory units. Prices use instrument price units, not ticks or bps. Successful submission queues an intent; it establishes neither risk acceptance nor a fill. Inspect later owned order states and engine execution snapshots. Malformed ABI requests fail with the affected operation/field. Ordinary risk rejection is an execution outcome, not a reason to invent inventory. |
| `execution` / accounting | Account return and capital utilization are fractions. Closed-trade return fields explicitly use bps. Missing marks remain undefined. Decimal arithmetic returns checked errors; it never substitutes zero for unavailable accounting. Snapshots are immutable during a callback. |
| `causal` | Capture only in root `on_decision`, after dependency and ordinary bar callbacks. Pass an unprefixed dataset name such as `signal`; the host publishes `causal/signal`. Dataset and feature names contain 1–64 ASCII letters, digits, dot, underscore or hyphen. Supply 1–256 finite features and a positive elapsed nanosecond horizon. Skip warm-up until all features are finite. |
| `causal` / host constraints | Symbol must exist in the active frame. Dataset feature names and horizon stay fixed. At most one capture per dataset/symbol/frame. Horizon addition must fit i64. Outcomes need an exact future availability on that cadence; missing and end-of-run outcomes remain undefined. Host rejection identifies the dataset/symbol/frame and bad field and fails the run even if ignored by author code. |
| `program` / metrics | Names follow `^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}$`; canonical harness metric names and their `_reason` forms are reserved. Values must be finite. Host failures identify the metric name and value. Omit undefined custom metrics or publish an observation with an explicit undefined reason. Ignoring a rejected publish still fails the run. |
| `outputs` | Output names must be unique; `causal/` is host-reserved. Tables have unique nonempty columns and rectangular scalar/null rows. Series contain either finite values without a reason or null with a nonempty reason. Timing must be causal. Diagnostics identify the output and bad row/field. |
| `research_views` | Field names, declared types, dataset/panel references, identity, grain and semantic roles are explicit. Complete-publication validation checks bindings, types, null/duplicate identity and grain, and event/availability order across all rows. Failures retain descriptor/output context and row/field locations where applicable. |
| `research_publishing` | Evidence tables validate rows before insertion; Haar history requires nonempty session/symbol identities and causal timestamps. These helpers publish observed evidence, not reconstructed future information or model significance. |
| `research_recipes` | Recipes bind explicitly named evidence slots and fields; missing slots/mappings are errors. A recipe provides presentation structure, not a statistical test or evidence of a trading advantage. |
| `indicator_api` | Undefined and missing inputs are explicit; no carry-forward is fabricated. Tick cadence is positive nanoseconds. Recording rejects time regression, overlapping intervals and unavailable symbols with their symbol/cadence/boundary values. |
| `wavelet` | `WaveletIndicator` computes trailing Haar features, not MODWT/CWT. Window is a power of two in 2–4096 bars; coarse support is a power of two in 2–window. Warm-up, missing input, flat/undefined arithmetic produce no snapshot. Historical lookup never substitutes a later observation. Constructor errors report actual window/support. |
| `building_blocks` | Periods count observations, not elapsed time. RSI uses simple averaging, not Wilder smoothing; ATR is average true range; VWAP is a rolling price-volume mean. Quantiles use nearest rank. Slope is bps per bar. Directional-change thresholds are absolute price distances. Normalization is frozen training state. Invalid inputs identify the operation and values; unrepresentable outputs and elapsed-time overflow return errors. Failed rolling arithmetic leaves prior state intact. |
| `decimal` | Exact decimal operations use checked arithmetic. Conversion to f64 is explicit and checked at the relevant boundary. The arithmetic context and errors are documented by the serving `num` source; price, quantity, notional, fractions and bps remain distinct units. |

## Agent interfaces and repair

Reference aliases are defined once in the SDK's `reference-topics.json`; every
discovered Rust source is also readable by its `sourceHashes` path. Read the
Thesis `index` topic for available selections. New SDK source files need no tool
schema or adapter registration. The alias manifest participates in SDK identity.

Failed tool outcomes require a validated category and message. Typed domain
failures retain their diagnostics and recovery fields through adapters, HTTP
details and canonical model delivery. A missing output returns selectors computed
from that artifact, while a pending output reports `not_ready` and automatic
completion delivery. Publication contention waits inside the existing fenced
coordinator; a lock deadline reports that publication did not start.

Errors belong to the layer that knows the violated constraint. SDK status codes
are categories; precise host diagnostics belong in the final job failure. Thesis
preserves these diagnostics through ordinary tool results and asynchronous
completion using the canonical conversation inbox. It does not replace mutation
failures with an unexplained generic failure or create a separate diagnostic queue.

- Schema failures identify argument paths, expected constraints and received
  values before dispatch. The error includes the tool name.
- Read errors distinguish invalid bounds/cursors, missing selections, stale
  references and unavailable dependencies. Knowledge request errors are distinct
  from corpus/provider failures; private loader paths are not exposed as content.
- Validation errors preserve structured compiler locations and suggestions.
- A job rejection/failure preserves the retained `job_id`, original submission
  attribution and diagnostic. A transport interruption after possible dispatch
  remains an unknown outcome: reconcile that ID, never silently resubmit.
- Asynchronous completion preserves exact `error_message` and wakes the owning
  conversation. Agents continue useful work or end their response while waiting;
  they do not poll.

## First execution and critique

The research proposal phase owns authoring, validation, execution and durable job
retention. Its prompt directs an initial actual trading-policy execution before
critique; diagnostic-only branches execute their assigned study. It inspects the
completed result and hands off exact program/job evidence, including failures.
A capture-only probe or submission acknowledgement is not a trading result.
Review can then inspect actual behavior, including an honestly reported zero-trade
policy, rather than repeatedly reviewing an unexecuted plan. This is agent
direction and evidence attribution, not an engine-enforced workflow gate.
