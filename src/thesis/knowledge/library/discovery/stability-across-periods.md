---
id: stability-across-periods
title: Stability across periods
version: 1
created: 2026-10-02
retrieved: 2026-10-02
source:
  url: https://github.com/Carson-Fricke/FCA
  publisher: FCA Thesis
  content_hash: a6f4942af682154cdfda0be7ceb18cda65f5b9bb4ae9b4c4fb4503bae306dc89
tags:
  - stability
  - subperiods
  - regimes
  - walk-forward
collections:
  - discovery
  - validation
links:
  - discovery-study-design
  - return-continuation-and-reversal
  - volume-and-volatility-conditions
  - time-of-day-breakdown
  - exploratory-search-and-multiple-attempts
  - discovery-versus-validation-data
relationships:
  - type: relates
    target: discovery-study-design
  - type: relates
    target: return-continuation-and-reversal
  - type: relates
    target: volume-and-volatility-conditions
  - type: relates
    target: time-of-day-breakdown
  - type: relates
    target: exploratory-search-and-multiple-attempts
  - type: relates
    target: discovery-versus-validation-data
trust:
  tier: curated
  state: published
---
# Stability across periods

An effect that appears in a single sample may be a property of that sample.
Splitting the sample shows whether the effect survives different conditions.

## In brief

- Split the sample by month, quarter, or volatility regime.
- Report every period, including the ones where the effect fails.
- Do not choose the best period after seeing all of them.
- A result that works in one period only is a candidate for more study.

## Why split

Splitting is a check on fragility. It also multiplies the number of tests,
because each period is another chance for noise to look like signal. More
periods mean a higher chance that one of them looks good by luck.

## Useful splits

Calendar splits by month or quarter are simple and predeclarable.
Volatility-regime splits separate calm and stressed markets; see
[Volume and volatility conditions](volume-and-volatility-conditions). A
time-of-day split belongs to a separate study.

## Report where it fails

A table of all periods is more honest than a sentence about the average.
Mark the periods where the effect is absent or reverses. A pattern that
flips sign across periods is not stable, whatever the pooled estimate says.

## Avoid selecting the best period

Picking the best month after seeing the table is selection, and the selected
month will look better than the effect really is. If a period stands out,
treat it as exploratory and test it on untouched data. See [Discovery
versus validation data](discovery-versus-validation-data) and [Exploratory
search and multiple attempts](exploratory-search-and-multiple-attempts).

## Related

- [Discovery study design](discovery-study-design)
- [Return continuation and reversal](return-continuation-and-reversal)
- [Volume and volatility conditions](volume-and-volatility-conditions)
- [Time-of-day breakdown](time-of-day-breakdown)
- [Exploratory search and multiple attempts](exploratory-search-and-multiple-attempts)
- [Discovery versus validation data](discovery-versus-validation-data)
