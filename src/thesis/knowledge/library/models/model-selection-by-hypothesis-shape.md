---
id: model-selection-by-hypothesis-shape
title: Model selection by hypothesis shape
version: 1
created: 2026-10-02
retrieved: 2026-10-02
source:
  url: https://github.com/Carson-Fricke/FCA
  publisher: FCA Thesis
  content_hash: 8b18338210db4a70ba51cc7ba4662deabf57197a0871aa4711c0235570a77c8b
tags:
  - model-selection
  - hypothesis-shape
  - falsifier
collections:
  - model-cards
links:
  - predictive-regression-and-lead-lag
  - cointegration-and-spread-stationarity
  - mean-reversion-half-life
  - time-series-momentum-and-trend
  - cross-sectional-momentum-and-rank-ic
  - realized-volatility-and-vol-forecasting
  - intraday-seasonality
  - order-flow-imbalance-and-price-impact
  - spread-and-execution-cost-estimation
  - event-studies
  - structural-breaks-and-regime-tests
  - overfitting-and-selection-discipline
  - point-in-time-data-validity
  - variance-risk-premium-and-options-measures
relationships:
  - type: relates
    target: predictive-regression-and-lead-lag
  - type: relates
    target: cointegration-and-spread-stationarity
  - type: relates
    target: mean-reversion-half-life
  - type: relates
    target: time-series-momentum-and-trend
  - type: relates
    target: cross-sectional-momentum-and-rank-ic
  - type: relates
    target: realized-volatility-and-vol-forecasting
  - type: relates
    target: intraday-seasonality
  - type: relates
    target: order-flow-imbalance-and-price-impact
  - type: relates
    target: spread-and-execution-cost-estimation
  - type: relates
    target: event-studies
  - type: relates
    target: structural-breaks-and-regime-tests
  - type: relates
    target: overfitting-and-selection-discipline
  - type: relates
    target: point-in-time-data-validity
  - type: relates
    target: variance-risk-premium-and-options-measures
trust:
  tier: curated
  state: published
---
# Model selection by hypothesis shape

A hypothesis arrives as a shape, not a test. This hub maps the shape of the claimed regularity to the card that can falsify it and the intake that card needs. Search the shared library for the hypothesis terms first.

## In brief

- Choose the card from the shape of the claim, not the data you already hold.
- Search the library for the hypothesis terms before opening any card.
- Intake is part of the choice: bars alone cannot test a spread claim.
- Each card names its robust statistic and its main false explanation.
- A rejected null on one series is not a portfolio edge; every card ends at costs.

## What it tests

Whether a falsifier can see the claimed regularity at all. A trend claim tested with a reversion statistic, or a spread claim tested on bars alone, produces a "no result" that says nothing about the hypothesis.

| Regularity shape in the claim | Card | Intake the falsifier needs |
| --- | --- | --- |
| Lagged response to a predictor or benchmark | [Predictive regression and lead-lag](predictive-regression-and-lead-lag) | Aligned predictor and target on one clock; quote or trade when the mechanism is microstructure |
| Two series held by an equilibrium | [Cointegration and spread stationarity](cointegration-and-spread-stationarity) | Synchronized prices or quotes for every leg; action adjusted |
| A shock that fades at a measurable speed | [Mean-reversion half-life](mean-reversion-half-life) | One clean contiguous price series, no overnight pooling |
| A persistent drift in one series | [Time-series momentum and trend](time-series-momentum-and-trend) | A long clean price history for the traded series |
| Winners keep beating losers across names | [Cross-sectional momentum and rank IC](cross-sectional-momentum-and-rank-ic) | A point-in-time panel of many securities |
| Volatility clusters or is forecastable | [Realized volatility and vol forecasting](realized-volatility-and-vol-forecasting) | Intraday or daily bars; ranges or realized variance |
| A move repeats at the same time each day | [Intraday seasonality](intraday-seasonality) | Intraday bars with clean sessions and correct timezone |
| Order pressure moves the price | [Order-flow imbalance and price impact](order-flow-imbalance-and-price-impact) | Quotes and trades together, with signed trades |
| The cost of trading is the question | [Spread and execution cost estimation](spread-and-execution-cost-estimation) | Quotes and trades for the same instrument and window |
| An announcement moves the price | [Event studies](event-studies) | Event dates with timestamps plus returns and a benchmark |
| The relationship changed partway through | [Structural breaks and regime tests](structural-breaks-and-regime-tests) | A long clean series covering both candidate regimes |
| Too many variants were tried | [Overfitting and selection discipline](overfitting-and-selection-discipline) | The full attempt log and an untouched holdout window |
| The data may not be what it claims to be | [Point-in-time data validity](point-in-time-data-validity) | As-of snapshots, membership history, action handling |
| Implied versus realized volatility | [Variance risk premium and options measures](variance-risk-premium-and-options-measures) | Options quotes; intake is roadmap, not supported yet |

## Required data

Declare the intake before the study. The harness study SDK accepts bars (1 minute or 1 day), quotes, and trades; a bars-only study cannot measure a spread, signed flow, or midquote returns. Start with the smallest intake that can falsify the claim.

## Simplest useful test

Open the matching card, run its "Simplest useful test" section with the window and variants written down beforehand, then apply its statistical and economic sections in order.

## False explanations

The shape may be misfiled: a spread bounce looks like reversion, a stale price looks like lead-lag, and a volatility pattern looks like a return pattern. A claim may also mix shapes, or describe a data artefact that disappears after point-in-time checks.

## Statistical test

The hub computes nothing. Each card names its robust statistic: HAC or wild bootstrap for regression and autocorrelation claims, Lo-MacKinlay for variance ratios, clustered or bootstrap inference for panel claims, and a selection-adjusted test when variants were tried.

## Economic test

Every card ends with costs, an execution delay, break-even, and turnover. If the estimated effect is smaller than the measured round-trip cost, stop and record the outcome; that is a finding.

## Failure conditions

- Observable: `data_ok`; condition: the intake cannot support the card; threshold: `data_ok < 1.0` means the result is not interpretable.
- Observable: net edge after delay; condition: gross effect is real but smaller than cost; threshold: net edge at or below zero.
- Observable: robust statistic; condition: significance under the card's robust version; threshold: robust \(t\) below the card's level.
- Observable: agreement; condition: two cards disagree on the same window; threshold: opposite signs or non-overlapping intervals.

## Limitations

The map is a starting point, not proof, and shapes overlap: reversion and trend can both live in one series at different horizons. New shapes without a matching card need method research before a falsifier is written.
