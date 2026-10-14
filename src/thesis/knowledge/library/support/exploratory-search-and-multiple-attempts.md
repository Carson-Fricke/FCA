---
id: exploratory-search-and-multiple-attempts
title: Exploratory search and multiple attempts
version: 2
created: 2026-10-02
retrieved: 2026-10-13
source:
  url: https://github.com/Carson-Fricke/FCA
  publisher: FCA Thesis
  content_hash: b8818e41447292474da2b032bd7971ad114a63d4c0c5925b537ef27d18708bfc
tags:
  - multiple-testing
  - data-snooping
  - attempts-log
  - predeclaration
collections:
  - validation
  - discovery
links:
  - discovery-study-design
  - return-continuation-and-reversal
  - signal-size-buckets
  - time-of-day-breakdown
  - volume-and-volatility-conditions
  - stability-across-periods
  - discovery-versus-validation-data
  - tradability-break-even-and-oos
  - directional-change-events
  - perceptually-important-points
relationships:
  - type: relates
    target: discovery-study-design
  - type: relates
    target: return-continuation-and-reversal
  - type: relates
    target: signal-size-buckets
  - type: relates
    target: time-of-day-breakdown
  - type: relates
    target: volume-and-volatility-conditions
  - type: relates
    target: stability-across-periods
  - type: relates
    target: discovery-versus-validation-data
  - type: relates
    target: tradability-break-even-and-oos
  - type: relates
    target: directional-change-events
  - type: relates
    target: perceptually-important-points
trust:
  tier: curated
  state: published
---
# Exploratory search and multiple attempts

Parameter search belongs in discovery. The selected training result is optimistic
because selection uses the same outcomes; this calls for transparent inference and
later validation, not a prohibition on fitting or a requirement that defaults work.

Retain every execution ID before dispatch: candidates, ablations, null replicates,
failures and technical repairs. In the scientific account also record design choices,
parameter vectors, objectives, ranges, refinements and discarded variants. One
batch is many candidate trials. One compiler/output repair uses resources but is not
a new scientific hypothesis unless its policy changes.

A winner's fixed-policy permutation asks about that fixed rule, conditional on
selection. It does not measure how often searching the same family produces a winner
under the null. A search-aware test repeats the entire stated search/selection
procedure per null, including adaptive decisions if the claim includes them.
Neither running many null seeds on one winner nor correcting only the final few
reported variants reconstructs the full search.

Training robustness, independent generalization and formal validation are different
claims. Report what ran and what remains unresolved. Do not present a small searched
p-value as proof, or a non-significant default as feature refutation.
See [Testing a searched candidate against a null](testing-a-searched-candidate-against-a-null).
