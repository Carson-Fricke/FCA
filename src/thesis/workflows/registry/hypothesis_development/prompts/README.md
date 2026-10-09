# Hypothesis-development prompts

These prompts belong to this workflow only. They do not schedule agents, read
sibling transcripts, perform publication, or enforce operator approval in code.

- `__init__.py` identifies the prompt package.
- `discovery.py` prompts candidate-thesis discovery — composable building
  blocks, exploratory studies, honest no-candidate conclusions — and the
  operator-confirmed discovery contract using committed, validated program
  references and job IDs.
- `model_creation.py` prompts statistical model creation and revision from the
  selected discovery candidate and explicit surviving evidence, including
  falsifier roles (claim, assumption, control).
- `verdict.py` prompts the operator-facing interpretation of the recorded
  research evidence envelope (assumptions, controls, trials, run states).
- `strategy_design.py` prompts prose-first design, conversational acceptance,
  committed Rust authoring, latest-ID validation, and reference-based
  completion, separating trading exits from assumption monitors.
- `candidates.py` prompts declared finite parameter searches without a universal
  point ceiling; authoritative job results support the shortlist.
- `nulls.py` prompts ordinary or block permutation with declared signal
  dependency and preserved properties. Full frozen seeds submit in bounded
  chunks; target precision determines replicate count. Failed replicates count
  conservatively, and significance is explicitly conditional on selection.
- `variation.py` prompts deterministic marginal or declared joint experiments.
  All vectors, methods and interaction groups freeze before forward exposure.
- `forward.py` repeats the complete frozen design on protected forward data,
  compares matched evidence and identifies adaptive holdout reuse.
- `sdk.py` maintains the SDK reference, targeted patch, validation, and repair
  guidance reused by discovery, model creation, and strategy design, and states
  that execution is phase-scoped: discovery may run studies only; backtests
  belong to the strategy phases.

Authoring phases read version-matched SDK signatures/templates before writing or
repairing Rust, author committed programs, apply exact version-checked patches,
and validate the latest ID. Completion carries accepted artifact references,
not source. The shared guidance asks through `request_workflow_input` after three
unsuccessful compiler repairs; this is prompt guidance, not a runtime-enforced
repair cap. Deterministic completion checks verify stored content/validation and
do not compile, rewrite, or repair programs.

Study/strategy/indicator templates are shared through `workspaces/programs.py`;
prompts consume them through `read_sdk_reference`, not copied Rust snippets.
Templates establish minimal SDK contracts. Agents must add the actual research
logic, outputs, and controls before submitting a substantive model completion.
