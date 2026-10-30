# Knowledge retrieval

This capability loads confined shared/workspace Markdown snapshots and returns
data-only references. It owns corpus integrity, search, trust and current-reference
validation, never model instructions, conversation delivery or execution authority.
Thesis tool adapters supply trusted scope/collection constraints. Shared runtime
text reading owns token budgets; corpus references bind the selected snapshot.

- `__init__.py`: public knowledge capability exports.
- `corpus.py`: confined corpus snapshots and page loading.
- `metadata.py`: strict page frontmatter and collection metadata.
- `links.py`: page-link decoding and reference resolution.
- `index.py`: indexed page and relationship records and embedding ports.
- `search.py`: ranking over lexical and semantic index evidence.
- `embeddings.py`: configured local embedding/tokenizer assets and inference.
- `trust.py`: source trust policies and provenance interpretation.
- `ingest.py`: validated corpus ingestion.
- `service.py`: scoped search/current-reference reads, token continuation, and
  model-repairable request errors separated from corpus/provider failures.
- `library/`: authored shared knowledge cards; these remain reference data.
