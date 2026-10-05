---
id: variance-risk-premium-and-options-measures
title: Variance risk premium and options measures
version: 1
created: 2026-10-02
retrieved: 2026-10-02
source:
  url: https://github.com/Carson-Fricke/FCA
  publisher: FCA Thesis
  content_hash: 01eed2462965ea3f631115f56d9bb8d1e4c50640a64c61a95cd7f188420bf1db
tags:
  - variance-risk-premium
  - implied-volatility
  - volatility-skew
  - term-structure
  - roadmap
collections:
  - model-cards
links:
  - model-selection-by-hypothesis-shape
  - predictive-regression-and-lead-lag
  - realized-volatility-and-vol-forecasting
relationships:
  - type: relates
    target: model-selection-by-hypothesis-shape
  - type: relates
    target: predictive-regression-and-lead-lag
  - type: relates
    target: realized-volatility-and-vol-forecasting
trust:
  tier: curated
  state: published
---
# Variance risk premium and options measures

The claim is that implied volatility carries a premium over subsequent
realized volatility, or that the skew and term structure of the option
surface predict returns. This card records the design for those claims.

## In brief

- What it tests: whether implied volatility exceeds realized volatility
  systematically, and whether surface shape predicts returns.
- Intake: options quotes or chains synchronized with the underlying; not
  currently available to the harness study SDK.
- Key robustness: implied and realized measures are noisy and measured
  over different horizons; error must be propagated.
- Main trap: stale or wide option quotes make the premium look larger than
  it is.
- Treat this card as a roadmap until options intake exists.

## What it tests

The variance risk premium, the difference between option-implied and
subsequently realized variance:

\[
VRP_t = IV^2_t - RV_{t,t+h}
\]

and surface descriptors such as the skew (downside versus upside implied
volatility) and the term-structure slope. The null is that the premium is
zero and surface shape has no forecasting content.

## Required data

Option quotes or chains with strikes, expiries, bid, ask, and open
interest, synchronized with the underlying price and a risk-free rate for
implied-volatility extraction, with filters for stale quotes. Neither the
option surface nor a chain subscription exists in the harness study SDK.

## Intake note

Options intake is not currently supported by the harness study SDK. The SDK
accepts bars, quotes, and trades for equities only, so this card cannot be
executed yet and no options falsifier should be authored against the current
intake. Keep the design here as a roadmap and revisit it when an options
chain subscription exists.

## Simplest useful test

When intake exists: compute the at-the-money implied variance for a
declared expiry, realized variance over the matching horizon, and test the
average difference with HAC inference. Until intake exists, confirm the
card is inapplicable and record that.

## False explanations

- **Stale and wide quotes.** Illiquid strikes inflate measured implied
  volatility; use midpoints with liquidity filters.
- **Variance-swap versus ATM convention.** Interpolated ATM implied
  variance is not the variance-swap rate.
- **Realized measure error.** Microstructure noise biases realized
  variance.
- **Risk-premium regimes.** The average premium hides stress periods when
  it inverts.

## Statistical test

Use HAC or Newey-West inference for overlapping-horizon premiums, and a
block bootstrap for the average difference and its quantiles. For
predictive claims use a declared lag, HAC \(t\), and an out-of-sample
\(R^2\), and control the number of surface descriptors tried.

## Economic test

Option positions pay bid-ask spreads, commissions, and margin, and the
strategy must be sized and rolled; these costs are wider than equity costs
and vary by strike and expiry. Report break-even premium after costs, roll
turnover, and capacity at the available open interest. A premium measured at
mid quotes often disappears at executable prices.

## Failure conditions

- Observable: intake support; condition: test can run; threshold: no
  options subscription in the harness study SDK.
- Observable: quote staleness; condition: usable surface; threshold:
  fraction of stale or crossed option quotes above the declared level.
- Observable: horizon match; condition: premium measured cleanly;
  threshold: implied and realized windows differ.
- Observable: average premium interval; condition: premium exists;
  threshold: bootstrap interval includes zero.
- Observable: open interest; condition: capacity; threshold: required size
  above available liquidity.

## Limitations

This card is documentation only until intake lands. Implied volatility
extraction is model-dependent, the premium is time-varying, and the
platform does not yet provide options market structure.
