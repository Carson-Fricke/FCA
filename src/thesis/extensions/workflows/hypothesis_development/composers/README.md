# thesis/extensions/workflows/hypothesis_development/composers

Owns the composers slice of the FCA hypothesis workflow extension. Native domain behavior stays in this extension and uses the runtime through public contracts and scoped activities.

- `__init__.py`: JSON compose helpers that turn contract payloads into typed drafts.
- `_shared.py`: Shared JSON extraction and text-normalization helpers for composers.
- `hypothesis.py`: Compose helpers for the canonical JSON hypothesis envelope.
