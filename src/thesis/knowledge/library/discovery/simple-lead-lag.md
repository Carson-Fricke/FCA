---
id: simple-lead-lag
title: Simple lead-lag
version: 2
created: 2026-10-02
retrieved: 2026-10-13
source:
  url: https://github.com/Carson-Fricke/FCA
  publisher: FCA Thesis
  content_hash: ca0d44e00c0af7dde235fa23e5e57e742ca6e18b15733f149cb4e822b4177409
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
- Explore a bounded, mechanism-informed set of lags on development data.
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

Searching many lags produces a winner even in noise. During discovery, choose
useful lag ranges, explore them and refine them when the results inform the
construction. Record the ranges, refinements and every tested lag. Search is
permitted; its winner requires selection-aware interpretation. Overlapping
windows need uncertainty calculations that preserve the overlap.

Use promising lag relationships inside the proposed entry/exit policy and
evaluate that policy after costs. A pooled regression is a diagnostic, not a
test of an omitted feature interaction or trading setup. See
[Constructive parameter and interaction search](constructive-parameter-and-interaction-search).

## Action delay

A signal cannot use a fill from before its information became available.
Evaluate causal processing and execution delay using the declared execution
feed and fill model, together with crossing costs. The delay need not be a
whole signal bar, especially when slow context drives faster entries. If the effect
dies with realistic delay, record that. See [Market-adjusted
returns](market-adjusted-returns) and [Discovery versus validation
data](discovery-versus-validation-data).

## Related

- [Discovery study design](discovery-study-design)
- [Market-adjusted returns](market-adjusted-returns)
- [Historical timing and leakage](historical-timing-and-leakage)
- [Discovery versus validation data](discovery-versus-validation-data)
