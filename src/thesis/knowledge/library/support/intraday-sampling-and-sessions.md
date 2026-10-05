---
id: intraday-sampling-and-sessions
title: Intraday sampling and sessions
version: 1
created: 2026-10-02
retrieved: 2026-10-02
source:
  url: https://github.com/Carson-Fricke/FCA
  publisher: FCA Thesis
  content_hash: 5ca69b191ba5986eabf806444df30a65eff88c978b85f8a4d3302bbae84d55a2
tags:
  - intraday
  - sessions
  - auctions
  - gaps
collections:
  - data-quality
links:
  - discovery-study-design
  - time-of-day-breakdown
  - trade-prices-versus-midquotes
  - historical-timing-and-leakage
relationships:
  - type: relates
    target: discovery-study-design
  - type: relates
    target: time-of-day-breakdown
  - type: relates
    target: trade-prices-versus-midquotes
  - type: relates
    target: historical-timing-and-leakage
trust:
  tier: curated
  state: published
---
# Intraday sampling and sessions

Intraday data is a sequence of sessions with breaks, auctions, and gaps.
Treating it as one smooth series creates effects that exist only in the
recording.

## In brief

- Build one contiguous, clearly defined series per instrument.
- Decide where the overnight break goes before measuring.
- Treat auctions, halts, and thin minutes explicitly.
- Check for missing intervals instead of filling them silently.

## The overnight break

A return from yesterday's close to today's open is an overnight return. It
is a different object from an intraday return and carries different risk.
Exclude it, include it, or model it separately, but decide before the study
and say which. Splicing across the break without thought creates jumps that
are not trading opportunities.

## Auctions and halts

Opening and closing auctions are not continuous trading. Their prices can be
far from the last continuous quote. Halts create gaps with no trades at all.
Mark these intervals and handle them with rules rather than treating their
prints as normal observations.

## First and last minutes

The first and last minutes of continuous trading often have the widest
spreads and the largest volume. They can dominate averages and create
apparent effects. Analyze them separately, especially for cost estimates.

## Missing intervals and standardization

Count observations per interval before aggregating. Silent gaps produce
returns that span more time than intended. Forward-filling a stale price
creates zero returns that look like calm markets. For time-of-day
comparisons, compare the same clock intervals across days. See
[Time-of-day breakdown](time-of-day-breakdown) and [Trade prices versus
midquotes](trade-prices-versus-midquotes).

## Related

- [Discovery study design](discovery-study-design)
- [Time-of-day breakdown](time-of-day-breakdown)
- [Trade prices versus midquotes](trade-prices-versus-midquotes)
- [Historical timing and leakage](historical-timing-and-leakage)
