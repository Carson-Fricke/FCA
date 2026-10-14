# thesis/extensions/workflows/hypothesis_development/contracts

Owns the contracts slice of the FCA hypothesis workflow extension. Native domain behavior stays in this extension and uses the runtime through public contracts and scoped activities.

- `__init__.py`: Request, discovery, hypothesis, plan, trial, verdict, candidate, null,
- `candidates.py`: The bounded candidate-evaluation contract for the optimization phase.
- `creation.py`: The preserved idea and trusted launch context; no research-design parameters are required at launch.
- `discovery.py`: Reference-only discovery handoff and investigation history.
- `evidence.py`: The falsification evidence envelope.
- `forward.py`: The forward out-of-sample validation contract for the forward phase.
- `hypothesis.py`: The falsifiable hypothesis and its separate statistical model.
- `nulls.py`: Explicit signal-destruction methods and conditional null evidence.
- `plan.py`: The planned falsification tests and negative controls.
- `rca.py`: One fresh-subagent root-cause diagnosis of a failed trial.
- `strategy.py`: Typed strategy-chain handoffs: the frozen program reference and the backtest.
- `trials.py`: One recorded falsifier trial with bound run evidence.
- `validation.py`: Advertised contract schemas and semantic checks beyond structure.
- `variation.py`: Deterministic marginal and explicitly declared joint-parameter experiments.
- `verdict.py`: The conservative falsification verdict for one hypothesis.
