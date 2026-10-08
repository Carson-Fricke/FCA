---
id: time-series-momentum-and-trend
title: Time-series momentum and trend
version: 1
created: 2026-10-02
retrieved: 2026-10-02
source:
  url: https://github.com/Carson-Fricke/FCA
  publisher: FCA Thesis
  content_hash: 18b16d68dab7fa8579b3aadabcb53877e3a7074646e5b78f895c65fce138a63e
tags:
  - time-series-momentum
  - trend-following
  - moving-average
  - hurst
  - crash-risk
collections:
  - model-cards
links:
  - model-selection-by-hypothesis-shape
  - mean-reversion-half-life
  - cross-sectional-momentum-and-rank-ic
  - overfitting-and-selection-discipline
  - directional-change-events
  - time-to-crossover-statistics
relationships:
  - type: relates
    target: model-selection-by-hypothesis-shape
  - type: relates
    target: mean-reversion-half-life
  - type: relates
    target: cross-sectional-momentum-and-rank-ic
  - type: relates
    target: overfitting-and-selection-discipline
  - type: relates
    target: directional-change-events
  - type: relates
    target: time-to-crossover-statistics
trust:
  tier: curated
  state: published
---
# Time-series momentum and trend

The claim is that a series' own past direction predicts its next direction:
sign rules, moving-average crossings, breakouts. The test is whether rule
returns have a positive conditional mean after costs and delay.

## In brief

- What it tests: continuation in one series under an explicitly stated
  rule.
- The sign rule and the moving-average crossover are the simplest baseline
  rules, not the search-space limit; event-time and duration framings are
  alternatives.
- Intake: a long clean price history at a declared interval; adjusted
  closes are usually enough.
- Key robustness: overlapping rule returns need HAC inference and a
  bootstrap, plus a risk and drawdown view.
- Main trap: a strong sample drift makes long-biased rules look
  predictive, and parameter sweeps make the best variant look real.
- Momentum crashes are the known tail cost of the strategy.

## What it tests

A rule maps history to a position and the test is whether the rule's
return has a positive mean:

\[
E\left[ w_t\, r_{t:t+h} \right] > 0
\]

for a declared position rule such as \(w_t = \operatorname{sign}(r_{t-k:t})\)
or a moving-average crossover. The rule form, lookback, and holding period
are declared before estimation.

## Required data

A long, contiguous adjusted price history at one declared interval. The
sample must include different volatility regimes; a single bull market
cannot distinguish trend from drift. If the rule uses intraday signals,
quotes or trades are needed for the fill assumption.

## Simplest useful test

Trade the sign rule on a declared lookback with a one-bar delay and hold
for \(h\) bars, then compare the mean rule return with buy-and-hold on the
same window using a HAC \(t\)-statistic. Repeat on a held-out period. Only
the out-of-sample comparison addresses the "everything went up"
explanation.

This sign/MA test is the baseline. Two alternatives worth declaring as
additional baselines: confirmation-time [Directional change
events](directional-change-events), which define entries by threshold moves
rather than by clock lags, and [Time-to-crossover
statistics](time-to-crossover-statistics), which treat the waiting time to a
crossover as the object under test.

## False explanations

- **Sample drift.** A long uptrend makes long-biased rules profitable
  without predictability; the benchmark handles this.
- **Lookahead in the rule.** A moving average that includes the current
  close cannot be traded at that close.
- **Parameter search.** The best of many lookback and holding choices is
  selected on noise.
- **Volatility scaling.** Rebasing returns to constant risk changes the
  comparison and must be declared, not chosen afterward.
- **Hurst exponent misread.** Estimated \(H > 0.5\) is noisy and biased in
  short samples; it is a description, not an edge.

## Statistical test

Overlapping rule returns are autocorrelated: use Newey-West or a block
wild bootstrap for the mean, and a HAC standard error for the Sharpe
ratio. When the rule family was searched, use White Reality Check, Hansen
SPA, or Romano-Wolf stepdown across variants and prefer the deflated
Sharpe ratio. A Hurst estimate needs a bootstrap interval before any
interpretation. Require stronger evidence than \(t > 3\) for a searched
rule.

## Economic test

Turnover is the core cost: a sign rule flips fully on small moves. Charge
spread, fees, and slippage on every flip plus one bar of delay, and report
break-even spread and capacity. Examine the worst drawdown and recovery
time, because momentum returns are negatively skewed: many small gains and
rare crashes. Risk adjustment is not optional.

## Failure conditions

- Observable: HAC \(t\)-statistic; condition: gross mean positive;
  threshold: \(|t^\*| < 3\) or interval includes zero.
- Observable: net versus buy-and-hold; condition: trend claimed;
  threshold: net rule mean no better than benchmark.
- Observable: break-even spread; condition: net edge positive; threshold:
  the measured effective spread is above the break-even spread, so the
  net edge is at or below zero.
- Observable: drawdown; condition: acceptable risk; threshold: drawdown
  deeper than declared or recovery longer than the hold budget.
- Observable: variant count; condition: many rules tried; threshold:
  selection-adjusted test fails.

## Limitations

Single-series trend rules give no diversification story and are dominated
by regime timing, and negative skew means Sharpe understates tail risk.

## Related

- [Model selection by hypothesis shape](model-selection-by-hypothesis-shape)
- [Mean-reversion half-life](mean-reversion-half-life)
- [Cross-sectional momentum and rank IC](cross-sectional-momentum-and-rank-ic)
- [Overfitting and selection discipline](overfitting-and-selection-discipline)
- [Directional change events](directional-change-events)
- [Time-to-crossover statistics](time-to-crossover-statistics)
