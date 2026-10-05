---
id: discovery-study-design
title: Discovery study design
version: 1
created: 2026-10-02
retrieved: 2026-10-02
source:
  url: https://github.com/Carson-Fricke/FCA
  publisher: FCA Thesis
  content_hash: eb38af3fd304fa62b6c6a4b1378fbeac87e33cdbb6a9a4934330255ca515e85d
tags:
  - discovery
  - study-design
  - validation
collections:
  - discovery
  - validation
links:
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

Discovery turns a market story into a candidate worth building. It runs
cheap, targeted studies: bars first, trades next, quotes only when needed.

## In brief

- One question per study, written down before touching data.
- Predeclare the window, the measure, the variants, and the stop rule.
- Log every attempt, including failures, and note what changed after seeing it.
- Keep a slice of data untouched for validation.
- Stop when the effect cannot pay its own costs.
- Hand off a named candidate, not a chart that looked good.

## Predeclare the study

Before looking, write down: the question; the data and window; the signal;
the measure; the number of variants; and the stop rule. Every attempt gets a
log line with purpose, data, window, result, and whether anything changed
after seeing it. Redesigning after a peek is how accidental discoveries are
born. See [Exploratory search and multiple attempts](exploratory-search-and-multiple-attempts).

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
