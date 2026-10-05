---
id: realized-volatility-and-vol-forecasting
title: Realized volatility and vol forecasting
version: 1
created: 2026-10-02
retrieved: 2026-10-02
source:
  url: https://github.com/Carson-Fricke/FCA
  publisher: FCA Thesis
  content_hash: 8a75b6c3d42e2932903e73f44f4ddc3dbaa9b400dd05754a9adf825c829f32c7
tags:
  - realized-volatility
  - volatility-forecasting
  - har
  - ewma
  - qlike
collections:
  - model-cards
links:
  - model-selection-by-hypothesis-shape
  - intraday-seasonality
  - overfitting-and-selection-discipline
relationships:
  - type: relates
    target: model-selection-by-hypothesis-shape
  - type: relates
    target: intraday-seasonality
  - type: relates
    target: overfitting-and-selection-discipline
trust:
  tier: curated
  state: published
---
# Realized volatility and vol forecasting

The claim is about volatility, not direction: volatility clusters, and the
next period's volatility can be predicted from realized measures. The test
compares estimators and forecasts out of sample.

## In brief

- What it tests: whether one volatility estimator or forecast beats
  another out of sample.
- Intake: intraday bars, quotes, or trades for realized variance; daily
  bars for range measures.
- Key robustness: measurement error and microstructure noise dominate, so
  compare losses with robust inference.
- Main trap: range estimators carry structural biases, and a better
  volatility forecast is not a return signal.
- Always compare against yesterday's realized variance or a random walk.

## What it tests

The target is realized variance over a horizon,

\[
RV_t = \sum_{j=1}^{m} r_{t,j}^2
\]

built from \(m\) intraday returns, while the object under test is a forecast
\(\hat\sigma^2_{t+1|t}\) from a model such as EWMA, GARCH, or the
heterogeneous autoregression

\[
RV_{t+1} = c + \beta_d RV^{(d)}_t + \beta_w RV^{(w)}_t + \beta_m RV^{(m)}_t
\]

The comparison is a loss difference, not a regression \(R^2\).

## Required data

Intraday returns at a consistent interval with clean sessions, plus the
daily close series for overnight gaps. Realized measures need enough
intraday observations; range measures (Parkinson, Garman-Klass,
Rogers-Satchell, Yang-Zhang) use daily open, high, low, and close and embed
price-path assumptions.

## Simplest useful test

Compute daily realized variance from 1-minute returns, forecast tomorrow
with a declared EWMA or HAR on a training window, and compare the forecast
with tomorrow's realized variance over a held-out period using the QLIKE
loss. Beat the random-walk benchmark or report that you did not.

## False explanations

- **Range-estimator bias.** Parkinson and Garman-Klass assume no drift and
  continuous observation; a biased estimator can look like a better model.
- **Microstructure noise.** Squared high-frequency returns include
  bounce; more sampling can make the measure worse.
- **Overnight gaps.** Ignoring the close-to-open move understates daily
  variance.
- **Annualization errors.** Mixing daily and annualized units reverses
  conclusions.
- **Regime change.** A model that wins in calm markets can lose in
  stressed ones.

## Statistical test

Use QLIKE or a robust loss with a Diebold-Mariano test whose standard error
is HAC; loss differences are autocorrelated. Report a wild bootstrap for
the loss difference and a Mincer-Zarnowitz regression of realized on
forecast, where a slope different from one indicates bias. Compare against
EWMA and random-walk benchmarks on the same split.

## Economic test

Volatility forecasts enter sizing, stops, and rebalancing, so their value
appears indirectly. Show how the forecast changes turnover, position size,
and cost, with a one-bar delay and measured spreads. A tight forecast that
raises turnover more than it improves risk-adjusted return is a loss.

## Failure conditions

- Observable: QLIKE difference; condition: model claimed better;
  threshold: difference interval includes zero.
- Observable: forecast bias slope; condition: calibrated forecast;
  threshold: Mincer-Zarnowitz slope outside a declared band.
- Observable: sampling frequency; condition: estimator stable; threshold:
  forecasts change materially with the sampling interval.
- Observable: net turnover; condition: forecast adds value; threshold:
  extra cost exceeds risk-adjusted improvement.
- Observable: regime; condition: model robust; threshold: loss ranking
  reverses between calm and stressed windows.

## Limitations

Estimator choice and sampling frequency are researcher decisions that must
be declared. Realized variance is a noisy target, and a statistically
better forecast can have no economic value.
