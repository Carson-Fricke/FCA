---
id: constructive-parameter-and-interaction-search
title: Constructive parameter and interaction search
version: 1
created: 2026-10-13
retrieved: 2026-10-13
source:
  url: https://github.com/Carson-Fricke/FCA
  publisher: FCA Thesis
  content_hash: 2165e4365f95956b26933779f9edfcee347bd3c06a590c4951217ecfb6cc7ddd
tags:
  - discovery
  - strategy-construction
  - multiple-testing
collections:
  - discovery
  - validation
links:
  - discovery-study-design
  - exploratory-search-and-multiple-attempts
relationships:
  - type: relates
    target: discovery-study-design
trust:
  tier: curated
  state: published
---
# Constructive parameter and interaction search

Build the requested policy first, then use development data to learn which settings
make it useful. Keep the objective and resource budget explicit, and the agent free
to refine choices with evidence. No fixed optimizer or mandatory search grid is implied.

Example, not market evidence: daily momentum lookbacks 5/10/20 sessions, managed
wavelet windows 16/32/64 completed bars, coarse-share thresholds, intraday regime
thresholds, reclaim/breakout triggers and 15/30/60-minute exits are meaningful choices.
Explore sensible combinations in stages when the full product is too large.
An interaction can matter even when each pooled feature forecast is weak. A filter
can be useful by avoiding bad trades without predicting the opposite sign.

Author a strategy with declared tunables, validate it and submit assignments via
run_backtest_batch. The harness evaluates actual entries/exits and costs. One point
is one retained job. Inspect all results; refine promising neighborhoods, compare
policy ablations and retain adverse variants. Use targeted studies to check why a
detector or outcome behaves unexpectedly, not to replace strategy evaluation.
Choose after-cost objectives alongside feasible turnover, risk and independent
session/episode support. Do not optimize profit at any cost or force a winner.

Allocate executionBudget across candidates, diagnostics, robustness/null replicates
and technical repairs. Report resource use separately from scientific alternatives.
Compilation and publication repair cannot demonstrate substantive exploration.
Inspect neighboring settings, costs, subperiods and concentration after selection.
Store exact selected strategy/parameters, candidate job and search evidence in
sketch.prototype; later phases start with that tested construction.

If none is useful, explain which policies/settings were explored, what failed and
what remains untested. Finite exploration does not refute the entire feature family.
