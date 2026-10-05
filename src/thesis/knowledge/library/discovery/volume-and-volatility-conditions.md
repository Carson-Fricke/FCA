---
id: volume-and-volatility-conditions
title: Volume and volatility conditions
version: 1
created: 2026-10-02
retrieved: 2026-10-02
source:
  url: https://github.com/Carson-Fricke/FCA
  publisher: FCA Thesis
  content_hash: b1e82134879c2f3944c24ed2d14e91c1f03647c8705f40061d2c232e9532e83c
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
- Split into a few predeclared groups.
- Sample sizes shrink fast; report counts.
- Conditioning after the fact is a hidden search.

## Why condition

A reversal may only pay when volume is high. A continuation may only appear
when volatility is low. Those are real possibilities worth testing. Each
extra condition also multiplies the cells being tested, so the search space
grows quickly.

## Choosing the split

Decide the variable and the edges first. Terciles on trailing volume or
realized volatility are common. Use the trailing value at decision time,
never a value from the future window. See [Historical timing and
leakage](historical-timing-and-leakage).

## Sample sizes shrink

Splitting by two conditions into three groups each leaves nine cells. The
smallest cells may hold too few observations to estimate anything. Report
counts for every cell and leave thin cells out of the conclusion. A stable
story should survive a coarser split too.

## Avoid post-hoc conditioning

Did the effect appear only in high volatility? That was one of many possible
splits. Unless it was predeclared or has a mechanism, treat it as
exploratory. See [Exploratory search and multiple
attempts](exploratory-search-and-multiple-attempts) and
[Stability across periods](stability-across-periods).

## Related

- [Discovery study design](discovery-study-design)
- [Signal size buckets](signal-size-buckets)
- [Stability across periods](stability-across-periods)
- [Exploratory search and multiple attempts](exploratory-search-and-multiple-attempts)
- [Historical timing and leakage](historical-timing-and-leakage)
