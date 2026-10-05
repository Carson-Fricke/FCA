---
id: cross-sectional-momentum-and-rank-ic
title: Cross-sectional momentum and rank IC
version: 1
created: 2026-10-02
retrieved: 2026-10-02
source:
  url: https://github.com/Carson-Fricke/FCA
  publisher: FCA Thesis
  content_hash: 47bf3b3ca81e066c63ad058aff328ac27c61eeb6b09ead4f57d61545b709c51f
tags:
  - cross-sectional-momentum
  - rank-ic
  - factor-neutralization
  - turnover
  - panel
collections:
  - model-cards
links:
  - model-selection-by-hypothesis-shape
  - overfitting-and-selection-discipline
  - point-in-time-data-validity
  - tradability-break-even-and-oos
relationships:
  - type: relates
    target: model-selection-by-hypothesis-shape
  - type: relates
    target: overfitting-and-selection-discipline
  - type: relates
    target: point-in-time-data-validity
  - type: relates
    target: tradability-break-even-and-oos
trust:
  tier: curated
  state: published
---
# Cross-sectional momentum and rank IC

The claim is that relative past performance sorts future relative
performance across a universe: winners keep outperforming losers. This
card covers portfolio sorts, rank information coefficients, neutralization,
and the turnover that decides the outcome.

## In brief

- What it tests: whether a cross-sectional signal ranks forward returns.
- Intake: a point-in-time panel of many securities with adjusted returns
  and a survivorship-free universe.
- Key robustness: cross-sectional and time dependence need clustering or
  a block bootstrap, not IID \(t\)-statistics.
- Main trap: the effect concentrates in microcaps and illiquid names that
  cannot be traded at the measured returns.
- Rebalancing turnover, not the long-short spread, usually decides.

## What it tests

Jegadeesh-Titman-style momentum: rank securities on their past
\(J\)-month return, form top and bottom portfolios, hold for \(K\) months,
and measure the spread. The panel counterpart is the rank information
coefficient, the Spearman correlation between signal and forward return:

\[
IC_t = \operatorname{corr}_{\text{rank}}\left(s_{i,t},\, r_{i,t+h}\right)
\]

A usable claim shows both a positive mean IC and a monotone decile spread.

## Required data

A panel of many securities over enough calendar time, with point-in-time
membership, adjusted returns, and delisting returns. The universe must be
defined as of each rebalance date, not today's list. Price and liquidity
filters must use information available at formation.

## Simplest useful test

Sort into five or ten deciles on the declared formation window, measure the
top-minus-bottom spread over the declared holding window with a one-period
delay, and report the average rank IC with its standard error. Repeat on a
held-out period. The honest comparison is net of costs; the gross spread is
only a screen.

## False explanations

- **Microcap and illiquidity.** Small, thin names drive the spread and
  cannot absorb size.
- **Size and value overlap.** A spread that disappears after
  neutralization is not momentum.
- **Industry momentum.** Sector effects produce cross-sectional
  persistence that concentration rules may already capture.
- **Bid-ask bounce in the signal.** Trade-price formation returns embed
  the spread and rank names by noise.
- **Survivorship.** Delisted losers omitted from the panel make the loser
  leg look better.

## Statistical test

Use Fama-MacBeth regressions with Newey-West standard errors for the signal
coefficient, or a block bootstrap resampled across calendar time for the
spread and its quantiles. Report the IC \(t\)-statistic adjusted for
autocorrelation, and test monotonicity across all deciles rather than the
extremes alone. Neutralize against size, value, and industry in a declared
regression and report both raw and neutralized spreads. When \(J\) and
\(K\) were searched, apply a selection-adjusted test.

## Economic test

Charge the spread on entry and exit for every name, plus fees and slippage,
and delay execution past the formation date. Report average turnover per
rebalance, the break-even spread, and capacity under a participation limit.
A spread that needs the full sample to cover costs, or that requires
illiquid names, fails regardless of its \(t\)-statistic.

## Failure conditions

- Observable: net long-short spread; condition: gross spread positive;
  threshold: net spread at or below zero after cost and delay.
- Observable: decile monotonicity; condition: signal ranks returns;
  threshold: more than one reversal with overlapping intervals.
- Observable: neutralized spread; condition: standalone momentum;
  threshold: spread indistinguishable from zero after neutralization.
- Observable: participation; condition: capacity adequate; threshold:
  required size above a declared fraction of volume.
- Observable: IC stability; condition: persistent IC; threshold: sign
  flips across held-out periods.

## Limitations

The result depends on universe construction and filters as much as on the
signal, and capacity binds quickly when the effect lives in small names.
