---
id: structural-breaks-and-regime-tests
title: Structural breaks and regime tests
version: 1
created: 2026-10-02
retrieved: 2026-10-02
source:
  url: https://github.com/Carson-Fricke/FCA
  publisher: FCA Thesis
  content_hash: 44b3482d5ca75597e834fbef1d7929ae99a5eb167c04fb27f2150c90bf96c1b8
tags:
  - structural-breaks
  - chow-test
  - bai-perron
  - cusum
  - walk-forward
collections:
  - model-cards
links:
  - model-selection-by-hypothesis-shape
  - mean-reversion-half-life
  - overfitting-and-selection-discipline
  - point-in-time-data-validity
relationships:
  - type: relates
    target: model-selection-by-hypothesis-shape
  - type: relates
    target: mean-reversion-half-life
  - type: relates
    target: overfitting-and-selection-discipline
  - type: relates
    target: point-in-time-data-validity
trust:
  tier: curated
  state: published
---
# Structural breaks and regime tests

The claim is that a relationship changed at some point: a beta shifted, a
mean moved, a spread stopped working. Testing for breaks is how a result
earns the right to be applied out of sample.

## In brief

- What it tests: whether model parameters are constant over the sample.
- Intake: a long clean series covering the candidate break date and both
  regimes, with bad prints already handled.
- Key robustness: unknown break dates searched over the sample need
  sup-statistics with bootstrap critical values.
- Main trap: a single bad tick, halt, or heteroskedasticity shift looks
  exactly like a structural break.
- Walk-forward evaluation is the operational form of the test.

## What it tests

Parameter constancy against a one-time shift, or against an unknown number
of shifts:

\[
y_t = x_t'\beta_1 + \varepsilon_t \ (t \le \tau), \qquad
y_t = x_t'\beta_2 + \varepsilon_t \ (t > \tau)
\]

A Chow test evaluates a known \(\tau\); Bai-Perron sup-\(F\), UDmax, and
WDmax statistics locate unknown breaks. The economic question is whether a
trading rule knows about the break in real time.

## Required data

A long, clean series covering the candidate break and enough observations
on both sides. Data problems must be resolved first: splits, symbol
changes, missing sessions, and outliers can create or hide breaks. When the
break is tied to an event, the event date is input, not output.

## Simplest useful test

Run a Chow test at the declared date on a declared regression, then a CUSUM
of recursive residuals over the whole sample, and finally a Bai-Perron scan
with bootstrap critical values when the date is unknown. Report every
candidate break with its confidence interval, and re-estimate on both sides
to show what changed.

## False explanations

- **Bad data.** One limit move or stale print is a break in a small window.
- **Heteroskedasticity.** A volatility shift changes \(t\)-statistics and
  can mimic a parameter break.
- **Searched dates.** Testing many candidate dates inflates the maximum
  statistic; critical values must account for the search.
- **Hindsight regime labels.** A regime named after the fact cannot be
  traded in real time.
- **Coincident events.** Two changes at once leave the cause unidentified.
- **Nonlinearity.** A smooth transition or threshold looks like a sharp
  break under linear tests.

## Statistical test

Use sup-\(F\) or UDmax/WDmax with bootstrap critical values for unknown
breaks, and CUSUM or fluctuation tests as stability complements. Report
break-date confidence intervals and the size of the parameter change, not
just its significance. Test residual variance stability before reading a
mean break. Then walk forward: estimate on a rolling window, apply to the
next period, and compare with the full-sample model. The rolling window is
a declared researcher choice.

## Economic test

A regime rule only earns if the break is detectable early enough to act on.
Charge the costs of switching, the delay in detection, and the turnover of
exiting and re-entering. Report the loss from false detections and the
break-even detection lag at which the rule stops paying. Most claimed
regime edges die from detection lag and whipsaw, not from the statistics.

## Failure conditions

- Observable: break-date interval; condition: identifiable break;
  threshold: interval wider than the declared action window.
- Observable: sup-\(F\) p-value; condition: break exists; threshold:
  bootstrap p-value above the declared level.
- Observable: detection lag; condition: tradable regime; threshold:
  realized detection delay beyond break-even.
- Observable: data cleaning; condition: genuine break; threshold: break
  disappears after correcting flagged prints.
- Observable: walk-forward return; condition: model improvement;
  threshold: no out-of-sample gain over the constant model.

## Limitations

Break tests have low power near the sample end, exactly where live
decisions occur, and searching for breaks is itself a source of false positives.
