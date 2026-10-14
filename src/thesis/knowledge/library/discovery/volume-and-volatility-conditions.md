---
id: volume-and-volatility-conditions
title: Volume and volatility conditions
version: 2
created: 2026-10-02
retrieved: 2026-10-13
source:
  url: https://github.com/Carson-Fricke/FCA
  publisher: FCA Thesis
  content_hash: b3b3dd1d72bf84fa345983379625c4f6417c92dc8b652e551ac1312d713b8f19
tags:
  - volatility
  - volume
  - conditioning
  - sample-size
collections:
  - discovery
links:
  - discovery-study-design
  - signal-size-buckets
  - stability-across-periods
  - exploratory-search-and-multiple-attempts
  - historical-timing-and-leakage
relationships:
  - type: relates
    target: discovery-study-design
  - type: relates
    target: signal-size-buckets
  - type: relates
    target: stability-across-periods
  - type: relates
    target: exploratory-search-and-multiple-attempts
  - type: relates
    target: historical-timing-and-leakage
trust:
  tier: curated
  state: published
---
# Volume and volatility conditions

Some effects depend on how active or how volatile the market is. Conditioning
can reveal that structure, and it can also manufacture it.

## In brief

- Condition on activity or volatility only when the question asks for it.
- Explore useful conditions and interactions on development data.
- Sample sizes shrink fast; report counts.
- Record conditioning choices and refinements as part of the search.

## Why condition

A reversal may only pay when volume is high. A continuation may only appear
when volatility is low. Those are real possibilities worth testing. Each
extra condition also multiplies the cells being tested, so the search space
grows quickly.

## Choosing the split

Choose an interpretable starting variable and range, then explore thresholds
and interactions as training choices. Terciles on trailing volume or realized
volatility are possible diagnostics, not required strategy settings. Use the trailing value at decision time,
never a value from the future window. See [Historical timing and
leakage](historical-timing-and-leakage).

## Sample sizes shrink

Splitting by two conditions into three groups each leaves nine cells. The
smallest cells may hold too few observations to estimate anything. Report
counts for every cell and leave thin cells out of the conclusion. A stable
story should survive a coarser split too.

## Develop and assess the conditioned policy

An effect found in high volatility can motivate a useful training policy.
Develop that condition in its intended feature interaction and evaluate the
actual policy after costs. Check neighboring thresholds, support, subperiods
and suitable null evidence. Preserve all explored splits; a fixed winner's
null test remains conditional on its selection. Exploratory status permits
development without establishing generalization. See [Exploratory search and multiple
attempts](exploratory-search-and-multiple-attempts) and
[Stability across periods](stability-across-periods).

## Related

- [Discovery study design](discovery-study-design)
- [Signal size buckets](signal-size-buckets)
- [Stability across periods](stability-across-periods)
- [Exploratory search and multiple attempts](exploratory-search-and-multiple-attempts)
- [Historical timing and leakage](historical-timing-and-leakage)
