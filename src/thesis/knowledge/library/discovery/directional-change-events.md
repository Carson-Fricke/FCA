---
id: directional-change-events
title: Directional change events
version: 2
created: 2026-10-06
retrieved: 2026-10-06
source:
  url: https://github.com/Carson-Fricke/FCA
  publisher: FCA Thesis
  content_hash: 45147302ef28756cff8462d962bfc6aba0fc3c76390438df09ba4dd44d0c3168
tags:
  - directional-change
  - event-time
  - overshoot
  - trend
  - segmentation
  - model-card
collections:
  - discovery
  - model-cards
links:
  - executable-discovery-tool-bag
  - return-continuation-and-reversal
  - time-series-momentum-and-trend
  - stability-across-periods
  - exploratory-search-and-multiple-attempts
  - model-selection-by-hypothesis-shape
relationships:
  - type: relates
    target: return-continuation-and-reversal
  - type: relates
    target: time-series-momentum-and-trend
  - type: relates
    target: stability-across-periods
  - type: relates
    target: exploratory-search-and-multiple-attempts
  - type: relates
    target: model-selection-by-hypothesis-shape
trust:
  tier: curated
  state: published
---
# Directional change events

An event-time clock advances only when the market acts, not when ordinary
wall-clock time passes. A directional change (DC) event is the simplest
such clock: it is a segmentation rule that fires when price travels a
declared threshold from the last confirmed extreme. DC events are one
baseline building block among several, and this card separates the
segmentation rule from any later directional hypothesis.

## In brief

- What it builds: an event-time segmentation of one price path.
- Baseline rule: a DC event is confirmed when price moves a threshold
  \(\theta\) away from the last confirmed extremum.
- Critical causal point: the extremum precedes confirmation, but the
  threshold move is only observable at confirmation; live features and fills
  must be timed from confirmation, never from the retrospective extremum.
- Main traps: the threshold is a searched parameter, confirmation lag eats
  part of every move, and quiet markets produce very few events.
- Overshoot and duration are summaries of the path, not directional edges.

## The method

Fix a threshold rule first. The simplest is a constant price threshold
\(\theta > 0\). Track the running maximum (for a down event) or minimum (for
an up event) since the last confirmed event. A down DC event is confirmed at
the first time \(t\) with

\[
\max_{s \le t} p_s - p_t \ge \theta .
\]

An up DC event is the mirror image. At confirmation, the running extreme is
recorded as the event's extremum; its timestamp lies in the past, but it was
observable as a running value, so duration and overshoot can be computed
causally at confirmation. What is retrospective is the claim that this
extreme is *the* turning point: later price can always exceed it. Backtests
that index features at the extremum timestamp instead of the confirmation
timestamp leak future information; see [Historical timing and leakage](historical-timing-and-leakage).

Common threshold variants, each a declared choice:

- **Volatility-scaled:** \(\theta_t = k\,\hat\sigma_t\), with \(\hat\sigma_t\)
  from a trailing realized-volatility or range estimator, using only data
  available at \(t\).
- **Asymmetric:** a larger \(\theta_{\text{up}}\) than
  \(\theta_{\text{down}}\), or the reverse, when the hypothesis says up and
  down moves differ.

The event sequence yields summaries: the distribution of overshoot beyond
\(\theta\), the elapsed clock or trading time between events, and counts by
direction. These describe the path. Any link to future returns requires a
separate directional hypothesis and a separate test.

Alternatives worth trying rather than assuming DC is the right segmentation:
fixed-lag returns, [Perceptually important points](perceptually-important-points),
change-point or piecewise-regression segmentation, and plain
moving-average-state rules in [Time-series momentum and
trend](time-series-momentum-and-trend).

## Simplest useful test

Declare the threshold rule, the direction of events, the forward horizon
\(h\), and a one-bar execution delay *before* looking. Then compare the mean
forward return after confirmed events with the unconditional mean forward
return over all bars on the same instrument and window:

\[
E\left[ r_{t:t+h} \mid \text{DC confirmed at } t \right]
\quad \text{versus} \quad E\left[ r_{t:t+h} \right].
\]

Run the comparison separately for up and down events, because continuation
after ups and reversal after downs are different hypotheses; connect the
result to [Return continuation and
reversal](return-continuation-and-reversal). This baseline can be expressed
as a condition on bars once event times are known, but detecting events,
tracking confirmation timestamps, and building duration or overshoot
summaries is supported by the SDK's `building_blocks::DirectionalChange`.
Its absolute-threshold events carry extreme and confirmation timestamps.
Compose and publish it as shown in the
[Executable discovery tool bag](executable-discovery-tool-bag). Volatility-scaled
and asymmetric variants require explicit authored detectors.

## Statistical test

Event times are sparse and dependent, and they inherit volatility
clustering, so an iid permutation of returns is the wrong null. Build an
event-time null that preserves serial dependence: block-bootstrap the return
series, re-run the same detector on each resampled path, and compare the
observed event-conditioned return with the resampled distribution, matched
on event count. Report the number of events, because a mean over a handful
of events has a wide interval. Use HAC inference for overlapping forward
returns, and compare against the unconditional baseline rather than zero.
Any threshold sweep is a search: count the thresholds tried and apply
selection-adjusted inference before believing the best one; see
[Exploratory search and multiple attempts](exploratory-search-and-multiple-attempts).

## Economic test

Confirmation lag is the cost that DC events add by construction: the move
from the extremum to confirmation has already happened when the event fires.
Charge the measured spread, fees, and at least one bar of delay from the
confirmation time, and report the break-even move versus \(\theta\),
turnover (events per year), and the planned holding horizon. Sparse events
mean few independent bets, so capacity and sample size matter (see
[Stability across periods](stability-across-periods)). A positive mean over
a few large moves is not a tradable edge until it survives these charges.

## Limitations

- Threshold choice and search: every \(\theta\) rule tried is an attempt.
- Confirmation lag: the detector is late by design, and latency is worse in
  fast reversals.
- Path dependence: event timing depends on the detector's start and the
  entire preceding path, not just current price.
- Sparse, overlapping events: quiet regimes give few observations, and
  forward windows overlap.
- Description, not edge: DC events segment the path; they do not say the
  next move continues or reverses until a directional test says so.

## Related

- [Return continuation and reversal](return-continuation-and-reversal)
- [Time-series momentum and trend](time-series-momentum-and-trend)
- [Stability across periods](stability-across-periods)
- [Exploratory search and multiple attempts](exploratory-search-and-multiple-attempts)
- [Model selection by hypothesis shape](model-selection-by-hypothesis-shape)
