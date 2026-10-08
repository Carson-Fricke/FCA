# Hypothesis-development prompts

These prompts belong to this workflow only. They do not schedule agents, read
sibling transcripts, perform publication, or enforce operator approval in code.

- `__init__.py` identifies the prompt package.
- `discovery.py` prompts cheap, targeted candidate discovery and the
  operator-confirmed discovery contract using committed, validated program
  references and job IDs.
- `model_creation.py` prompts statistical model creation and revision from the
  selected discovery candidate and explicit surviving evidence.
- `verdict.py` prompts the operator-facing interpretation of recorded trials.
- `strategy_design.py` prompts prose-first design, conversational acceptance,
  committed Rust authoring, latest-ID validation, and reference-based completion.
- `sdk.py` maintains the SDK reference, targeted patch, validation, and repair
  guidance reused by discovery, model creation, and strategy design.

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
