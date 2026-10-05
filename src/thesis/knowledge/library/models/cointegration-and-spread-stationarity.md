---
id: cointegration-and-spread-stationarity
title: Cointegration and spread stationarity
version: 1
created: 2026-10-02
retrieved: 2026-10-02
source:
  url: https://github.com/Carson-Fricke/FCA
  publisher: FCA Thesis
  content_hash: 5a42708499baad396931ab26813f685c5bab0d65243ec302e83ca728a2c14aeb
tags:
  - cointegration
  - pairs-trading
  - adf
  - johansen
  - stationarity
collections:
  - model-cards
links:
  - model-selection-by-hypothesis-shape
  - mean-reversion-half-life
  - robust-autocorrelation-inference
  - tradability-break-even-and-oos
relationships:
  - type: relates
    target: model-selection-by-hypothesis-shape
  - type: relates
    target: mean-reversion-half-life
  - type: relates
    target: robust-autocorrelation-inference
  - type: relates
    target: tradability-break-even-and-oos
trust:
  tier: curated
  state: published
---
# Cointegration and spread stationarity

The claim is that two or more prices are tied together in the long run, so
their spread wanders but returns to an equilibrium. The profit must cover
costs on every leg.

## In brief

- What it tests: a stationary long-run equilibrium among two or more
  non-stationary prices.
- Intake: synchronized, action-adjusted prices or quotes for every leg,
  covering the same sessions.
- Key robustness: residual-based critical values, HAC inference, and
  rolling stability of the hedge ratio.
- Main trap: searching many pairs makes cointegration appear, and a
  rejected test says nothing about costs.

## What it tests

Two series that are each integrated of order one have a cointegrating
relationship when some combination is stationary:

\[
z_t = y_t - \beta x_t, \qquad z_t \sim I(0)
\]

The null of no cointegration is that every combination remains
non-stationary. A verified spread must also revert fast enough to trade.

## Required data

Synchronized prices for all legs on the same clock, adjusted for splits
and dividends, in one currency. Sessions must match: an instrument that
does not trade with its partner produces stale-price spreads. The sample
must cover the regimes the trade will face, and the number of pairs
examined is part of the search.

## Simplest useful test

Estimate the hedge ratio by ordinary least squares on a declared window,
then test the residual for a unit root with an augmented Dickey-Fuller
regression. Use the residual-based critical values of Engle and Granger or
MacKinnon, not the standard ADF table: the fitted residual is not an
observed series. For more than two legs use the Johansen trace or maximum
eigenvalue test and report the chosen rank and lag order.

## False explanations

- **Searched pairs.** Running many pairs and reporting the significant one
  is data mining.
- **Spurious regression.** Two independent random walks can look related
  in a finite sample.
- **Beta instability.** A hedge ratio that drifts makes the spread look
  stationary in one window and explosive in the next.
- **Corporate actions and stale prices.** Unadjusted splits, dividends,
  or non-trading create artificial reversion.

## Statistical test

Use Engle-Granger or Johansen for the relationship, ADF for the residual
null, and KPSS as a complement: ADF fails to reject and KPSS rejects
together suggest a unit root, while the reverse suggests stationarity.
Residual-based inference is small-sample biased, so report a bootstrap
distribution and a half-life estimate. Re-estimate rolling hedge ratios to
check stability, and compare the spread's ADF statistic with a residual
bootstrap under the no-cointegration null.

## Economic test

A pairs trade pays the spread on entry and exit for every leg, plus fees,
borrow, and slippage. Compute the break-even spread from the average
expected reversion, count round trips per year, and compare with the
measured effective spread on each leg. Delay execution by one bar and
re-run; a convergence that needs the touch price is not tradable.

## Failure conditions

- Observable: residual ADF; condition: stationarity claimed; threshold:
  statistic above the MacKinnon critical value.
- Observable: half-life; condition: spread reverts within the horizon;
  threshold: half-life longer than the planned holding period.
- Observable: rolling beta; condition: constant ratio; threshold: beta
  range wider than a declared band.
- Observable: leg spread; condition: break-even below trading cost;
  threshold: expected reversion below the sum of leg spreads.
- Observable: pair count; condition: many pairs examined; threshold:
  selection-adjusted significance fails.

## Limitations

Cointegration describes a sample, not a law: relationships decay after
structural change and arbitrage. The test has low power in short samples
and can accept or reject on tiny lag differences. A stationary spread with
a half-life of months is not a trade.
