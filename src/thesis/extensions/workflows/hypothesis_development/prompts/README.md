# thesis/extensions/workflows/hypothesis_development/prompts

Owns the prompts slice of the FCA hypothesis workflow extension. Native domain behavior stays in this extension and uses the runtime through public contracts and scoped activities.

- `__init__.py`: Prompt builders for the hypothesis-development phases.
- `candidates.py`: Candidate-evaluation prompts: the bounded optimization phase.
- `discovery.py`: Dataset-window declaration, constructive research loop, concurrent bounded research-investigation delegation and asynchronous triage, training parameter search and robustness, scoped interpretation, operator steering and a pinned prototype handoff.
- `forward.py`: Forward evidence repeats the immutable development design without tuning.
- `model_creation.py`: Model-creation prompts and model-output helpers.
- `nulls.py`: Supported signal-destruction experiments and truthful conditional evidence.
- `sdk.py`: One SDK authoring and repair protocol shared by all authoring phases.
- `strategy_design.py`: Strategy-design prompts: the operator-confirmed strategy proposal.
- `variation.py`: Deterministic marginal and declared joint-parameter experiments.
- `verdict.py`: Verdict-phase prompts: the recorded-trials ask-before-completing protocol.
