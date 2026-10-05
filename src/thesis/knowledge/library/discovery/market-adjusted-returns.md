---
id: market-adjusted-returns
title: Market-adjusted returns
version: 1
created: 2026-10-02
retrieved: 2026-10-02
source:
  url: https://github.com/Carson-Fricke/FCA
  publisher: FCA Thesis
  content_hash: 6b3b9b3f4ee4b2ffdb4ee0367f89d99642dfe5b95f8049b2ec907cf3508ca271
tags:
  - benchmark
  - beta
  - idiosyncratic-return
  - exposure
collections:
  - discovery
  - model-cards
links:
  - discovery-study-design
  - return-continuation-and-reversal
  - simple-lead-lag
  - discovery-versus-validation-data
relationships:
  - type: relates
    target: discovery-study-design
  - type: relates
    target: return-continuation-and-reversal
  - type: relates
    target: simple-lead-lag
  - type: relates
    target: discovery-versus-validation-data
trust:
  tier: curated
  state: published
---
# Market-adjusted returns

A signal asset often moves because the whole market moves. Market adjustment
separates the shared part of a return from the part specific to the asset.

## In brief

- Compare the signal asset with a benchmark before crediting the signal.
- The residual after the benchmark is the idiosyncratic part.
- A benchmark is not a control by itself; it shares market-wide effects.
- Report both raw and adjusted results.

## Why adjust

A rise in the signal asset may just be the market rising. Adjusting removes
the shared part and leaves the part specific to the asset. Regress asset
returns on benchmark returns:

\[
r_{i,t} = \alpha + \beta\, r_{m,t} + \epsilon_{i,t}
\]

The residual \(\epsilon_{i,t}\) is the idiosyncratic return.

## Benchmark is not a control

A benchmark absorbs common exposure, but it carries its own spread, session,
and volatility effects. Subtracting it does not remove microstructure bias,
and it does not prove the signal is causal. Choose the benchmark before the
study and state why it is the right comparison.

## How to report

Show the raw effect and the adjusted effect side by side. If the effect
exists only in raw returns, it is likely exposure. If it exists in the
residual, it belongs to the asset. State the estimation window for \(\beta\)
and keep it out of the window used to judge the effect.

## Extensions

For a cross-asset timing study, adjust both sides. For sector or style
exposure, add more than one benchmark, but each addition costs a degree of
freedom. Keep the list short and predeclared. See [Simple
lead-lag](simple-lead-lag) and [Discovery versus validation
data](discovery-versus-validation-data).

## Related

- [Discovery study design](discovery-study-design)
- [Return continuation and reversal](return-continuation-and-reversal)
- [Simple lead-lag](simple-lead-lag)
- [Discovery versus validation data](discovery-versus-validation-data)
