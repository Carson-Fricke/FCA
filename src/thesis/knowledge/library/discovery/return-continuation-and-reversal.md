---
id: return-continuation-and-reversal
title: Return continuation and reversal
version: 4
created: 2026-10-02
retrieved: 2026-10-12
source:
  url: https://github.com/Carson-Fricke/FCA
  publisher: FCA Thesis
  content_hash: 13e8700550e07606603aa178fbb7a317e9c1f84022cea2ef38e30777b097e80d
tags:
  - return-predictability
  - continuation
  - mean-reversion
  - model-card
  - conditioning-comparison
  - between-group-contrast
  - incremental-value
  - inconclusive-evidence
collections:
  - discovery
  - model-cards
links:
  - from-market-idea-to-trading-construction
  - developing-a-candidate-through-evidence
  - investigating-representations-and-episodes
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
    target: investigating-representations-and-episodes
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

A useful component question is whether a recent move predicts the next move.
Continuation repeats the move; reversal undoes it. Start with the trading
construction and the decision this measurement will inform; this baseline is
not a prerequisite that every proposed strategy must pass.

## The measurement

Let \(r_{t-k:t}\) be the return over the prior window and \(r_{t:t+h}\) the
return over the next window. Estimate

\[ r_{t:t+h} = \alpha + \beta\, r_{t-k:t} + \varepsilon_t \]

A positive \(\beta\) is continuation; a negative \(\beta\) is reversal.
Report \(\beta\) with a robust standard error and the residual spread.
Declare the windows and horizon before looking.

Other representations include [event-time moves](directional-change-events),
[path shape](perceptually-important-points), [duration states](time-to-crossover-statistics)
and [learned representations](autoencoder-anomaly-detection). Choose by intended
role and [representation fitness](investigating-representations-and-episodes).

## Buckets make the shape visible

A single \(\beta\) hides the shape. Split the prior return into a few
predeclared groups: large down, small down, small up, large up. For each
group, report the average later return, the count, and an uncertainty
interval. See [Signal size buckets](signal-size-buckets).

## What this test does and does not evaluate

A daily momentum bucket followed by an unconditional five-minute return does
not evaluate a pivot-reclaim setup with selective entries, structural invalidation
and a different holding policy. If those decisions matter, investigate the setup
occurrences and their paths. A weak baseline may motivate a better representation
or challenge one component; it does not refute an omitted composition. A favorable
bucket does not validate that composition either. See
[From market idea to trading construction](from-market-idea-to-trading-construction).

## Worked conditioning comparison

Suppose a hypothetical study uses one setup outcome per session, with 20 sessions
in each momentum group. It reports these means and dependence-aware 95% intervals
for the same trigger and horizon. The figures illustrate reasoning, not market
findings or a prescribed estimator.

| Quantity | Estimate, bps | Interval, bps |
| --- | ---: | --- |
| All setup sessions | +12 | [+1, +23] |
| Momentum aligned | +10 | [-5, +25] |
| Momentum conflicting | +14 | [-3, +31] |
| Aligned minus conflicting | -4 | [-27, +19] |

The pooled interval excludes zero; neither subgroup interval does. This does not
establish separation or absence of useful conditioning. Each subgroup result asks
about its own mean relative to zero. The contrast estimates their difference;
here its uncertainty admits economically different conclusions. Do not reconstruct
contrast uncertainty by comparing p-values or interval overlap.

Three questions lead to different investigations:

- **Within a condition:** what outcome follows this setup when momentum is aligned?
- **Between conditions:** how do aligned and conflicting setup outcomes differ,
  for the intended population, weighting and other relevant context?
- **Incremental construction value:** what changes when the proposed policy uses
  momentum, including occurrence count, hypothetical entry/exit paths and costs?

A group contrast alone may not answer the policy question. Report the actual
comparison, effect, uncertainty and independent support. When observations share
sessions or episodes, preserve that dependence in estimation. Separate filters do
not evaluate their joint interaction. First occurrence per session also differs
from averaging every occurrence within each session; name the actual construction.

## Decide what the evidence permits

In the example, a simpler unconditioned variant may be practical, while momentum's
incremental usefulness remains unresolved. A more consequential question can take
priority, but meaningful parameter and interaction exploration belongs in discovery. To argue that useful benefit
is excluded, specify what size matters and examine whether the relevant contrast's
uncertainty excludes it. Sparse support or non-significance alone cannot do this.
Scope adverse conclusions to the tested representation, setup and sample. Use the
[worked research loop](developing-a-candidate-through-evidence) to connect the
finding to the next decision, preserving the search history and remaining unknowns.

## Microstructure alternatives

- Small reversals are common and usually come from the bid-ask spread:
  trades bounce between bid and ask, so the next trade looks like a
  correction. See [Trade prices versus midquotes](trade-prices-versus-midquotes).
- A real reversal must exceed what the spread mechanism explains. See
  [Microstructure false positives](microstructure-false-positives).
- Evaluate continuation over the actual holding policy after costs; a pooled bar result cannot settle it.

## Report effect, count, uncertainty, and spread

Every bucket needs all four. A large average on a small sample may be uncertain;
thousands of overlapping observations may contain little independent evidence.
Include spread, [market controls](market-adjusted-returns) and
[stability across periods](stability-across-periods) where relevant.

## Costs and delay

Rebuild the relevant measurement with causal entry timing, plausible delay and
declared costs. If its economics fail, record that result for the tested
construction; failure alone does not establish the cause or reject all alternative
setups. See [Cost and delay sensitivity](cost-and-delay-sensitivity) and
[Simple trading economics](simple-trading-economics).

## Related

- [Discovery study design](discovery-study-design)
- [Developing a candidate through evidence](developing-a-candidate-through-evidence)
- [Robust autocorrelation and variance-ratio inference](robust-autocorrelation-inference)
- [Exploratory search and multiple attempts](exploratory-search-and-multiple-attempts)

## Develop the policy before judging the feature

Explore useful momentum lookbacks and thresholds inside the requested composed
strategy. Test regime switches, exits and abstention as decisions, not only as return
buckets. Keep all attempts. Compare incremental after-cost policy value and then
assess robustness/selection uncertainty; do not require every baseline to be significant.
