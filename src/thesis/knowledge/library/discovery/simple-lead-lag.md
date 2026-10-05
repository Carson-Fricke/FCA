---
id: simple-lead-lag
title: Simple lead-lag
version: 1
created: 2026-10-02
retrieved: 2026-10-02
source:
  url: https://github.com/Carson-Fricke/FCA
  publisher: FCA Thesis
  content_hash: 64790ea698f8873faf7943417bbc461ee21aa810efcbbf92dc57bd601afae57a
tags:
  - lead-lag
  - cross-asset
  - timestamp-alignment
  - snooping
collections:
  - discovery
  - model-cards
links:
  - discovery-study-design
  - market-adjusted-returns
  - historical-timing-and-leakage
  - discovery-versus-validation-data
relationships:
  - type: relates
    target: discovery-study-design
  - type: relates
    target: market-adjusted-returns
  - type: relates
    target: historical-timing-and-leakage
  - type: relates
    target: discovery-versus-validation-data
trust:
  tier: curated
  state: published
---
# Simple lead-lag

A lead-lag study asks whether one asset's move predicts another asset's
later move. The pattern is easy to find in noise if the timing is wrong.

## In brief

- Test whether one asset's move predicts another asset's later move.
- Align timestamps by availability, not event time.
- Sweep only a small, predeclared set of lags.
- Add a realistic action delay before claiming value.

## Cross-asset timing

Let \(r^{A}_{t}\) and \(r^{B}_{t}\) be returns for assets A and B. The study
asks whether \(r^{A}_{t}\) predicts \(r^{B}_{t+\ell}\) for some lag
\(\ell\):

\[
r^{B}_{t+\ell} = \alpha + \beta_\ell\, r^{A}_{t} + \varepsilon_{t}
\]

A positive \(\beta_\ell\) with the right timing is a candidate.

## Align clocks first

Timestamps must reflect when each price became known. A close printed at
16:00 may only be available after 16:00. Publication delays and venue clocks
differ. Use availability time. See [Historical timing and
leakage](historical-timing-and-leakage).

## Lag sweeps and snooping

Testing every lag from 1 to 100 will find a best lag even in pure noise.
Predeclare a small set of lags justified by the mechanism. Report every lag
tested, not only the best. Overlapping windows need standard errors that
account for the overlap.

## Action delay

A signal observed at \(t\) cannot be traded before \(t+1\) at the earliest.
Add at least one bar of delay and the cost of the crossing. If the effect
dies with delay, record that. See [Market-adjusted
returns](market-adjusted-returns) and [Discovery versus validation
data](discovery-versus-validation-data).

## Related

- [Discovery study design](discovery-study-design)
- [Market-adjusted returns](market-adjusted-returns)
- [Historical timing and leakage](historical-timing-and-leakage)
- [Discovery versus validation data](discovery-versus-validation-data)
