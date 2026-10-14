---
id: cost-and-delay-sensitivity
title: Cost and delay sensitivity
version: 2
created: 2026-10-02
retrieved: 2026-10-11
source:
  url: https://github.com/Carson-Fricke/FCA
  publisher: FCA Thesis
  content_hash: ed2b6f332ed312a0f0d1a48a9fe3997facf9e9e4983999e742dc1efe4433dbe9
tags:
  - transaction-costs
  - execution-delay
  - break-even
  - sensitivity
collections:
  - discovery
  - trading-costs
links:
  - developing-a-candidate-through-evidence
  - discovery-study-design
  - return-continuation-and-reversal
  - trade-prices-versus-midquotes
  - microstructure-false-positives
  - simple-trading-economics
  - data-cost-planning
  - tradability-break-even-and-oos
relationships:
  - type: relates
    target: discovery-study-design
  - type: relates
    target: return-continuation-and-reversal
  - type: relates
    target: trade-prices-versus-midquotes
  - type: relates
    target: microstructure-false-positives
  - type: relates
    target: simple-trading-economics
  - type: relates
    target: data-cost-planning
  - type: relates
    target: tradability-break-even-and-oos
trust:
  tier: curated
  state: published
---
# Cost and delay sensitivity

A gross effect is not a net effect. Small costs and one bar of delay change
the answer more often than most other study choices.

## In brief

- Investigate candidate economics with plausible costs and causal execution delay.
- Compute the break-even cost: the cost where the net effect is zero.
- If relevant economics fail, record which construction and assumptions failed.
- Cost assumptions belong in the study log, not hidden in the conclusion.

## Add cost and delay

First specify the opportunity, entry eligibility, invalidation and holding/exit
policy being investigated. A pooled forward-bar average is not an estimate of a
selective setup's entry-to-exit outcome. Observation-only studies can inspect
hypothetical paths with explicit fill assumptions; they do not establish executed
PnL or replace later strategy backtests.

Charge costs on hypothetical round trips and compare plausible execution delays.
One bar is a useful sensitivity case, not a universal latency assumption across
different timeframes and tick streams. Ensure the signal is observable before
the assumed entry price:

\[
\text{net} = \text{gross} - \text{cost per round trip}
\]

Tabulate the net effect for a few predeclared cost levels, such as zero,
half a tick, one tick, and two ticks, and for delays of zero, one, and two
bars. A table shows sensitivity better than a single number.

## Break-even cost

The break-even cost is the cost that makes the tested gross expectation zero.
Report uncertainty and compare like units: round-trip fees, spread crossings,
slippage and turnover under the declared execution assumptions. If plausible costs
exceed the estimated gross expectation, that construction has unfavorable expected
net economics under those assumptions. See
[Simple trading economics](simple-trading-economics) and [Tradability,
break-even, and out-of-sample](tradability-break-even-and-oos).

## If it dies at one tick

Record the adverse economics for the tested opportunity. Do not extrapolate from
a weak component average to every construction using the feature. Refinements
need a mechanism-based rationale, not endless cost/threshold changes seeking a
positive answer. Portfolio breadth may diversify risk but does not eliminate
negative expected per-trade net economics. Finer data can improve measurement;
it does not guarantee a larger signal or cheaper execution. See
[Microstructure false positives](microstructure-false-positives).

## Cost assumptions

Use observed spreads for the instrument and period when available. If only bars
are available, label cost scenarios as assumptions, and report missing quote or
execution evidence rather than asserting a measured cost floor. Include fees and
a simple slippage estimate. Keep the assumptions
visible in the study log. See [Trade prices versus
midquotes](trade-prices-versus-midquotes) and
[Data cost planning](data-cost-planning).

## Related

- [Discovery study design](discovery-study-design)
- [Return continuation and reversal](return-continuation-and-reversal)
- [Trade prices versus midquotes](trade-prices-versus-midquotes)
- [Microstructure false positives](microstructure-false-positives)
- [Simple trading economics](simple-trading-economics)
- [Data cost planning](data-cost-planning)
- [Tradability, break-even, and out-of-sample](tradability-break-even-and-oos)
