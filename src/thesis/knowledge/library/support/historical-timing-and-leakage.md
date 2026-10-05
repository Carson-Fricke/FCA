---
id: historical-timing-and-leakage
title: Historical timing and leakage
version: 1
created: 2026-10-02
retrieved: 2026-10-02
source:
  url: https://github.com/Carson-Fricke/FCA
  publisher: FCA Thesis
  content_hash: 37ed10a604e18b5c89cb64234d1369ca51caf0019166d8c764f1a6761e21757f
tags:
  - point-in-time
  - availability
  - restatement
  - lookahead
collections:
  - data-quality
links:
  - discovery-study-design
  - volume-and-volatility-conditions
  - simple-lead-lag
  - intraday-sampling-and-sessions
  - discovery-versus-validation-data
relationships:
  - type: relates
    target: discovery-study-design
  - type: relates
    target: volume-and-volatility-conditions
  - type: relates
    target: simple-lead-lag
  - type: relates
    target: intraday-sampling-and-sessions
  - type: relates
    target: discovery-versus-validation-data
trust:
  tier: curated
  state: published
---
# Historical timing and leakage

Most spectacular backtest results come from using information earlier than a
real trader could have known it. Timing is a data property, not an
afterthought.

## In brief

- Every row needs a time when it became knowable.
- Use availability time \(t_{avail}\), not event time \(t_{event}\).
- A decision at time \(t\) may only use data with \(t_{avail} \le t\).
- Restated data is a common hidden leak.

## Event time versus availability time

Event time is when something happened. Availability time is when the data
record became usable to a decision maker. A closing price has an event time
of 16:00 but may be available only after the close is published.
Fundamentals have an event quarter and a much later filing date. Joins must
use availability time.

## Restatements and as-of data

Many datasets show today's version of history, not the version that existed
at the time. Earnings are restated, index membership changes, and delisted
instruments disappear. An as-of or point-in-time dataset records what was
known on each date. If the data is not point-in-time, say so in the study
log and treat timing claims as weak.

## Lookahead examples

- Trading at today's open using today's closing price.
- Using full-day volume at noon.
- Using a signal published after the close to trade that same close.
- Using revised fundamentals to explain an old return.

## A cheap checklist

For every input, ask when the value was first knowable, whether that time
precedes the decision, and whether the dataset restates history. Write the
answers into the study log. See [Simple lead-lag](simple-lead-lag),
[Intraday sampling and sessions](intraday-sampling-and-sessions), and
[Discovery versus validation data](discovery-versus-validation-data).

## Related

- [Discovery study design](discovery-study-design)
- [Volume and volatility conditions](volume-and-volatility-conditions)
- [Simple lead-lag](simple-lead-lag)
- [Intraday sampling and sessions](intraday-sampling-and-sessions)
- [Discovery versus validation data](discovery-versus-validation-data)
