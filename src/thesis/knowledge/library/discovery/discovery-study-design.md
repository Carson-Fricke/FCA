---
id: discovery-study-design
title: Discovery study design
version: 4
created: 2026-10-02
retrieved: 2026-10-13
source:
  url: https://github.com/Carson-Fricke/FCA
  publisher: FCA Thesis
  content_hash: ac268ba19d2f3d45a2076ba39110c04ec26a97abb3d04fcc80d40beab949c8bb
tags:
  - discovery
  - study-design
  - validation
collections:
  - discovery
  - validation
links:
  - from-market-idea-to-trading-construction
  - investigating-representations-and-episodes
  - developing-a-candidate-through-evidence
  - executable-discovery-tool-bag
  - return-continuation-and-reversal
  - signal-size-buckets
  - time-of-day-breakdown
  - trade-prices-versus-midquotes
  - market-adjusted-returns
  - volume-and-volatility-conditions
  - simple-lead-lag
  - cost-and-delay-sensitivity
  - stability-across-periods
  - historical-timing-and-leakage
  - intraday-sampling-and-sessions
  - microstructure-false-positives
  - exploratory-search-and-multiple-attempts
  - discovery-versus-validation-data
  - simple-trading-economics
  - data-cost-planning
relationships:
  - type: relates
    target: return-continuation-and-reversal
  - type: relates
    target: signal-size-buckets
  - type: relates
    target: time-of-day-breakdown
  - type: relates
    target: trade-prices-versus-midquotes
  - type: relates
    target: market-adjusted-returns
  - type: relates
    target: volume-and-volatility-conditions
  - type: relates
    target: simple-lead-lag
  - type: relates
    target: cost-and-delay-sensitivity
  - type: relates
    target: stability-across-periods
  - type: relates
    target: historical-timing-and-leakage
  - type: relates
    target: intraday-sampling-and-sessions
  - type: relates
    target: microstructure-false-positives
  - type: relates
    target: exploratory-search-and-multiple-attempts
  - type: relates
    target: discovery-versus-validation-data
  - type: relates
    target: simple-trading-economics
  - type: relates
    target: data-cost-planning
trust:
  tier: curated
  state: published
---
# Discovery study design

Choose an experiment for the decision it informs. Discovery is constructive training
research: it may fit parameters and explore interactions. Formal preregistration and
sealed forward validation occur later.

Before a search batch, state the composed policy, objective, feasible support,
information timing, fill/cost model, parameter ranges and bounded resource allocation.
This records what the batch means; it does not freeze the entire discovery phase.
Revise based on evidence and operator steering, preserving all earlier choices.

Diagnostics assess a representation, episode or calculation. Backtests evaluate
executable entries, exits, risk and abstention after costs. A diagnostic result must
not be presented as testing an omitted strategy. Compare conditioned policies with
appropriate controls and opportunity sets; condition-on-condition means and a policy
ablation answer different questions. Different holding horizons and occurrence
weightings define different policies, not simultaneous significance hurdles.

Verify feature availability at the original decision. Wavelet features use managed
SDK snapshots, not hand-written transforms or later values attached to old setups.
Preserve sessions/episodes and overlapping comparison arms in uncertainty calculations.
Check long/short signs, denominators, per-side costs, fills and feasible independent
support. No arbitrary sample floor should exceed the available eligible population.

After finding a useful candidate, examine neighbors, cost stress, concentration and
null evidence. See [Constructive parameter and interaction search](constructive-parameter-and-interaction-search)
and [Testing a searched candidate against a null](testing-a-searched-candidate-against-a-null).
Keep all attempts and scope inference to the actual search and comparison. An
inconclusive result is uncertainty; a resource limit is a resource limit.
