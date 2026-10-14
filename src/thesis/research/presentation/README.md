# Study presentation

This capability profiles immutable study evidence and ranks locally generated
visualization plans. It never submits jobs, computes financial claims, or writes
agent messages. Field explanations come from authors; observed types and shape
are explicitly distinguished from published scientific meaning.

- `__init__.py` exposes `PresentationService`.
- `profiles.py` computes full-output ranges, cardinality, missingness, quantiles,
  value frequencies and histograms, retaining the originating dataset binding.
- `candidates.py` registers deterministic grammar generators and checks complete
  output coordinates before proposing plans. Extend this registry alongside the
  UI's renderer registry for a new visual grammar.
- `decisions.py` calls OpenRouter Decisions with a server-side key and typed
  scores over complete plans. The default is Perplexity Decider V1.1 27B.
- `service.py` balances candidate budgets across outputs, selects diverse views,
  caches by artifact/model/planner identity, and exposes deterministic suggestions
  with an explicit status when the decision provider is unavailable.
