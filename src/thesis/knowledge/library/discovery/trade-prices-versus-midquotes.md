---
id: trade-prices-versus-midquotes
title: Trade prices versus midquotes
version: 1
created: 2026-10-02
retrieved: 2026-10-02
source:
  url: https://github.com/Carson-Fricke/FCA
  publisher: FCA Thesis
  content_hash: 89f4cfaa0303ac27bfb69a693eb031818c728b5f3604369b6496d4eb2a397b79
tags:
  - midquote
  - last-trade
  - bid-ask-bounce
  - signal-quality
collections:
  - discovery
  - data-quality
links:
  - discovery-study-design
  - return-continuation-and-reversal
  - cost-and-delay-sensitivity
  - intraday-sampling-and-sessions
  - microstructure-false-positives
  - simple-trading-economics
  - data-cost-planning
  - roll-bid-ask-bounce
relationships:
  - type: relates
    target: discovery-study-design
  - type: relates
    target: return-continuation-and-reversal
  - type: relates
    target: cost-and-delay-sensitivity
  - type: relates
    target: intraday-sampling-and-sessions
  - type: relates
    target: microstructure-false-positives
  - type: relates
    target: simple-trading-economics
  - type: relates
    target: data-cost-planning
  - type: relates
    target: roll-bid-ask-bounce
trust:
  tier: curated
  state: published
---
# Trade prices versus midquotes

Last-trade prices and midquotes measure different things. Confusing them
creates some of the most common false discoveries in short-horizon research.

## In brief

- The midquote is the midpoint of the best bid and ask.
- Last-trade prices bounce across the spread.
- For signal tests, prefer midquotes.
- Request quotes only when the question needs them; they cost the most.

## Two different objects

The midquote is \(m_t = (a_t + b_t)/2\), where \(a_t\) is the best ask and
\(b_t\) the best bid. The last trade is \(p_t = m_t + (s_t/2) Q_t\), where
\(s_t\) is the effective spread and \(Q_t\) is \(+1\) for a buy and \(-1\)
for a sell. Trades that bounce between the two sides create negative
short-horizon correlation even when the midquote is a random walk.

## Spread bounce looks like reversal

A buy at the ask followed by a sell at the bid shows a price drop that was
only the spread. A study built on last trades can therefore discover mean
reversion that cannot be traded. See [Microstructure false
positives](microstructure-false-positives) and
[Roll bid-ask bounce](roll-bid-ask-bounce).

## When to request quotes

Quotes are the heaviest data to fetch and store. Use them when the question
is about the efficient price, the spread, or short horizons. Use trades
when the question is about fills and execution realism. See
[Data cost planning](data-cost-planning).

## A simple check

Repeat the study on midquotes. If the effect disappears, it lived in the
bounce. If quotes are unavailable, replace each trade with the current
midquote approximated from the trade and the spread, or split trades by
side. See [Return continuation and
reversal](return-continuation-and-reversal).

## Related

- [Discovery study design](discovery-study-design)
- [Return continuation and reversal](return-continuation-and-reversal)
- [Cost and delay sensitivity](cost-and-delay-sensitivity)
- [Intraday sampling and sessions](intraday-sampling-and-sessions)
- [Microstructure false positives](microstructure-false-positives)
- [Simple trading economics](simple-trading-economics)
- [Data cost planning](data-cost-planning)
- [Roll bid-ask bounce](roll-bid-ask-bounce)
