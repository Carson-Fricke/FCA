---
id: discovery-versus-validation-data
title: Discovery versus validation data
version: 2
created: 2026-10-02
retrieved: 2026-10-13
source:
  url: https://github.com/Carson-Fricke/FCA
  publisher: FCA Thesis
  content_hash: 77131df906bc77dcbe5eea6751a89310e61b5b0dbc964f3fc33a673d486f0ff3
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

Discovery uses development data to construct strategies, fit parameters,
explore feature interactions and assess training robustness. These activities
are authorized parts of discovery. Sealed forward data later evaluates the
frozen policy on observations that did not guide its construction.

## In brief

- Choose the split before looking at any result.
- Development data supports candidate construction, search and training tests.
- Forward data evaluates a frozen plan, including its declared comparisons.
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

An in-sample robustness test is allowed on development data. Its claim must
match what ran: a fixed selected-policy null is conditional on selection;
search-aware evidence requires a procedure that accounts for that selection.
Neither claim establishes independent forward generalization. See
[Testing a searched candidate against a null](testing-a-searched-candidate-against-a-null).

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
