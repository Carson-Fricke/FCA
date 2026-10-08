---
id: return-continuation-and-reversal
title: Return continuation and reversal
version: 1
created: 2026-10-02
retrieved: 2026-10-02
source:
  url: https://github.com/Carson-Fricke/FCA
  publisher: FCA Thesis
  content_hash: 314d172a3da7c207e502772d7bf5caf9d714f69b1a4f1f43644c01aeb4542328
tags:
  - return-predictability
  - continuation
  - mean-reversion
  - model-card
collections:
  - discovery
  - model-cards
links:
  - discovery-study-design
  - signal-size-buckets
  - trade-prices-versus-midquotes
  - market-adjusted-returns
  - cost-and-delay-sensitivity
  - stability-across-periods
  - microstructure-false-positives
  - exploratory-search-and-multiple-attempts
  - simple-trading-economics
  - robust-autocorrelation-inference
  - directional-change-events
  - perceptually-important-points
  - time-to-crossover-statistics
  - autoencoder-anomaly-detection
relationships:
  - type: relates
    target: discovery-study-design
  - type: relates
    target: signal-size-buckets
  - type: relates
    target: trade-prices-versus-midquotes
  - type: relates
    target: market-adjusted-returns
  - type: relates
    target: cost-and-delay-sensitivity
  - type: relates
    target: stability-across-periods
  - type: relates
    target: microstructure-false-positives
  - type: relates
    target: exploratory-search-and-multiple-attempts
  - type: relates
    target: simple-trading-economics
  - type: relates
    target: robust-autocorrelation-inference
  - type: relates
    target: directional-change-events
  - type: relates
    target: perceptually-important-points
  - type: relates
    target: time-to-crossover-statistics
  - type: relates
    target: autoencoder-anomaly-detection
trust:
  tier: curated
  state: published
---
# Return continuation and reversal

The simplest discovery question is whether a recent move predicts the next
move. Continuation repeats the move; reversal undoes it.

## In brief

- Measure the later return as a function of the prior return.
- This fixed-window regression is the simplest baseline, not the limit of
  the search space; event-time, path-shape, duration, and learned-
  representation framings are alternatives to try.
- Use a few predeclared buckets, not a search over thresholds.
- Report effect, count, uncertainty, and spread for every bucket.
- A clean reversal is often a spread bounce, not a real forecast.

## The measurement

Let \(r_{t-k:t}\) be the return over the prior window and \(r_{t:t+h}\) the
return over the next window. Estimate

\[ r_{t:t+h} = \alpha + \beta\, r_{t-k:t} + \varepsilon_t \]

A positive \(\beta\) is continuation; a negative \(\beta\) is reversal.
Report \(\beta\) with a robust standard error and the residual spread.
Declare the windows and horizon before looking.

This clock-time regression is the simplest baseline. The conditioning
variable does not have to be a fixed prior-window return: event-time moves
([Directional change events](directional-change-events)), path-shape
summaries ([Perceptually important
points](perceptually-important-points)), waiting-time or duration states
([Time-to-crossover statistics](time-to-crossover-statistics)), and learned
representations of a window
([Autoencoder anomaly detection](autoencoder-anomaly-detection)) are
alternative building blocks. Try them as additional baselines rather than
treating one clock slicing as the only measurement.

## Buckets make the shape visible

A single \(\beta\) hides the shape. Split the prior return into a few
predeclared groups: large down, small down, small up, large up. For each
group, report the average later return, the count, and an uncertainty
interval. See [Signal size buckets](signal-size-buckets).

## What usually shows up

- Small reversals are common and usually come from the bid-ask spread:
  trades bounce between bid and ask, so the next trade looks like a
  correction. See [Trade prices versus midquotes](trade-prices-versus-midquotes).
- A real reversal must exceed what the spread mechanism explains. See
  [Microstructure false positives](microstructure-false-positives).
- Continuation over minutes and hours is rare after costs.

## Report effect, count, uncertainty, and spread

Every bucket needs all four. A large average on 30 observations is not
evidence; a tight interval on thousands is a candidate. Include the spread
so the reader sees how much of the effect is friction. See [Market-adjusted returns](market-adjusted-returns) and [Stability across periods](stability-across-periods).

## Costs and delay

Rebuild the test with a one-bar execution delay and a small cost. If the
effect vanishes, it was a friction artifact. See [Cost and delay sensitivity](cost-and-delay-sensitivity) and [Simple trading economics](simple-trading-economics).

## Related

- [Discovery study design](discovery-study-design)
- [Signal size buckets](signal-size-buckets)
- [Trade prices versus midquotes](trade-prices-versus-midquotes)
- [Market-adjusted returns](market-adjusted-returns)
- [Cost and delay sensitivity](cost-and-delay-sensitivity)
- [Stability across periods](stability-across-periods)
- [Microstructure false positives](microstructure-false-positives)
- [Exploratory search and multiple attempts](exploratory-search-and-multiple-attempts)
- [Simple trading economics](simple-trading-economics)
- [Robust autocorrelation and variance-ratio inference](robust-autocorrelation-inference)
- [Directional change events](directional-change-events)
- [Perceptually important points](perceptually-important-points)
- [Time-to-crossover statistics](time-to-crossover-statistics)
- [Autoencoder anomaly detection](autoencoder-anomaly-detection)
