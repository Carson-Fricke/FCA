# Scientific research records

This package holds immutable research identities and pure scientific-contract
logic. Workflow phases and workspace operations consume it; it does not own
harness execution, provider turns or operator interaction.

| File | Purpose |
| --- | --- |
| `__init__.py` | Public research records and validation operations. |
| `evidence.py` | Shared scalar parameter identities, accepted-document hashes and numeric views of exact harness metrics. |
| `grammar.py` | Research-grammar parsing and normalization. |
| `hypothesis.py` | Immutable hypothesis artifacts and validation. |
| `models.py` | Research model references and metadata. |

Candidate selection, null testing, parameter variation and forward validation
share the parameter and metric primitives in `evidence.py`. Parameter vectors
retain JSON types, so integer and float assignments remain distinct. Harness
decimal strings stay exact in evidence; finite numeric views serve statistical
calculations. Textual undefined reasons remain source evidence rather than zero.
