---
id: event-studies
title: Event studies
version: 1
created: 2026-10-02
retrieved: 2026-10-02
source:
  url: https://github.com/Carson-Fricke/FCA
  publisher: FCA Thesis
  content_hash: 8f2e2ff3290ec69ba813acf20b11089536fe533d403e712e0bae038b895edc0e
tags:
  - event-study
  - abnormal-return
  - car
  - cross-sectional-dependence
  - placebo
collections:
  - model-cards
links:
  - model-selection-by-hypothesis-shape
  - intraday-seasonality
  - point-in-time-data-validity
relationships:
  - type: relates
    target: model-selection-by-hypothesis-shape
  - type: relates
    target: intraday-seasonality
  - type: relates
    target: point-in-time-data-validity
trust:
  tier: curated
  state: published
---
# Event studies

The claim is that a defined event moves the price: an announcement, an index
change, an earnings date, a regulatory decision. The test measures the
return around the event against what the price would have done otherwise.

## In brief

- What it tests: abnormal return in a window around a dated event.
- Intake: a clean event list with exact timestamps plus returns and a
  benchmark for the affected securities.
- Key robustness: events cluster in calendar time, so cross-sectional
  dependence must be corrected or bootstrapped.
- Main trap: event-window leakage — a window that starts before the
  timestamp captures the move you claim to predict.
- Placebo events are part of the test, not an optional extra.

## What it tests

The market-model abnormal return

\[
AR_{i,t} = r_{i,t} - \left(\hat\alpha_i + \hat\beta_i\, r_{m,t}\right)
\]

estimated on a window outside the event, cumulated over the event window:

\[
CAR_i = \sum_{t \in \text{window}} AR_{i,t}
\]

The claim is that the average CAR differs from zero and that the direction
matches the hypothesis.

## Required data

Event dates with exact timestamps and timezone, a return series for each
affected security, and a benchmark index. The event list must be
point-in-time: events selected by outcome bias the result. Intraday timing
matters when the event is announced during the session: the first tradable
price after the timestamp defines the start.

## Simplest useful test

Estimate the market model on a pre-event window, compute the CAR over a
short declared event window beginning at the first tradable timestamp, and
report the cross-sectional mean with a clustered or bootstrap standard
error. Repeat with placebo dates drawn from non-event days; the real events
must stand out from the placebos.

## False explanations

- **Confounding events.** Earnings, dividends, and macro releases cluster;
  the window captures more than the named event.
- **Leakage.** Announcement times leak; a window starting before the event
  measures anticipation.
- **Cross-sectional dependence.** Overlapping calendar events correlate
  individual CARs; IID cross-sectional errors overstate significance.
- **Drift.** The benchmark model ignores size, value, and momentum.
- **Selection.** Events chosen after seeing returns, or a window chosen for
  the best result.

## Statistical test

Use the standardized cross-sectional test of Boehmer, Musumeci, and Poulsen,
or the Kolari-Pynnönen correction for cross-sectional dependence, and
report a bootstrap that resamples event days. For clustered events use a
calendar-time portfolio with HAC inference. Declare the event and
estimation windows before looking, and apply a multiple-testing correction
when many event types or windows were examined. Placebo events with
shuffled dates give the null distribution.

## Economic test

A significant CAR is not a trade unless the reaction is capturable after
the event is public. Report the return from the first tradable timestamp
onward, charge spread, fees, and delay, and measure turnover. Event windows
have wider spreads and higher volatility; costs measured on ordinary days
understate them.

## Failure conditions

- Observable: placebo CAR; condition: event reaction real; threshold:
  event mean inside the placebo distribution.
- Observable: pre-event window; condition: reaction not anticipation;
  threshold: significant CAR before the timestamp.
- Observable: clustered standard error; condition: significance; threshold:
  IID error significant but clustered interval includes zero.
- Observable: tradable window return; condition: net edge; threshold:
  post-timestamp return below cost plus delay.
- Observable: event count; condition: enough events; threshold: sample
  below the declared minimum or dominated by one name.

## Limitations

Event studies cannot establish causation when event timing is endogenous,
and the economic window is usually much shorter than the statistical one.
