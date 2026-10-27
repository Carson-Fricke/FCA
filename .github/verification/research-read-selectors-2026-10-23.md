# Research read selector investigation — 2026-10-23

## Observed failure

The supplied local conversation's proposal phase
`subagent-63a2f812518749d5b092be0561c65606` made 25 research reads in the
inspected history. One selected the brief successfully. The other 24 supplied
only `execution_id`; all returned offset zero of a 128,734-character accepted
state. The intended handoff/page was never selected. The persisted provider
request advertised all four parameters: `execution_id`, `handoff`, `offset`
and `max_chars`. None was required in the original schema.

The audit establishes the advertised schema and canonical emitted requests. It
is not a raw response capture, so it alone cannot prove where fields disappeared.

## Wire-level and controlled model evidence

Fresh bounded read-only probes captured OpenRouter SSE bytes before local
argument parsing. A minimal request explicitly naming all four values still
returned only `execution_id`. Concatenating the raw tool argument fragments and
passing the frames through the production parser produced identical arguments.
Nonstreamed responses reproduced the omission as well.

The original successful historical call used Wafer, while the repeated failures
used InferenceNet. Controlled requests reproduced the omission through both,
so the evidence does not establish an InferenceNet-only problem. Routing for
the comparison followed the official
[OpenRouter provider selection contract](https://openrouter.ai/docs/guides/routing/provider-selection),
with the actual endpoint and generation IDs recorded in temporary local evidence.

Differential checks with the same model (`deepseek/deepseek-v4.1-flash-1017`):

- The original optional-field schema omitted the requested numeric selectors in
  six minimal streamed requests across the two upstream providers.
- A clearer description with the fields still optional also reproduced the omission.
- A plain JSON answer contained all four requested values, while a tool call with
  the original schema did not. The requested values were present in the input.
- Requiring the four fields produced all four values. Explicit nullable branch
  and handoff selectors also worked, preserving their intended null values.
- With the implemented contract, six focused phase-read cases passed exactly:
  brief selection, a specific handoff/page, and copying continuation arguments,
  each through InferenceNet and Wafer.

This locates a reproducible failure upstream of local dispatch, in tool-argument
generation under the optional schema. Client-side captures cannot distinguish
model behavior from an upstream server's native tool conversion. No local
argument-dropping defect was found.

An ancillary Wafer probe included all fields but altered a long branch ID.
Completeness is not proof of correct values or universal model reliability.
Owning phases use an explicit null branch selector; discovery still needs an
exact branch ID, and branch authorization continues to reject unrelated IDs.
There is no automatic provider switch or mutation replay in this change.

## Current implementation

The tool now requires all four arguments. `execution_id=null` selects the
phase's own branch or discovery's branch list; `handoff=null` selects accepted
state/branch inspection, zero selects the original brief, and a positive index
selects an exact accepted handoff. Offset and page size are explicit. Missing
selectors fail ordinary argument validation instead of becoming a successful
implicit page-zero read.

Paged responses include `nextArguments` with the complete selector, page size
and next offset, or null at the end. Reads remain stateless; repeating a call
does not advance a hidden cursor. The descriptor, research phase prompts,
discovery prompt, package READMEs and architecture document use this contract.
There is no old-schema acceptance branch or inference from planning prose.
Persisted workflow products and version 3.0.0 remain unchanged.

## Verification

- `uv lock --check`: passed.
- `uv run --extra dev ruff check .`: passed.
- `uv run --extra dev ruff format --check .`: passed.
- `uv run --extra dev ty check`: passed after correcting types in the new test.
- `uv run --extra dev pytest -p no:cacheprovider --durations=10`:
  **1,707 passed, 1 opt-in live Cosmos test skipped, 145.83 seconds**.
  The mandatory delivery architecture guard is included.
- The research behavior suite covers required-selector feedback, exact handoff
  content, reconstructed pagination via returned arguments, discovery and phase
  access, immutable products, branch isolation and causal sibling restrictions.
- A new full provider/session/research-handler behavior test splits tool arguments
  across SSE frames after `execution_id`. Handoff, offset and page size reach the
  real handler and durable canonical receipt unchanged. The handler returns the
  exact selected slice of the accepted handoff; the result reenters provider context.
- Against the affected branch's real PostgreSQL data, read-only verification using
  its current active phase read the original brief (6,375 characters), all seven
  pages of handoff 5 (54,726 characters), and all twenty pages of accepted state
  (156,997 characters, including 28 issue records). The historical proposal phase
  had completed by this verification; it was not treated as an active caller.
- LF and Git whitespace checks: passed.
- Local Thesis API restarted with the revised code. API health, authenticated
  access and the existing UI each returned HTTP 200 afterward.

## Why this escaped existing coverage

The scripted providers supplied the correct selectors themselves. Those tests
proved retrieval and scope once arguments were correct, but could not establish
that the production model/server would emit them. Optional mode selectors and
default offset zero then made incomplete calls appear successful. Large accepted
states made repeatedly receiving that first page costly and unproductive.

The evaluation runbook now includes model/upstream qualification for selectors,
exact branch IDs and continuation, plus comparison of raw response fragments,
canonical requests and dispatched receipts. This complements offline behavior
tests; prompt text and green scripted tests are not evidence of model compliance.
