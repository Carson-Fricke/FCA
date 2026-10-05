---
id: intraday-seasonality
title: Intraday seasonality
version: 1
created: 2026-10-02
retrieved: 2026-10-02
source:
  url: https://github.com/Carson-Fricke/FCA
  publisher: FCA Thesis
  content_hash: c7d9dfe95aefc882425b737a6e256f7e5f5c6c1b2cdbc10d9177179189f31ca0
tags:
  - intraday-seasonality
  - time-of-day
  - session-hygiene
  - multiple-testing
collections:
  - model-cards
links:
  - model-selection-by-hypothesis-shape
  - realized-volatility-and-vol-forecasting
  - predictive-regression-and-lead-lag
  - overfitting-and-selection-discipline
relationships:
  - type: relates
    target: model-selection-by-hypothesis-shape
  - type: relates
    target: realized-volatility-and-vol-forecasting
  - type: relates
    target: predictive-regression-and-lead-lag
  - type: relates
    target: overfitting-and-selection-discipline
trust:
  tier: curated
  state: published
---
# Intraday seasonality

The claim is that a move repeats at the same time of day: an open effect, a
lunch lull, a close ramp. The test must separate time-of-day mean structure
from the volatility U-shape and session artefacts.

## In brief

- What it tests: whether mean return or a signal differs across declared
  time-of-day bins.
- Intake: intraday bars with correct timezone, clean sessions, and
  auctions and halts handled explicitly.
- Key robustness: joint tests across bins with multiple-testing control
  and a bootstrap that preserves the clock.
- Main trap: looking at many bins and reporting the best one; volatility
  seasonality is not mean seasonality.
- Decimal and DST effects can create or destroy a bin overnight.

## What it tests

For a declared bin \(b\) and bar interval, the null is

\[
E\left[r_{t}\mid t \in b\right] = E\left[r_t\right]
\]

for all bins, against the alternative that one or more bins carry a
distinct mean. The mechanism matters: scheduled flows, auction dynamics,
and macro-release timing should explain why a specific bin moves.

## Required data

Intraday bars at a fixed interval for many days, timestamped in one
timezone with daylight-saving transitions handled. The open auction,
closing auction, halts, and half-days are separate regimes and must be
excluded or modelled. Overnight returns must not be pooled with intraday
returns. A bars-only intake cannot separate bid-ask bounce from a mean
effect.

## Simplest useful test

For each predeclared bin, compute the mean return across days with a HAC
\(t\)-statistic, then repeat on a placebo set of days and on shuffled clock
assignments. Report the full vector of bin means, not the best bin: the
shape is the evidence.

## False explanations

- **Volatility seasonality.** The U-shape is in variance; standardizing by
  estimated time-of-day volatility often removes an apparent mean effect.
- **Stale opening prices.** The first bar mixes auction prints and stale
  quotes.
- **Bid-ask bounce.** Bins with more trade reversals show mechanical mean
  effects.
- **Sample-specific calendar.** One earnings season or a macro-release
  cluster drives the bin.
- **Timestamp errors.** Wrong timezone or DST shifts every bin.
- **Multiple testing.** Many bins times many assets guarantees a winner.

## Statistical test

Inference must account for the many bins and the strong intraday
autocorrelation: use HAC or a block bootstrap that resamples whole days,
then a joint Wald or max-statistic across bins with bootstrap critical
values. Apply Romano-Wolf stepdown or a false-discovery rate when many bins
and instruments are examined. Estimate bin patterns in one period and test
them in the next.

## Economic test

A seasonality claims a repeatable move in a fixed window, so costs recur
every day: charge spread, fees, and slippage on each entry and exit, delay
fills past the bin, and report break-even spread and turnover. If the
average bin move is comparable to the spread, the pattern is a
microstructure description, not a trade.

## Failure conditions

- Observable: joint bin test; condition: seasonality claimed; threshold:
  max-statistic bootstrap p-value above the declared level.
- Observable: out-of-sample bins; condition: pattern persists; threshold:
  sign or rank of the top bin changes in the held-out period.
- Observable: volatility standardization; condition: mean effect;
  threshold: effect vanishes after time-of-day scaling.
- Observable: average move versus spread; condition: net positive;
  threshold: bin mean below the round-trip cost.
- Observable: sample support; condition: enough days per bin; threshold:
  count below the declared minimum.

## Limitations

Seasonality is descriptive and decays as it is exploited. Bin definitions
are choices that must be predeclared, and a bars-only study cannot resolve
the microstructure causes behind the pattern.
