---
id: overfitting-and-selection-discipline
title: Overfitting and selection discipline
version: 1
created: 2026-10-02
retrieved: 2026-10-02
source:
  url: https://github.com/Carson-Fricke/FCA
  publisher: FCA Thesis
  content_hash: 2cb3d15f1396ac2fed6bebcf1a518ecbec0bca5f1214af6ac7e69e3b4be34817
tags:
  - overfitting
  - selection-bias
  - purged-cross-validation
  - deflated-sharpe
  - multiple-testing
collections:
  - model-cards
  - inference
  - validation
links:
  - model-selection-by-hypothesis-shape
  - predictive-regression-and-lead-lag
  - time-series-momentum-and-trend
  - cross-sectional-momentum-and-rank-ic
  - tradability-break-even-and-oos
relationships:
  - type: relates
    target: model-selection-by-hypothesis-shape
  - type: relates
    target: predictive-regression-and-lead-lag
  - type: relates
    target: time-series-momentum-and-trend
  - type: relates
    target: cross-sectional-momentum-and-rank-ic
  - type: relates
    target: tradability-break-even-and-oos
trust:
  tier: curated
  state: published
---
# Overfitting and selection discipline

This card tests the research process, not the market: given how many
variants were tried, could the winner have passed by luck? An untracked
search cannot be corrected after the fact.

## In brief

- What it tests: whether the selected result survives the number of tries
  and the dependence in financial data.
- Intake: the complete attempt log — variants, windows, outcomes — plus a
  holdout that has never been examined.
- Key robustness: purged and embargoed cross-validation, PBO, deflated
  Sharpe, and reality-check family corrections.
- Main trap: leakage from preprocessing, overlapping labels, or a holdout
  quietly reused after a failed attempt.
- Require stronger evidence than \(t > 2\); \(t > 3\) is a working floor.

## What it tests

The selection-adjusted question: the maximum Sharpe or \(t\)-statistic
across \(N\) variants has a null distribution far wider than the single-
test distribution. With \(N\) independent trials the expected maximum of a
standard normal is roughly

\[
E\left[\max_{1 \le i \le N} z_i\right] \approx \sqrt{2\ln N}
\]

so the best of a thousand variants clears \(t > 3\) by chance alone at
conventional levels. The test asks whether the winner clears the right bar.

## Required data

The complete log of attempts: signal variants, windows, thresholds, cost
assumptions, and results, including failures. The exact count \(N\) is the
key input. An untouched holdout set, defined and reserved before the
search, is the strongest evidence.

## Simplest useful test

Evaluate the final rule on the reserved holdout and report the result once.
Then compute the probability of backtest overfitting with combinatorially
symmetric cross-validation, or the deflated Sharpe given \(N\), skew, and
kurtosis. If the holdout was opened, treat the result as in-sample.

## False explanations

- **Leakage.** Scaling, filtering, or feature selection done on the full
  sample feeds holdout information into training.
- **Overlapping labels.** Adjacent observations share outcomes; random
  folds train on the future of the validation point.
- **Researcher degrees of freedom.** Stop rules, exclusions, and metric
  choices made after seeing results.

## Statistical test

Use purged \(K\)-fold cross-validation with an embargo sized to the label
horizon, so training folds cannot see validation-adjacent observations.
Compute the probability of backtest overfitting from CSCV, the deflated
Sharpe with the trial count, and White Reality Check, Hansen SPA, or
Romano-Wolf stepdown. Report bootstrap intervals for the OOS Sharpe, and
publish \(N\): if it is unknown, no correction restores validity.

## Economic test

Selection-adjust the expected Sharpe, then apply costs: a Sharpe that only
exceeds zero before the deflation and cost haircut is not an edge. Report
turnover and break-even in the selected rule, and the cost at which the
deflated edge disappears.

## Failure conditions

- Observable: probability of backtest overfitting; condition: result
  robust; threshold: PBO above 0.5.
- Observable: deflated Sharpe; condition: edge survives selection;
  threshold: deflated Sharpe at or below zero.
- Observable: trial count; condition: correction feasible; threshold:
  \(N\) unrecorded or holdout already opened.
- Observable: OOS versus IS; condition: consistent performance; threshold:
  holdout Sharpe below half the in-sample Sharpe.
- Observable: label overlap; condition: clean validation; threshold:
  embargo shorter than the label horizon.

## Limitations

Corrections assume the trial count and dependence model are known, which is
rarely exactly true. PBO and deflated Sharpe are diagnostics, not proofs;
the durable defence is an untouched holdout and a log written first.
