---
id: spread-and-execution-cost-estimation
title: Spread and execution cost estimation
version: 1
created: 2026-10-02
retrieved: 2026-10-02
source:
  url: https://github.com/Carson-Fricke/FCA
  publisher: FCA Thesis
  content_hash: cd926f45a77d312d96f3423a4ea15d01b50e9c84e9619b5bd65d28441585f311
tags:
  - effective-spread
  - realized-spread
  - roll-model
  - break-even
  - capacity
collections:
  - model-cards
links:
  - model-selection-by-hypothesis-shape
  - order-flow-imbalance-and-price-impact
  - tradability-break-even-and-oos
  - roll-bid-ask-bounce
relationships:
  - type: relates
    target: model-selection-by-hypothesis-shape
  - type: relates
    target: order-flow-imbalance-and-price-impact
  - type: relates
    target: tradability-break-even-and-oos
  - type: relates
    target: roll-bid-ask-bounce
trust:
  tier: curated
  state: published
---
# Spread and execution cost estimation

Every other card ends here. A signal is only an edge if the expected move
exceeds the cost of capturing it. This card measures quoted, effective, and
realized spreads, the Roll bound, execution delay, break-even, and capacity.

## In brief

- What it tests: the round-trip cost a rule must overcome, measured from
  quotes and trades rather than assumed.
- Intake: quotes and trades for the same instrument and window; bars
  cannot measure a spread.
- Key robustness: report quantiles and cash-weighted means across days,
  with bootstrap intervals, not one pooled average.
- Main trap: using the quoted spread when fills happen at worse prices, or
  ignoring size and time variation of cost.
- Break-even first: if the signal is smaller than cost, stop.

## What it tests

Three standard measures:

\[
\text{effective spread} = 2\left|p_t - m_t\right|
\]

\[
\text{realized spread} = 2\,d_t\left(p_t - m_{t+\delta}\right)
\]

\[
\text{Roll bound} = 2\sqrt{-\operatorname{Cov}(\Delta p_t, \Delta p_{t-1})}
\]

where \(p_t\) is the trade price, \(m_t\) the prevailing midquote, and
\(d_t\) the trade direction. The realized spread is what the liquidity
provider keeps after horizon \(\delta\); the difference from the effective
spread is adverse selection.

## Required data

Quotes and trades, synchronized and classified, over a window that includes
different volatility and activity conditions. Report the trade size
distribution because cost depends on size. The Roll bound only applies when
the return autocovariance is negative; otherwise it is undefined.

## Simplest useful test

Compute the volume-weighted effective spread and the realized spread at a
declared horizon such as five minutes, then compare both with the signal's
average expected move. If the expected move is below the effective spread
plus fees plus delay, the result is settled before any backtest.

## False explanations

- **Stale quotes.** Midquotes that lag the trade make the effective spread
  look small or negative.
- **Time variation.** Cost is wide at the open, at the close, and in
  stress; one average hides the regimes a strategy trades.

## Statistical test

Estimate cost per day and report the cross-day distribution: median,
cash-weighted mean, and upper quantiles with bootstrap intervals. Costs are
autocorrelated, so use HAC or a block bootstrap, and cluster by instrument
when pooling a panel.

## Economic test

Build the break-even explicitly: expected gross move minus effective
spread minus fees minus slippage minus impact of the required size. Report
turnover and the capacity at which impact makes the edge disappear. Delay
the fill by one bar and recompute; an edge in the touch price does not exist.

## Failure conditions

- Observable: break-even spread; condition: signal positive; threshold:
  break-even above measured effective spread.
- Observable: realized minus effective; condition: liquidity provider
  claim; threshold: realized spread non-positive at the declared horizon.
- Observable: cost quantiles; condition: cost budget; threshold: upper
  decile cost above the declared budget.
- Observable: capacity; condition: size feasible; threshold: required size
  above a declared participation share.
- Observable: delay sensitivity; condition: edge survives; threshold: edge
  turns negative after one bar of delay.

## Limitations

Measured historical cost is not future cost, and hidden liquidity and
order-type choices change realized fills. Capacity estimates depend on an
impact model that is itself uncertain.
