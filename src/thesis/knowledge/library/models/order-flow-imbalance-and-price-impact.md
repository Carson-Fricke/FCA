---
id: order-flow-imbalance-and-price-impact
title: Order-flow imbalance and price impact
version: 1
created: 2026-10-02
retrieved: 2026-10-02
source:
  url: https://github.com/Carson-Fricke/FCA
  publisher: FCA Thesis
  content_hash: 50541659ebf3fed2a6873be492c3d68f959c95e3700f6701a3dc6c77551ee562
tags:
  - order-flow
  - price-impact
  - kyle-lambda
  - adverse-selection
  - microstructure
collections:
  - model-cards
links:
  - model-selection-by-hypothesis-shape
  - spread-and-execution-cost-estimation
  - point-in-time-data-validity
relationships:
  - type: relates
    target: model-selection-by-hypothesis-shape
  - type: relates
    target: spread-and-execution-cost-estimation
  - type: relates
    target: point-in-time-data-validity
trust:
  tier: curated
  state: published
---
# Order-flow imbalance and price impact

The claim is that buying and selling pressure moves the price, measured as
the relation between signed order flow and the midquote return. It is the
workhorse relation behind slippage, adverse selection, and capacity, and it
is easy to misread.

## In brief

- What it tests: how signed volume or trade imbalance moves the price.
- Intake: quotes and trades together for the same instrument, with signed
  trades and synchronized timestamps.
- Key robustness: flow and returns are highly autocorrelated, so use HAC
  or GMM with clustered errors, never IID formulas.
- Main trap: a contemporaneous relation is impact, not prediction; only
  the delayed version can be traded.
- No-trade intervals and classification errors bias the slope.

## What it tests

The linear price-impact relation

\[
r_t = \lambda\, OFI_t + \varepsilon_t
\]

where \(OFI_t\) is signed order flow, for example net buy volume over total
volume, and \(\lambda\) is the Kyle-style impact coefficient.
Contemporaneous estimation measures execution cost; a delayed version tests
whether flow predicts the next return.

## Required data

Quotes and trades at synchronized timestamps, with trades classified as
buyer- or seller-initiated. The quote series defines the midquote and
spread; the trade series gives signed flow and volume. Intervals with no
trades must be retained as zero-flow observations or the regression is
conditioned on activity.

## Simplest useful test

Regress the midquote return over a declared interval on the concurrent
order-flow imbalance with a HAC standard error. Report \(\hat\lambda\), its
interval, and the implied cost of demanding liquidity. Then move the flow
one interval back and re-estimate: if only the contemporaneous estimate is
strong, the relation is impact, not a forecast.

## False explanations

- **Bid-ask bounce.** Trade prices alternate across the spread; the
  midquote removes some but not all of this.
- **Simultaneity.** Flow and price are determined together; impact can be
  read as prediction.
- **Trade classification error.** Tick and quote rules misclassify large
  fractions of trades, biasing the slope.
- **Inventory and quote effects.** Dealers skew quotes as inventory
  builds; the midquote moves without information.
- **Event clustering.** News drives both flow and price.
- **No-trade bias.** Conditioning on trades drops quiet intervals and
  overstates the relation.

## Statistical test

Use Newey-West standard errors with a bandwidth covering the flow and
return autocorrelation, cluster by day where dependence is cross-sectional,
and consider GMM with lagged instruments for the simultaneous equation.
Report a block bootstrap interval for \(\hat\lambda\) and estimate it on
sub-periods. For the predictive question, use a declared lag, HAC \(t\),
and an out-of-sample split.

## Economic test

Impact is a cost, so the economic test is a break-even: the edge from any
flow-based signal must exceed the spread plus the impact of the size the
signal needs. Report how \(\hat\lambda\) scales with order size, the
break-even size, and capacity under a participation limit. Delay the signal
one interval before filling.

## Failure conditions

- Observable: delayed coefficient; condition: predictive claim; threshold:
  delayed estimate indistinguishable from zero.
- Observable: classification rate; condition: usable flow measure;
  threshold: unclassified fraction above a declared level.
- Observable: impact versus edge; condition: net positive signal;
  threshold: implied impact cost above the forecast move.
- Observable: intake; condition: quotes and trades available; threshold:
  bars-only intake means the claim cannot be tested.
- Observable: sub-period slope; condition: stable impact; threshold: slope
  sign or magnitude changes materially across periods.

## Limitations

The linear impact model ignores depth, order-size distribution, and
nonlinearity; the estimate is local to the sample's liquidity and does not
transfer across venues, assets, or volatility regimes.
