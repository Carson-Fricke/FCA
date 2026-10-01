# Hypothesis-development prompts

These prompts belong to this workflow only. They do not schedule agents, read
sibling transcripts, perform publication, or enforce operator approval in code.

- `__init__.py` identifies the prompt package.
- `creation.py` prompts statistical model creation and revision from explicit
  surviving evidence.
- `verdict.py` prompts the operator-facing interpretation of recorded trials.
- `strategy_design.py` prompts prose-first design, conversational acceptance,
  pure Rust authoring through `author_strategy`, and receipt-based completion.
