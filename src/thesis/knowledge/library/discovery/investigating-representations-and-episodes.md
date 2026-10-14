---
id: investigating-representations-and-episodes
title: Investigating representations through episodes
version: 2
created: 2026-10-11
retrieved: 2026-10-12
source:
  url: https://github.com/Carson-Fricke/FCA
  publisher: FCA Thesis
  content_hash: 8b48a3279f02391a68a3027a6dc21db0431537a7396f1a899008c812346364ef
tags:
  - discovery
  - trend-detection
  - episodes
  - duration
  - multi-timeframe
  - causal-timing
  - momentum-representation
  - regime-conditioning
  - proxy-fitness
collections:
  - discovery
links:
  - from-market-idea-to-trading-construction
  - developing-a-candidate-through-evidence
  - return-continuation-and-reversal
  - historical-timing-and-leakage
  - directional-change-events
  - publishing-research-exhibits
relationships:
  - type: relates
    target: return-continuation-and-reversal
  - type: relates
    target: historical-timing-and-leakage
trust:
  tier: curated
  state: published
---
# Investigating representations through episodes

A weak prediction from an unsuitable measurement does not settle the underlying
idea. Before interpreting predictive results, inspect whether the representation
captures the phenomenon and whether it becomes available in time to act.

## Recognition and prediction are different questions

For week-to-month daily trends, five-day momentum sign measures recent direction;
efficiency ratio measures path efficiency, not linearity itself. Neither is
automatically a detector of the episodes an operator means. Compare plausible
representations by their behavior: missed episodes, false detections, fragmentation,
confirmation delay and sensitivity to volatility. Recognizing a visually clear
trend retrospectively does not prove predictable continuation after confirmation.

Publish daily price, detector state, direction, scale and availability alongside
episode records. Inspect examples using a declared selection rule spanning early,
middle and late development periods, successful and failed detections, rather than
only striking trends. Record disagreement with visual interpretation; inspection
helps diagnose representation, but is not independent statistical confirmation.

## Worked example: momentum and regime proxy fitness

Suppose the idea is to trade renewed short-term momentum after a pullback within
a persistent slower trend. Five-day return sign supplies recent daily direction;
it leaves slower trend strength, intraday retracement and reacceleration unmeasured.
A weak outcome conditional on that sign answers a narrow proxy question.

Inspect causally available states alongside price paths using a declared sample
selection rule. Suppose the sign flips within sustained advances and treats a
single large gap as equivalent to persistent movement. The next decision is whether
a representation of strength and persistence better supplies the intended context.
Completed slower-bar volatility-scaled movement and a faster pullback/turn state
are possible alternatives, chosen for this mismatch, not a required recipe. Record
the reason for revision before examining its predictive outcomes.

Regime representation fitness depends on the intended role, too.
An efficiency-ratio threshold measures path efficiency; it may omit the volatility
or transition behavior that motivated regime conditioning. Inspect whether the state
differentiates that behavior before interpreting setup outcomes. A more complex
detector still needs this check.

If a suitable representation captures the phenomenon, investigate the relevant
setup comparison and uncertainty; see the [worked conditioning comparison](return-continuation-and-reversal).
If there are too few independent episodes, leave incremental usefulness unresolved.
It can be reasonable to simplify the construction or stop within budget. Neither
requires exhausting every representation or declaring the underlying idea absent.

## Define the episode clock causally

Distinguish the retrospective start, first confirmation, current age and termination
rule. The first confirmation is the earliest possible decision time. A later
termination can supply an outcome label; it cannot become an earlier feature.
Estimate remaining duration from information available at the current age, not
from the episode's eventual length. Include still-open episodes as censored rather
than silently dropping them. Rolling observations within the same episode are
dependent; many daily or intraday rows may represent few independent episodes.

Possible questions include continuation probability over a declared horizon,
remaining-duration distribution, or termination risk conditional on age and state.
Use simple controls and report uncertainty and episode counts. A six-month sample
may contain too few long trends for a useful duration model; report that limitation
rather than extending the operator's window or declaring absence of persistence.

## Preserve the conditioning direction

"How does daily trend state condition overnight moves?" asks about future
close-to-next-open outcomes given a state known at the preceding close.
"Does the overnight gap predict today's intraday return?" asks a different
question, using information available at the open. Neither replaces the other.

For intraday conditioning, use the latest completed daily state available before
the trigger. Decompose daily movement into overnight and regular-session pieces
with explicit session boundaries and comparable price adjustment conventions.
Compare observations at similar episode ages when that matters. Broad open-to-close
averages do not evaluate selective intraday triggers or path-dependent exits.

## Diagnose before concluding

Check joined timestamps and a few underlying rows before celebrating large
effects. A prior close accidentally taken from the current session can create
algebraic relationships. If timing or adjustment is wrong, audit every finding
that depends on the affected inputs; unaffected facets require verification.

An investigation may justify replacing a detector, refining a setup, or leaving
the question inconclusive. It does not require searching until a positive result
appears. See [Developing a candidate through evidence](developing-a-candidate-through-evidence)
for the decision loop and [Publishing research exhibits](publishing-research-exhibits)
for supported evidence shapes. Consult the SDK for actual callable capabilities.
