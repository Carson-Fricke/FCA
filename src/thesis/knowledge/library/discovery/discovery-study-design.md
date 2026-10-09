---
id: discovery-study-design
title: Discovery study design
version: 2
created: 2026-10-02
retrieved: 2026-10-02
source:
  url: https://github.com/Carson-Fricke/FCA
  publisher: FCA Thesis
  content_hash: 67217c5ace00ff0993da6a4d5292d0c6142cb4ed2086fd1171dc46efd947962e
tags:
  - discovery
  - study-design
  - validation
collections:
  - discovery
  - validation
links:
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

Discovery turns a market story into an evolving strategy sketch. Describe the
mechanism, representation, conditioning, entry/exit/abstention, and assumptions.
Reach for the [Executable discovery tool bag](executable-discovery-tool-bag)
before letting existing studies define the search. Compose or author indicators,
event detectors, shape features, and scale-specific representations. Inspect
their sequences and events; targeted studies support revision of the sketch.
Choose bars, trades, or quotes according to the mechanism and information needed.

## In brief

- Give each investigation a purpose tied to a sketch assumption or design choice.
- Predeclare the window, the measure, the variants, and the stop rule.
- Log every attempt, including failures, and note what changed after seeing it.
- Keep a slice of data untouched for validation.
- Stop when the effect cannot pay its own costs.
- Hand off a named candidate, not a chart that looked good.

## Predeclare the study

Before looking, write down: the question; the data and window; the signal;
the measure; the number of variants; and the stop rule. Every attempt gets a
log line with purpose, data, window, result, and whether anything changed
after seeing it. Discovery may revise the sketch after observing development
evidence. Record those revisions and all alternatives tried; they are exploratory
search, not independent confirmatory evidence. See
[Exploratory search and multiple attempts](exploratory-search-and-multiple-attempts).

## Keep validation data untouched

Split the calendar before the study starts: a discovery window and a
validation window. Validation data is opened once, near the end. See
[Discovery versus validation data](discovery-versus-validation-data).

## Stop conditions

Stop when the question is answered, the effect is below the round-trip
cost, the effect has no mechanism outside one slice, or the data budget is
spent. Stopping is a result; record it.

## Hand off a candidate

The handoff names: opportunity; signal; information time; direction;
holding period; profit source; effect with uncertainty; competing
explanation; and evidence. Those fields make the candidate testable.

## Interesting is not tradeable

Interesting means larger than noise with a mechanism. Tradeable means it
survives spread, fees, slippage, and execution delay. Most findings die at
the cost stage; record that outcome. See [Cost and delay
sensitivity](cost-and-delay-sensitivity) and [Simple trading economics](simple-trading-economics).
