---
id: exploratory-search-and-multiple-attempts
title: Exploratory search and multiple attempts
version: 1
created: 2026-10-02
retrieved: 2026-10-02
source:
  url: https://github.com/Carson-Fricke/FCA
  publisher: FCA Thesis
  content_hash: dcaa2067bbd2479c459a7272254d78d4fd94b4371a1094ed378db276af37737a
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
trust:
  tier: curated
  state: published
---
# Exploratory search and multiple attempts

The best result from many tries is overstated by construction. This is the
single most common reason a backtest looks better than the live strategy
ever will.

## In brief

- The best result from many tries is overstated by construction.
- Count every attempt, including failed and discarded ones.
- Predeclare the study before running it.
- Exploratory findings need independent confirmation.

## Why the best of many is overstated

If a single test has a 1 in 20 chance of looking good when nothing is there,
then 20 independent tests give about a 2 in 3 chance that at least one looks
good:

\[
1 - (1 - 0.05)^{20} \approx 0.64
\]

The winner of that search is not a discovery. It is the luckiest of the
attempts. The more variants tried, the stronger the winner looks and the
less it means.

## Count attempts

Every bucket, lag, threshold, period, and conditioning choice is an attempt.
The study log should record the total number tried, not only the ones
reported. When the count is unknown, say so. An uncounted search cannot be
corrected later.

## Predeclare before running

Write the question, the measurement, and the variants in advance. This fixes
the number of attempts before the results are known. A small predeclared
study with an honest count is worth more than a large search with a
flattering winner. See [Discovery study design](discovery-study-design).

## What to do with exploration

Exploration is allowed and useful. Label it exploratory, keep the log, and
confirm any candidate on data that was untouched during the search. See
[Discovery versus validation
data](discovery-versus-validation-data), [Signal size
buckets](signal-size-buckets), and [Tradability, break-even, and
out-of-sample](tradability-break-even-and-oos).

## Related

- [Discovery study design](discovery-study-design)
- [Return continuation and reversal](return-continuation-and-reversal)
- [Signal size buckets](signal-size-buckets)
- [Time-of-day breakdown](time-of-day-breakdown)
- [Volume and volatility conditions](volume-and-volatility-conditions)
- [Stability across periods](stability-across-periods)
- [Discovery versus validation data](discovery-versus-validation-data)
- [Tradability, break-even, and out-of-sample](tradability-break-even-and-oos)
