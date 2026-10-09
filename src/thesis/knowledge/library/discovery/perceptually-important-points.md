---
id: perceptually-important-points
title: Perceptually important points
version: 2
created: 2026-10-06
retrieved: 2026-10-06
source:
  url: https://github.com/Carson-Fricke/FCA
  publisher: FCA Thesis
  content_hash: f15a9020a7d41cef319351274069b73ef7bdb7790d6dd18a7367de96cb1fc530
tags:
  - perceptually-important-points
  - pattern-extraction
  - segmentation
  - path-shape
  - time-series
  - model-card
collections:
  - discovery
  - model-cards
links:
  - executable-discovery-tool-bag
  - directional-change-events
  - return-continuation-and-reversal
  - signal-size-buckets
  - historical-timing-and-leakage
  - model-selection-by-hypothesis-shape
relationships:
  - type: relates
    target: directional-change-events
  - type: relates
    target: return-continuation-and-reversal
  - type: relates
    target: signal-size-buckets
  - type: relates
    target: historical-timing-and-leakage
  - type: relates
    target: model-selection-by-hypothesis-shape
trust:
  tier: curated
  state: published
---
# Perceptually important points

A learned representation is not the only way to summarize the shape of a
price path. Perceptually important points (PIPs) are a deterministic baseline
for shape extraction: repeatedly keep the point that matters most to the
current piecewise approximation. The output is a compact geometric summary of
a window, suitable as a conditioning feature for continuation or reversal
hypotheses.

## In brief

- What it builds: a small set of points that summarizes a window's shape.
- Baseline rule: iteratively select the point farthest from the current
  piecewise approximation in the time-price plane.
- Critical causal rule: full-window extraction is retrospective. Live
  features must extract PIPs from the available prefix only, and the time at
  which each point becomes decided must be declared.
- Main traps: endpoint anchors always win, results depend on the declared
  distance rule and scaling, and noise creates spurious points.
- Shape is a condition, not a direction: pair it with an explicit
  continuation or reversal rule.

## The method

Given a window of points \((t_i, p_i)\), start the approximation with the
window's first and last points. Then iterate:

1. For every segment between consecutive selected points, find candidate
   points and their distance from that segment.
2. Select the candidate with the largest distance and insert it.
3. Stop when the selected count reaches \(k\), or when the largest distance
   falls below a tolerance.

The distance definition is a declared choice, not a detail: vertical
distance (price distance at the point's time) and perpendicular distance to
the segment can select different points. Time scaling and price
normalization (raw, log, or z-scored) are equally consequential. The
stopping rule — a fixed count \(k\) or a distance tolerance — is another
searched parameter.

**Rolling windows and the causal rule.** PIP extraction over a window
\([t-W, t]\) uses the current endpoint \(t\) as an anchor, so a PIP computed
at \(t\) can be revised when new data arrives: a later point may be farther
from the approximation. Any live feature must therefore run on the available
prefix, and the study must declare when a point is treated as decided. A
backtest that extracts PIPs on the full window and then indexes features at a
PIP's own timestamp leaks the selection; see [Historical timing and
leakage](historical-timing-and-leakage).

Derived shape features include segment slopes and signs, the residual
distance to the approximation (a rough choppiness measure), the count of
direction changes, the time since the last PIP, and monotonicity indicators.

Alternatives when PIPs are not the right lens: turning-point or ZigZag
detectors, which behave like [Directional change
events](directional-change-events); piecewise-regression or change-point
segmentation; and fixed-lag slope features that require no selection at all.

## Simplest useful test

Declare the window length \(W\), the point count \(k\) (or tolerance), the
distance rule, and the normalization. Compute a small number of causal shape
features from prefix-only PIPs, split them into a few predeclared buckets,
and compare the mean forward return in each bucket with the unconditional
mean on the same window with a one-bar delay:

\[
E\left[ r_{t:t+h} \mid \text{shape bucket at } t \right]
\quad \text{versus} \quad E\left[ r_{t:t+h} \right].
\]

Bucket the feature once, before looking, using the discipline in [Signal
size buckets](signal-size-buckets), and connect the direction to whichever of
continuation or reversal is claimed in [Return continuation and
reversal](return-continuation-and-reversal). The SDK now exposes the
vertical-distance `building_blocks::perceptually_important_points` kernel.
Compose it in a trailing window and publish shape/event outputs using the
[Executable discovery tool bag](executable-discovery-tool-bag). The selected
earlier points remain retrospective; the shape is available only at window end.

## Statistical test

Use HAC standard errors or a block bootstrap for the bucket-versus-baseline
difference because forward windows overlap and volatility clusters. A time
shuffle destroys dependence, so prefer a block bootstrap or a
phase-randomized surrogate that preserves autocorrelation. Report every
bucket's count and interval, not the best one. Because \(k\), the distance
rule, and the normalization can each be varied, treat reparameterization as
a search: a shape effect that appears under only one configuration is a
selection result, not a finding (see [Exploratory search and multiple
attempts](exploratory-search-and-multiple-attempts)).

## Economic test

Shape conditioning changes when a position is taken, not whether it pays
costs. Rebuild the bucket comparison with a one-bar delay and the measured
spread, and report break-even spread and turnover. A shape feature that
selects rare windows produces few trades and wide uncertainty; the learned
or geometric representation must add value over the unconditional baseline
after these charges, or it is only a description of the past path.

## Limitations

- Endpoint bias: the first and last points of the window are always
  selected, and early iterations are anchored to them.
- Reparameterization sensitivity: distance rule, scaling, and \(k\) change
  the selected points.
- Noise: a single spike can win the distance contest and dominate the
  summary.
- Anchor selection: where the window starts and ends is part of the test,
  not an innocent choice.
- Description, not edge: PIPs summarize shape; a directional and economic
  hypothesis is still required.

## Related

- [Directional change events](directional-change-events)
- [Return continuation and reversal](return-continuation-and-reversal)
- [Signal size buckets](signal-size-buckets)
- [Historical timing and leakage](historical-timing-and-leakage)
- [Model selection by hypothesis shape](model-selection-by-hypothesis-shape)
