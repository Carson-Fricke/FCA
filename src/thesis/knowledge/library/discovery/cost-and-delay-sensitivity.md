---
id: cost-and-delay-sensitivity
title: Cost and delay sensitivity
version: 1
created: 2026-10-02
retrieved: 2026-10-02
source:
  url: https://github.com/Carson-Fricke/FCA
  publisher: FCA Thesis
  content_hash: 856b65e5e8af9a96de8b16d197af259a57f55153edaef707861e2edb490fd725
tags:
  - transaction-costs
  - execution-delay
  - break-even
  - sensitivity
collections:
  - discovery
  - trading-costs
links:
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

- Re-run every result with a small cost and an execution delay.
- Compute the break-even cost: the cost where the net effect is zero.
- If the effect dies at one tick of cost, record that and stop.
- Cost assumptions belong in the study log, not hidden in the conclusion.

## Add cost and delay

Charge a cost on every round trip and delay the signal by at least one bar:

\[
\text{net} = \text{gross} - \text{cost per round trip}
\]

Tabulate the net effect for a few predeclared cost levels, such as zero,
half a tick, one tick, and two ticks, and for delays of zero, one, and two
bars. A table shows sensitivity better than a single number.

## Break-even cost

The break-even cost is the cost that makes the net effect zero. Compare it
with the observed spread. If the break-even cost is below the spread, the
strategy loses by construction for a taker who crosses. See
[Simple trading economics](simple-trading-economics) and [Tradability,
break-even, and out-of-sample](tradability-break-even-and-oos).

## If it dies at one tick

Record the death. A finding that cannot survive one tick of cost is a fact
about friction, not a trading edge. This is a valid and useful study result.
State it plainly instead of hiding it under gross numbers. See
[Microstructure false positives](microstructure-false-positives).

## Cost assumptions

Use the observed spread for the instrument and period, not a market-wide
average. Include fees and a simple slippage estimate. Keep the assumptions
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
