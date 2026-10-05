---
id: time-of-day-breakdown
title: Time-of-day breakdown
version: 1
created: 2026-10-02
retrieved: 2026-10-02
source:
  url: https://github.com/Carson-Fricke/FCA
  publisher: FCA Thesis
  content_hash: 506139287bf732ed767f673d63e951fc4be09e1da3ccbc8e0f16290d52beb84b
tags:
  - intraday
  - time-of-day
  - multiple-comparisons
collections:
  - discovery
links:
  - discovery-study-design
  - intraday-sampling-and-sessions
  - exploratory-search-and-multiple-attempts
  - stability-across-periods
relationships:
  - type: relates
    target: discovery-study-design
  - type: relates
    target: intraday-sampling-and-sessions
  - type: relates
    target: exploratory-search-and-multiple-attempts
  - type: relates
    target: stability-across-periods
trust:
  tier: curated
  state: published
---
# Time-of-day breakdown

Intraday effects often cluster around the open and the close. Splitting the
session into segments can reveal that structure, but it also multiplies the
tests being run.

## In brief

- Split the session into a few segments and define them before running.
- Expect open and close effects; they have known causes.
- More segments mean more chances for a false best hour.
- Require a mechanism before reporting a time effect.

## Define segments first

Use a small set: opening auction, first 30 minutes, midday, last 30 minutes,
and closing auction. Write the boundaries before running the study. Segment
definitions are choices, and changing them after seeing results turns one
study into many. See [Intraday sampling and
sessions](intraday-sampling-and-sessions).

## Open and close effects

The open carries overnight information and auction imbalance. The close
carries index and fund flows. Both can produce real, repeatable patterns.
They can also look real when the measurement is wrong: auctions have no
continuous trading, and the first and last minutes have wide spreads.
Measure them separately from continuous trading.

## Multiple comparisons

With \(m\) segments, one will look best by chance even when nothing is
there. If each segment has a \(5\%\) chance of a false positive under the
null, the chance that at least one looks good is roughly
\(1 - 0.95^m\). Four segments give about one in five studies a false
winner. Count and report all segments.

## Require a mechanism

Before calling a time effect real, name why it should exist: auction flow,
funding, liquidity, or information release. A pattern with no story is a
candidate for the next study, not a finding. Re-check it in a period that
was not used to find it. See
[Stability across periods](stability-across-periods) and [Exploratory
search and multiple attempts](exploratory-search-and-multiple-attempts).

## Related

- [Discovery study design](discovery-study-design)
- [Intraday sampling and sessions](intraday-sampling-and-sessions)
- [Exploratory search and multiple attempts](exploratory-search-and-multiple-attempts)
- [Stability across periods](stability-across-periods)
