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
- `candidates.py` prompts the bounded candidate-evaluation phase: compose at
  most 16 parameter assignments from the committed declaration, submit them
  once through the batch boundary, inspect every run, record states and metrics
  honestly, and select an evidence-based shortlist with its selection rule.
- `nulls.py` prompts the block-permutation null-testing phase: reuse the
  shortlisted candidate's parameters and recorded metrics (or one
  `run_backtest`), choose `block_size` and 1..16 unique seeds explicitly,
  submit one `run_null_batch`, inspect every replicate run, copy metrics
  exactly, and record the observed statistic, direction, exceedance count, and
  p-value computed honestly from the returned metrics.
- `variation.py` prompts the deterministic parameter-variation phase: read the
  committed strategy's declared parameters through `read_strategy`, take the
  best shortlisted candidate from the recorded evaluation as the center (its
  recorded run and metrics, or one `run_backtest`), construct a one-at-a-time
  neighborhood within the declared bounds/levels, submit one
  `run_backtest_batch`, inspect every point run, copy metrics exactly, and
  record the per-parameter stability assessment and honest limitations.
- `forward.py` prompts the protected forward out-of-sample validation phase:
  re-run the same center (`run_backtest`), the same null design
  (`run_null_batch`), and the same variation points (`run_backtest_batch`) on
  the forward window the phase policy rewrites automatically, inspect every
  run, copy metrics exactly, compute the exceedance count and p-value honestly,
  and report degradation versus development rather than retuning on forward
  data.
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
