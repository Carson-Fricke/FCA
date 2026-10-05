---
id: simple-trading-economics
title: Simple trading economics
version: 1
created: 2026-10-02
retrieved: 2026-10-02
source:
  url: https://github.com/Carson-Fricke/FCA
  publisher: FCA Thesis
  content_hash: 05f938909a201be140f49aa42de60ef298b9857fb26c04827a9df9e82f447352
tags:
  - spread
  - slippage
  - impact
  - turnover
  - capacity
collections:
  - trading-costs
links:
  - discovery-study-design
  - return-continuation-and-reversal
  - trade-prices-versus-midquotes
  - cost-and-delay-sensitivity
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
    target: cost-and-delay-sensitivity
  - type: relates
    target: data-cost-planning
  - type: relates
    target: tradability-break-even-and-oos
trust:
  tier: curated
  state: published
---
# Simple trading economics

Gross profit is not net profit. Every strategy pays to trade, and the
payments are large enough to erase most small edges.

## In brief

- Costs: spread, fees, slippage, impact, turnover, funding.
- Compute the net number before tuning anything.
- Break-even is the cost level where net profit is zero.
- Capacity is the size at which impact eats the edge.

## The cost list

- Spread: crossing pays roughly half the spread per side, about a full
  spread per round trip.
- Fees: commissions, exchange fees, and taxes.
- Slippage: the gap between the expected and realized fill price.
- Impact: your own order moves the price against you.
- Turnover: how often the position changes multiplies every per-trade cost.
- Funding: interest or borrow cost for leveraged, short, or perpetual
  positions.

## Gross versus net

\[
\text{net} = \text{gross} - \text{spread} - \text{fees} - \text{slippage}
- \text{impact} - \text{funding}
\]

A strategy with a gross edge smaller than its round-trip costs loses money
reliably. Compute the net number before tuning anything.

## Break-even

Break-even cost is the total per-trade cost at which net profit reaches zero.
Compare it with the instrument's observed spread and fee schedule. If
break-even is below the market's cost, the idea is dead as specified. See
[Tradability, break-even, and out-of-sample](tradability-break-even-and-oos)
and [Cost and delay sensitivity](cost-and-delay-sensitivity).

## Capacity intuition

Impact grows with order size while the edge per trade does not. Capacity is
the size where the two meet. A small edge that works at low size may vanish
at institutional size. State the assumed size when reporting a net result.
See [Data cost planning](data-cost-planning).

## Related

- [Discovery study design](discovery-study-design)
- [Return continuation and reversal](return-continuation-and-reversal)
- [Trade prices versus midquotes](trade-prices-versus-midquotes)
- [Cost and delay sensitivity](cost-and-delay-sensitivity)
- [Data cost planning](data-cost-planning)
- [Tradability, break-even, and out-of-sample](tradability-break-even-and-oos)
