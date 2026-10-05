---
id: predictive-regression-and-lead-lag
title: Predictive regression and lead-lag
version: 1
created: 2026-10-02
retrieved: 2026-10-02
source:
  url: https://github.com/Carson-Fricke/FCA
  publisher: FCA Thesis
  content_hash: b54c2fbdde2dd0dd465d9c8497d31d1357df1d55d1961b283acc8aa74c6fb42c
tags:
  - predictive-regression
  - lead-lag
  - hac
  - out-of-sample
  - timestamp-alignment
collections:
  - model-cards
links:
  - model-selection-by-hypothesis-shape
  - mean-reversion-half-life
  - overfitting-and-selection-discipline
  - robust-autocorrelation-inference
relationships:
  - type: relates
    target: model-selection-by-hypothesis-shape
  - type: relates
    target: mean-reversion-half-life
  - type: relates
    target: overfitting-and-selection-discipline
  - type: relates
    target: robust-autocorrelation-inference
trust:
  tier: curated
  state: published
---
# Predictive regression and lead-lag

The claim is that information available now predicts a later return: a
benchmark leads a follower, a slow signal leads a fast one, or a lagged
value predicts another series.

## In brief

- What it tests: conditional mean predictability from a lagged predictor.
- Intake: an aligned predictor and target on one clock; bars first, quotes
  or trades only for microstructure mechanisms.
- Key robustness: HAC standard errors, a declared lag, and a true
  out-of-sample split.
- Main trap: nonsynchronous or restated timestamps manufacture lead-lag,
  and a sweep over lags manufactures significance.
- Predictor and target must be point-in-time values, not latest revisions.

## What it tests

The conditional mean restriction

\[
E\left[r_t \mid x_{t-k}\right] = 0
\]

for a declared lag \(k\). The alternative is that \(x_{t-k}\) carries
information about \(r_t\) beyond the series' own history. State direction
and horizon before estimation.

## Required data

One predictor and one target timestamped on the same clock and timezone,
with the lag fixed in advance. The predictor must be the value knowable at
\(t-k\), not a later revision. Bars can test slow lead-lag; short-horizon
price discovery claims need quotes or trades aligned to the same interval.

## Simplest useful test

Estimate the one-lag regression

\[
r_t = \alpha + \beta\, x_{t-k} + \varepsilon_t
\]

with a Newey-West \(t\)-statistic, report \(\hat\beta\) and its interval,
then repeat on a held-out half. A candidate requires the sign, size, and
significance to survive the split.

## False explanations

- **Nonsynchronous trading.** A frequently traded benchmark moves first
  and a thin follower updates later; the lead is the clock.
- **Microstructure noise.** Bid-ask bounce in the target creates lag
  dependence that reads as a predictor effect.
- **Omitted common factor.** Both series respond to a third factor.
- **Overlapping windows.** Shared shocks inflate the information in one
  lag.

## Statistical test

Use a HAC standard error with bandwidth tied to the lag and horizon, and a
wild bootstrap for the null of zero mean information. For overlapping
horizons use the Lo-MacKinlay variance-ratio statistic, not an IID formula.
When several lags or predictors were tried, apply White Reality Check,
Hansen SPA, or Romano-Wolf stepdown, and require stronger evidence than
\(t > 2\). Pair every in-sample fit with an out-of-sample \(R^2\) and a
Mincer-Zarnowitz check.

## Economic test

Signal at \(t\), fill at \(t+1\), and charge spread, fees, and slippage on
every position change. Report turnover, the break-even spread implied by
the average forecast, and capacity. Compare break-even with the measured
effective spread before calling the effect tradable.

## Failure conditions

- Observable: HAC \(t\)-statistic; condition: raw significance under OLS;
  threshold: \(|t^\*| < 3\) after HAC and the declared lag count.
- Observable: coefficient sign; condition: stable relation; threshold:
  opposite sign in the held-out half.
- Observable: out-of-sample \(R^2\); condition: in-sample fit positive;
  threshold: OOS \(R^2 \le 0\).
- Observable: forecast size; condition: average forecast above round-trip
  cost; threshold: break-even spread above measured spread.
- Observable: predictor vintages; condition: values not restated;
  threshold: any restatement inside the window.

## Limitations

Linear lag regression sees only linear mean dependence; sign, size, and
nonlinear state effects need their own falsifiers. Timestamp alignment
quality caps every conclusion here, and a clean lead-lag can still be
untradable.
