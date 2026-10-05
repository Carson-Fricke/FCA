---
id: signal-size-buckets
title: Signal size buckets
version: 1
created: 2026-10-02
retrieved: 2026-10-02
source:
  url: https://github.com/Carson-Fricke/FCA
  publisher: FCA Thesis
  content_hash: 8802d1d4df37f96a630db200c9dfb16a03644b2424e8e15e4ebb723df96b1209
tags:
  - signal-buckets
  - threshold-choice
  - selection
collections:
  - discovery
links:
  - discovery-study-design
  - return-continuation-and-reversal
  - volume-and-volatility-conditions
  - exploratory-search-and-multiple-attempts
relationships:
  - type: relates
    target: discovery-study-design
  - type: relates
    target: return-continuation-and-reversal
  - type: relates
    target: volume-and-volatility-conditions
  - type: relates
    target: exploratory-search-and-multiple-attempts
trust:
  tier: curated
  state: published
---
# Signal size buckets

A prior move can be split into size groups to see whether small and large
moves behave differently. The main risk is selection: the best-looking
group will always exist.

## In brief

- Split the prior move into a few predeclared size groups.
- Measure the later return in each group.
- Report every group, not only the winner.
- Never present the best bucket as an edge.

## Split before you look

Choose the bucket edges from a rule, not from the data being tested. Three
or four groups are enough: large down, small down, small up, large up. More
edges mean more buckets, and more buckets mean more chances that one looks
good by accident. See [Exploratory search and multiple
attempts](exploratory-search-and-multiple-attempts).

## Measure later returns per group

For group \(g\), average the later return:

\[
\bar r_g = \frac{1}{n_g} \sum_{i \in g} r_{i,t:t+h}
\]

Report \(\bar r_g\), the count \(n_g\), an uncertainty interval, and the
spread. Groups with few observations carry wide intervals and should not be
highlighted.

## Never present the best bucket as an edge

A table of four buckets will almost always have a best-looking row. That row
was selected by the same data used to judge it, so presenting only that row
hides the selection. Show the whole table. If the effect exists only in one
extreme group, describe it as a tail observation that needs more data, not
as an edge.

## Look for shape and mechanism

A believable effect usually varies smoothly across groups or has a named
cause. A single spike in one group with no mechanism is a warning. Volume
and volatility can change which group wins; see
[Volume and volatility conditions](volume-and-volatility-conditions).

## Related

- [Discovery study design](discovery-study-design)
- [Return continuation and reversal](return-continuation-and-reversal)
- [Volume and volatility conditions](volume-and-volatility-conditions)
- [Exploratory search and multiple attempts](exploratory-search-and-multiple-attempts)
