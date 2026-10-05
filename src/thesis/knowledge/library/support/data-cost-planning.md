---
id: data-cost-planning
title: Data cost planning
version: 1
created: 2026-10-02
retrieved: 2026-10-02
source:
  url: https://github.com/Carson-Fricke/FCA
  publisher: FCA Thesis
  content_hash: fa1ea24c616ce5f50ec5fe75ed81851c731b22308760c2e9fce6dc15a61fd3a8
tags:
  - data-costs
  - bars
  - trades
  - quotes
collections:
  - data-quality
  - trading-costs
links:
  - discovery-study-design
  - trade-prices-versus-midquotes
  - cost-and-delay-sensitivity
  - simple-trading-economics
relationships:
  - type: relates
    target: discovery-study-design
  - type: relates
    target: trade-prices-versus-midquotes
  - type: relates
    target: cost-and-delay-sensitivity
  - type: relates
    target: simple-trading-economics
trust:
  tier: curated
  state: published
---
# Data cost planning

Data is not free. Bars, trades, and quotes differ by orders of magnitude in
size and preparation time, and a study only needs some of them.

## In brief

- Bars are cheapest, trades are heavy, quotes are heaviest.
- Declare only the inputs the question needs.
- The first fetch of a cold window includes setup time.
- Start cheap and escalate when the question requires it.

## The price ladder

Bars are small and fast to load, and they answer shape, trend, and
volatility questions. Trades are larger and answer execution and activity
questions. Quotes are the largest and answer spread and short-horizon price
questions. Each step up costs storage, fetch time, and preprocessing.

## Declare only needed inputs

A study plan should list its inputs before any fetch: instrument, window,
frequency, and fields. Extra fields and extra instruments multiply the cost
and the number of accidental comparisons. If an input is not used in the
measurement, leave it out.

## Cold-window fetch time

The first load of a new instrument or a new time window can take much longer
than later loads. Budget for it in the study, and avoid launching many cold
fetches at once. A small warm-up fetch tells you the data is usable before
the full pull.

## Start cheap, escalate when justified

Run the first pass on bars. If the question is about fills, costs, or the
spread, move to trades and quotes. Escalate only after the cheap pass shows
something worth the cost. See [Trade prices versus
midquotes](trade-prices-versus-midquotes), [Simple trading
economics](simple-trading-economics), and [Cost and delay
sensitivity](cost-and-delay-sensitivity).

## Related

- [Discovery study design](discovery-study-design)
- [Trade prices versus midquotes](trade-prices-versus-midquotes)
- [Cost and delay sensitivity](cost-and-delay-sensitivity)
- [Simple trading economics](simple-trading-economics)
