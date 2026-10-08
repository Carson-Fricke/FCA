---
id: time-to-crossover-statistics
title: Time-to-crossover statistics
version: 1
created: 2026-10-06
retrieved: 2026-10-06
source:
  url: https://github.com/Carson-Fricke/FCA
  publisher: FCA Thesis
  content_hash: e93ee05f9e6e595a8dcd01b22edb2a39adad48718037949f5a4ee4b3d1d407f3
tags:
  - duration-analysis
  - hazard
  - event-time
  - censoring
  - moving-average
  - model-card
collections:
  - discovery
  - model-cards
links:
  - time-series-momentum-and-trend
  - mean-reversion-half-life
  - directional-change-events
  - robust-autocorrelation-inference
  - model-selection-by-hypothesis-shape
relationships:
  - type: relates
    target: time-series-momentum-and-trend
  - type: relates
    target: mean-reversion-half-life
  - type: relates
    target: directional-change-events
  - type: relates
    target: robust-autocorrelation-inference
  - type: relates
    target: model-selection-by-hypothesis-shape
trust:
  tier: curated
  state: published
---
# Time-to-crossover statistics

Some hypotheses are about waiting, not direction: how long until a
fast/slow moving-average crossover, until a drawdown recovers, or until a
breakout follows through. The duration is the object under test, and the
central discipline is that non-events are right-censored, not dropped. This
card is a baseline duration view that pairs with a separate directional and
holding rule.

## In brief

- What it tests: the elapsed-time distribution of a named event, with
  non-events censored.
- Baseline event: a moving-average crossover, drawdown recovery, or level
  breakout, with the windows and detection rule stated exactly.
- Critical rule: right-censor every path that never reaches the event at the
  end of the study window; do not discard it.
- Main traps: the windows are searched parameters, many pairs multiply the
  comparisons, and a short waiting time is not a directional edge.
- Pair every duration claim with a direction and holding condition before
  any economic claim.

## The method

Define the event precisely before measuring. For a moving-average crossover:
the fast window \(f\), the slow window \(s\), whether the cross is detected
at a completed bar's close or intraday, and how ties are broken. For a
drawdown recovery: the peak definition and the recovery level. For a
breakout: the level rule and the confirmation rule. Then define the clock
start: the last opposite crossing, the drawdown peak, or the signal time.
The duration \(T\) is the elapsed time from the start until the event.

Two disciplines make the numbers meaningful:

- **Right-censoring.** Paths that have not reached the event by the end of
  the study window carry only the information that \(T\) exceeds the
  observed time. Dropping them biases the duration downward; include them as
  censored observations, and report the censoring rate.
- **Time scale.** Clock time, trading time (time in market, skipping
  overnight and weekend gaps), and event time (bar counts or number of
  segments) are different clocks. Declare which one \(T\) uses and how
  sessions and gaps are handled, because the choice changes the
  distribution. See [Directional change events](directional-change-events)
  for an event-time alternative.

Summarize the distribution with quantiles, not only the mean: median and
quartiles from a survival estimate such as Kaplan-Meier survive censoring,
while the sample mean of observed durations does not.

**Hazard and conditional duration.** The survival function \(S(t) = P(T >
t)\) and the hazard \(h(t) = P(T = t \mid T \ge t)\) frame the question as a
rate of arrival. A crossover rate that rises with elapsed time (a hazard that
increases) is a different claim from crossing times being memoryless, and
conditional duration models can include covariates such as volatility or
spread. State the model and check its proportionality or distributional
assumptions.

**Overlapping and dependent events.** Crossing episodes share paths and
calendar time: a second start can overlap the first event's waiting window,
and sequential durations within one series are dependent. Treat the sample
accordingly.

## Simplest useful test

Declare \(f\), \(s\), the direction rule, and the holding rule, then estimate
the duration distribution on one window with censoring. Compare the mean
forward return after a confirmed crossing with the unconditional baseline on
the same window, using a one-bar delay:

\[
E\left[ r_{t:t+h} \mid \text{crossing at } t \right]
\quad \text{versus} \quad E\left[ r_{t:t+h} \right].
\]

The durations alone do not say the sign: pair the wait with the direction of
the crossing and the hold condition in [Time-series momentum and
trend](time-series-momentum-and-trend), or test mean reversion after the
wait as in [Mean-reversion half-life](mean-reversion-half-life). State the
null explicitly — for example, that crossover waiting times match the
distribution generated on a block-bootstrapped series with the same serial
dependence. Tracking event starts, censoring, and survival summaries
requires authoring custom study code today; the knowledge cards describe the
method, not a platform primitive.

## Statistical test

Estimate survival and quantiles with Kaplan-Meier and Greenwood or bootstrap
intervals, and report the censoring rate alongside. Compare groups (for
example calm versus stressed windows, or up-crossings versus down-crossings)
with a log-rank test, and use a block bootstrap over the series for
dependent events (see [Robust autocorrelation and variance-ratio
inference](robust-autocorrelation-inference)). For conditional
duration models, report coefficient uncertainty with a robust covariance and
check the distributional assumption. Because the moving-average pair is a
searched choice, count the window pairs tried and apply selection discipline
([Exploratory search and multiple
attempts](exploratory-search-and-multiple-attempts)); a
selection-adjusted test is required when many pairs were compared.

## Economic test

Waiting time sets exposure and turnover. If a crossing is rare, a strategy
conditioned on it trades rarely, and its per-trade gross edge must clear the
round-trip spread, fees, and one bar of delay on every flip: report the
break-even spread, crossings per year, and capacity. Waiting-time statistics
alone are not a directional edge — a series can cross quickly and then
whipsaw — so charge costs on the paired direction and holding rule, not on
the duration. A tradable candidate needs the duration to shorten the
exposure without destroying the gross move.

## Limitations

- Window selection: \(f\) and \(s\) (or the peak, level, and threshold
  rules) are searched parameters.
- Multiple comparisons: every pair of windows is another attempt.
- Censoring: a high censoring rate leaves the tail poorly identified.
- Dependency: overlapping episodes and serial dependence violate
  independence-based tests.
- Regimes: duration distributions shift with volatility, so pooling
  regimes can average different processes.
- No direction: duration is a property of arrival, not of the next return.

## Related

- [Time-series momentum and trend](time-series-momentum-and-trend)
- [Mean-reversion half-life](mean-reversion-half-life)
- [Directional change events](directional-change-events)
- [Robust autocorrelation and variance-ratio inference](robust-autocorrelation-inference)
- [Model selection by hypothesis shape](model-selection-by-hypothesis-shape)
