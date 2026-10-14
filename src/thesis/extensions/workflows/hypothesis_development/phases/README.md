# thesis/extensions/workflows/hypothesis_development/phases

Owns the phases slice of the FCA hypothesis workflow extension. Native domain behavior stays in this extension and uses the runtime through public contracts and scoped activities.

- `__init__.py`: Phase vocabulary and phase metadata for hypothesis development.
- `creation.py`: The workflow-start durable trigger text.
- `trials.py`: Falsification-trials phase executor.
