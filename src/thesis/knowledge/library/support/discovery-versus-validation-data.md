---
id: discovery-versus-validation-data
title: Discovery versus validation data
version: 1
created: 2026-10-02
retrieved: 2026-10-02
source:
  url: https://github.com/Carson-Fricke/FCA
  publisher: FCA Thesis
  content_hash: 36946220091c18a75c647e7a13105b6aa6bf31a7bca90228a0bb6e0fb7fb40dc
tags:
  - in-sample
  - out-of-sample
  - holdout
  - data-splits
collections:
  - validation
  - discovery
links:
  - discovery-study-design
  - market-adjusted-returns
  - simple-lead-lag
  - stability-across-periods
  - historical-timing-and-leakage
  - exploratory-search-and-multiple-attempts
relationships:
  - type: relates
    target: discovery-study-design
  - type: relates
    target: market-adjusted-returns
  - type: relates
    target: simple-lead-lag
  - type: relates
    target: stability-across-periods
  - type: relates
    target: historical-timing-and-leakage
  - type: relates
    target: exploratory-search-and-multiple-attempts
trust:
  tier: curated
  state: published
---
# Discovery versus validation data

Searching and confirming cannot use the same data without inflating the
result. The split that makes confirmation meaningful has to be chosen before
the search begins.

## In brief

- Choose the split before looking at any result.
- Discovery data is for searching; validation data is for one test.
- Once validation data is seen, it is no longer untouched.
- If the sample is too small to split, say so.

## Choosing the split

Common splits are time (an earlier discovery window and a later validation
window), cross-section (some instruments to search, others to confirm), and
regime (calm periods to search, stressed periods to confirm). The time split
fits most market studies because the later period is genuinely unavailable
at design time.

## Testing the separation

After choosing the split, check that both parts contain the instruments,
sessions, and activity the study needs. Record the split dates in the study
log before running anything. The split is itself a design choice and should
not move afterward.

## Once seen, it is spent

Every peek at validation data uses some of its information. Tuning the
signal, the window, or the threshold after a peek converts validation data
into discovery data. The remaining honest test must then come from newer or
different data. See [Exploratory search and multiple
attempts](exploratory-search-and-multiple-attempts).

## When there is not enough data

Small samples may leave no untouched holdout at all. In that case, run the
study as exploratory only, say clearly that no validation data exists, and
treat any result as a hypothesis. Do not pretend a split exists when both
halves were used to search. See
[Stability across periods](stability-across-periods) and
[Historical timing and leakage](historical-timing-and-leakage).

## Related

- [Discovery study design](discovery-study-design)
- [Market-adjusted returns](market-adjusted-returns)
- [Simple lead-lag](simple-lead-lag)
- [Stability across periods](stability-across-periods)
- [Historical timing and leakage](historical-timing-and-leakage)
- [Exploratory search and multiple attempts](exploratory-search-and-multiple-attempts)
