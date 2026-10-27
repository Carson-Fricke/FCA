# DeepSeek nonblank pattern adaptation — 2026-10-23

## Failure and causal evidence

The supplied discovery conversation `chat-381e092cae094ae5aff35ba740a23d60`,
child `subagent-76da2024bbda48d69b71760ae381d421`, made 52 sketch writes
(3 authors and 49 updates). All used `deepseek/deepseek-v4.1-flash-1017`
through Ionstream. Prose fields were single characters; 25 updates failed
because they did not change the sketch. The advertised nonblank pattern was
`\S`, with minimum length 1 and no maximum length.

JSON Schema patterns use substring semantics, so this canonical constraint
accepts a sentence containing nonwhitespace. See the official
[string constraint documentation](https://json-schema.org/understanding-json-schema/reference/string).
Three controlled generation-only requests pinned to Ionstream requested the
same sentence, "Completed daily trends condition intraday pullback entries."

| Provider-facing pattern | Raw returned premise | Generation |
| --- | --- | --- |
| `\S` | `.` | `gen-1792807743-VypQWVTut1GeWgJtgoeO` |
| `^[\s\S]*\S[\s\S]*$` | Exact sentence | `gen-1792807744-UcYo4W9coxk2A1VMY0iR` |
| Omitted | Exact sentence | `gen-1792807746-NUd7vJS2pzr3fxVSGA9j` |

The response was already one character before local argument decoding. This
strongly supports an upstream constrained-generation incompatibility with
substring patterns; client observations do not expose its internal algorithm.
The separate research-read selector fix is not a remedy for this failure.

## Implementation and boundaries

The existing `DeepSeekAdapter` now presents the exact nonblank pattern `\S`
as `^[\s\S]*\S[\s\S]*$`. This preserves the canonical accepted values,
including surrounding whitespace, multiline text and non-Latin text. It visits
schema positions recursively: properties, definitions, arrays, unions and other
schema-valued keywords. It does not rewrite literal defaults, examples, enum
values or constants, and leaves unrelated regexes unchanged.

Canonical domain schemas and dispatch validation remain unchanged. The pure
family adapter owns this provider presentation, with no argument repair, tool
execution, persistence, new delivery path or provider routing policy. The shared
live/replay projection advertises and audits the same transformed schema. Adapter
version 2 identifies this presentation in invocation audits. Other model families
retain their existing schemas. Existing single-character sketch data is not
rewritten; this change enables future generation to preserve intended prose.

## Verification

- The provider/session behavior regression failed before implementation: the
  simulated whole-string generator dispatched a single character instead of
  the requested sentence. Both single- and multiple-branch cases pass after
  adaptation, including durable audit/replay and original union constraints.
- Generated-schema tests verify validation equivalence, whole-string generation
  across nested schema positions, whitespace rejection, multiline/Unicode text,
  literal-data preservation, unchanged unrelated regexes, idempotence and no
  canonical schema mutation.
- Focused provider suite: **102 passed**. Focused family tests plus the mandatory
  delivery architecture guard: **76 passed**.
- Live generation-only requests used the actual `author_strategy_sketch`
  descriptor through adapter version 2. Ionstream, Wafer and InferenceNet all
  returned the exact requested premise and sketches valid against both schemas:
  `gen-1792808688-lEkpGW8a5dfH2vvMundJ`,
  `gen-1792808688-Zzn6oTDM2jxGDPEXrTIb`,
  `gen-1792808688-ZdNQyD8y5jdelLrMPyEN`, respectively.
- Wafer and InferenceNet copied the supplied nested payload exactly. The first
  Ionstream response failed strict nested equality despite the exact premise;
  that response body was not retained, so the differing field is unproven.
  A bounded follow-up compared supplied values recursively, allowing additional
  optional fields, and matched every value with both schemas valid:
  `gen-1792808714-5MmcvxTrLPSGNmi2MBbZ`. These small probes demonstrate the
  correction for the observed generation failure, not universal model reliability.
- No generated business tool call was dispatched during qualification.
- Full frozen integration: `uv lock --check`, `uv run --extra dev ruff check .`,
  `uv run --extra dev ruff format --check .` and
  `uv run --extra dev ty check` all passed.
- `uv run --extra dev pytest -p no:cacheprovider --durations=10`:
  **1,708 passed, 1 opt-in live Cosmos test skipped, 158.69 seconds**. This
  includes the mandatory delivery architecture guard and the pending research
  read selector changes. No second full integration run was needed.
- Git whitespace and repository LF checks passed.
- The local Thesis API was restarted after verification. Health, authenticated
  access to the supplied conversation and the existing UI returned HTTP 200.

The evaluation runbook now includes actual sketch-schema prose qualification.
Scripted tool calls alone cannot establish production model/server behavior;
generation can be schema-valid while destroying the requested content.
