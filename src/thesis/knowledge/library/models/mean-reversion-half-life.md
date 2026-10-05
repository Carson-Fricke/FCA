---
id: mean-reversion-half-life
title: Mean-reversion half-life
version: 1
created: 2026-10-02
retrieved: 2026-10-02
source:
  url: https://github.com/Carson-Fricke/FCA
  publisher: FCA Thesis
  content_hash: 55dd5302da3f2c86d8a06f8ae12190f92c5baebd8e1528e1664a46f3c8a56d47
tags:
  - mean-reversion
  - half-life
  - ar1
  - ornstein-uhlenbeck
  - bootstrap
collections:
  - model-cards
links:
  - model-selection-by-hypothesis-shape
  - cointegration-and-spread-stationarity
  - time-series-momentum-and-trend
  - roll-bid-ask-bounce
  - single-security-price-regularity-tests
relationships:
  - type: relates
    target: model-selection-by-hypothesis-shape
  - type: relates
    target: cointegration-and-spread-stationarity
  - type: relates
    target: time-series-momentum-and-trend
  - type: relates
    target: roll-bid-ask-bounce
  - type: relates
    target: single-security-price-regularity-tests
trust:
  tier: curated
  state: published
---
# Mean-reversion half-life

The claim is that after a shock the series returns toward its average at a
measurable speed. The half-life is the time for half of a deviation to
disappear, the bridge from a reversion test to a holding period.

## In brief

- What it tests: the speed of reversion, not merely the presence of it.
- Intake: one clean contiguous series; midquotes or adjusted closes, with
  overnight moves handled explicitly.
- Key robustness: the AR(1) estimate is biased in small samples; correct
  it and report an interval, not a point.
- Main trap: bid-ask bounce and stale prices produce a short, fake
  half-life that cannot be traded.
- A half-life longer than the holding period means no usable reversion.

## What it tests

For a log price \(p_t\), the first-order autoregression

\[
p_t = c + \phi\, p_{t-1} + \varepsilon_t
\]

has a half-life

\[
H = \frac{\ln 2}{-\ln \phi}
\]

when \(0 < \phi < 1\). In continuous time this is the Ornstein-Uhlenbeck
mean-reversion speed. The claim is a value of \(H\) with uncertainty.

## Required data

One price series with a consistent sampling interval, adjusted for
corporate actions, with the overnight move excluded or modelled
separately. Trade prices carry the spread; midquotes are preferred. Zero
returns, missing bars, and halts must be handled explicitly.

## Simplest useful test

Estimate the AR(1) on the declared window, convert \(\hat\phi\) to a
half-life, and report a confidence interval from a bootstrap or the delta
method. Repeat on two disjoint sub-periods. A candidate requires a narrow
interval, agreeing sub-period estimates, and a deviation larger than the
spread.

## False explanations

- **Bid-ask bounce.** Trade prices alternate between bid and ask; the
  apparent half-life can be minutes.
- **Outliers and aggregation.** One bad print dominates a short sample,
  and mixing bar intervals or pooling sessions distorts the dependence.
- **Regime change.** Reversion in one period is trend in another; pooling
  averages two half-lives into a meaningless one.

## Statistical test

\(\hat\phi\) is downward biased in small samples, approximately
\(-(1+3\phi)/n\). Apply a bias correction (Kendall, Andrews, or Hansen
median-unbiased) or bootstrap the estimation, and report the half-life
interval including no reversion. Complement the AR estimate with ADF,
KPSS, and a wild bootstrap.

## Economic test

Turn the half-life into a holding period and a turnover rate. Reversion of
size \(\Delta\) over one half-life must exceed the round-trip spread, fees,
and one bar of delay. Report the break-even deviation, the round trips per
year implied by \(H\), and the capacity at which impact changes the reversion.

## Failure conditions

- Observable: bootstrap interval; condition: reversion claimed;
  threshold: interval for \(H\) includes infinity or zero.
- Observable: half-life versus holding period; condition: tradable
  reversion; threshold: \(H\) greater than the planned hold.
- Observable: sub-period estimates; condition: stable reversion;
  threshold: half-lives differ by more than a declared ratio.
- Observable: deviation size; condition: reversion exceeds costs;
  threshold: typical deviation below spread plus fees.
- Observable: price source; condition: trade prices used; threshold: the
  spread-implied bound explains the estimate.

## Limitations

AR(1) is a linear model of a nonlinear phenomenon; asymmetric or state-
dependent reversion needs another falsifier. Half-life says nothing about
the deviation's cause, and costs truncate the usable part of any reversion.
