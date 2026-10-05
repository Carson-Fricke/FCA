---
id: microstructure-false-positives
title: Microstructure false positives
version: 1
created: 2026-10-02
retrieved: 2026-10-02
source:
  url: https://github.com/Carson-Fricke/FCA
  publisher: FCA Thesis
  content_hash: 1ae52402b61dd74486dad8e9658db20092fd88f6e0f26eb6452c07c25c5ab25a
tags:
  - bid-ask-bounce
  - stale-prices
  - discretization
  - outliers
collections:
  - data-quality
  - inference
links:
  - discovery-study-design
  - return-continuation-and-reversal
  - trade-prices-versus-midquotes
  - cost-and-delay-sensitivity
  - roll-bid-ask-bounce
relationships:
  - type: relates
    target: discovery-study-design
  - type: relates
    target: return-continuation-and-reversal
  - type: relates
    target: trade-prices-versus-midquotes
  - type: relates
    target: cost-and-delay-sensitivity
  - type: relates
    target: roll-bid-ask-bounce
trust:
  tier: curated
  state: published
---
# Microstructure false positives

Short-horizon patterns are often artifacts of how prices are recorded, not
properties of the market. A few cheap checks catch most of them.

## In brief

- The four common causes: bounce, stale prices, discretization, and outliers.
- Each creates patterns that look like predictability.
- Run the cheap checks before treating a pattern as a finding.

## Bid-ask bounce

Trades alternate between bid and ask, so measured trade returns show
negative short-horizon correlation even when the efficient price is a random
walk. The Roll model gives the mechanical size of the effect. See
[Roll bid-ask bounce](roll-bid-ask-bounce). The fix is to measure on
midquotes when the question is about predictability.

## Stale prices

An illiquid instrument may repeat its last trade for long stretches. Those
repeated prices enter as zero returns and make the asset look calm and
predictable. Check the share of unchanged prices and the time since the last
trade. If most observations are stale, the study is about the recording
process, not the market.

## Discretization

Prices move in ticks. When a typical move is one or two ticks, the tick grid
shapes measured returns and can create apparent patterns at round numbers.
Compare the average move with the tick size before reading anything into the
shape of the data.

## One outlier

A single bad print can create an apparently strong effect. Compute the
result with the largest observation removed. If the result changes sign or
collapses, the finding was one observation. See [Return continuation and
reversal](return-continuation-and-reversal).

## Cheap checks

- Compare the mean with the median.
- Drop the largest 1% of observations and recompute.
- Repeat the study on midquotes.
- Compare the effect with the Roll bound for the measured spread.
- Slice by spread or volume and check whether thin slices carry the result.

If any check kills the effect, the original was likely an artifact. See
[Cost and delay sensitivity](cost-and-delay-sensitivity) and [Trade prices
versus midquotes](trade-prices-versus-midquotes).

## Related

- [Discovery study design](discovery-study-design)
- [Return continuation and reversal](return-continuation-and-reversal)
- [Trade prices versus midquotes](trade-prices-versus-midquotes)
- [Cost and delay sensitivity](cost-and-delay-sensitivity)
- [Roll bid-ask bounce](roll-bid-ask-bounce)
