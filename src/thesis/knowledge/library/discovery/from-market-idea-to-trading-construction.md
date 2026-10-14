---
id: from-market-idea-to-trading-construction
title: From market idea to trading construction
version: 2
created: 2026-10-11
retrieved: 2026-10-13
source:
  url: https://github.com/Carson-Fricke/FCA
  publisher: FCA Thesis
  content_hash: 283529be2b91099c4f6f4b632ef9b62a039e409481cc13895437da4b9102ed8b
tags:
  - discovery
  - strategy-construction
  - feature-composition
  - momentum
  - pivots
collections:
  - discovery
links:
  - investigating-representations-and-episodes
  - developing-a-candidate-through-evidence
  - executable-discovery-tool-bag
  - discovery-study-design
relationships:
  - type: relates
    target: developing-a-candidate-through-evidence
trust:
  tier: curated
  state: published
---
# From market idea to trading construction

Discovery develops a useful training policy, then investigates how much confidence
to place in it. Suggested features supply roles in a composed decision sequence;
they are not a checklist of standalone next-bar forecasts.

For daily momentum, wavelets and intraday regimes, a plausible construction is:
completed daily momentum supplies direction; the managed trailing wavelet snapshot
describes coarse structure; intraday regime selects continuation or reversal setups;
a completed-bar trigger determines entry; structural failure and time determine
exit; ambiguous states or excessive costs suppress entries. This is a hypothesis,
not an MSFT finding or prescribed algorithm.

Implement at least one such composition as a committed, parameterized strategy.
Explore meaningful lookbacks, thresholds, interactions and exits on development
data. Compare after-cost policy outcomes with suitable simpler policies and ablations.
An ablation may omit a feature deliberately; state which incremental question it
answers. A pooled return baseline cannot retire a feature whose role it never used.

See [Constructive parameter and interaction search](constructive-parameter-and-interaction-search)
for the search account and [Testing a searched candidate against a null](testing-a-searched-candidate-against-a-null)
for inference. A profitable training fit is useful but does not prove generalization.
Do not demand significance before building the policy, or promise success afterward.
Keep honest negative findings scoped to actual implementations and resource limits.
