---
id: tradability-break-even-and-oos
title: Tradability, break-even, and out-of-sample
version: 1
created: 2026-10-01
retrieved: 2026-10-01
source:
  url: https://github.com/Carson-Fricke/FCA
  publisher: FCA Thesis
  content_hash: 3d5c00199af5c50b87ed06fcdcaf27ce5c0d14a767f353a51c52dbe2e431a3ca
tags:
  - tradability
  - transaction-costs
  - break-even
  - out-of-sample
  - multiple-testing
  - execution-lag
collections:
  - model-cards
  - trading-costs
links:
  - single-security-price-regularity-tests
  - roll-bid-ask-bounce
  - robust-autocorrelation-inference
relationships:
  - type: relates
    target: single-security-price-regularity-tests
  - type: relates
    target: roll-bid-ask-bounce
  - type: relates
    target: robust-autocorrelation-inference
trust:
  tier: curated
  state: published
---
# Tradability, break-even, and out-of-sample

A significant statistic is not an edge. With \(n \sim 10^5\), \(|\rho| = 0.02\)
produces \(|z| \sim 6\) and an \(R^2\) of 0.0004. Testing the point null
\(\rho = 0\) will reject trivial, untradable effects. The decision null must be
economic.

## Flip the null

\[
H_0:\; E[\text{net P\&L after spread, fees, slippage, and execution lag}] \le 0
\]

An edge exists only if a fully specified rule survives that null.

## Specify the rule before testing

- **Forecast.** For example \(\hat r_{t+1|t} = \hat\rho\, r_t\).
- **Threshold.** Trade only when \(|\hat r_{t+1|t}| > c\), where \(c\) covers
  half the effective spread, fees, and one bar of execution lag.
- **Execution.** Signal at \(t\), fill at \(t+1\) at the bid or ask. Crossing
  the spread every interval must be charged, not assumed away.
- **Selective trading.** The unconditional mean of \(\rho\) understates the
  tail profitability of a thresholded rule and overstates small-move
  profitability. Report gross versus net means, turnover, and the break-even
  spread.
- **Costs.** Use realized fee schedules and an impact or slippage model. Compare
  the break-even spread to the measured effective spread; an edge narrower than
  the spread is a maker problem, not a taker problem.

## Break-even first

Before any strategy work, compute the cost per round trip implied by the
signal's average expected move. If the break-even spread is below the observed
effective spread, stop. This single check eliminates most microstructure
artefacts, including the bid-ask bounce case in
[Roll bid-ask bounce](roll-bid-ask-bounce).

## Out-of-sample and multiple testing

- Estimate parameters in the first half of the sample and trade the second
  half; then walk forward.
- Report out-of-sample \(R^2\), net Sharpe with HAC standard errors, and
  turnover.
- Correct for data snooping. When lags, horizons, or thresholds were searched,
  use a White Reality Check, Hansen SPA, Romano-Wolf stepdown, or a deflated
  Sharpe ratio, and require stronger evidence than \(t > 2\).
- Zero autocorrelation does not prove the martingale null. Linear dependence
  misses conditional or nonlinear structure: sign, size, time of day, order
  book state, and lead-lag to a benchmark. Test the alternatives the hypothesis
  actually implies.
- A benchmark series such as SPY is not a control by itself; it shares the same
  spread bias. At most use it to orthogonalize: test the autocorrelation of
  idiosyncratic returns \(r_{i,t} = \alpha + \beta r_{m,t} + \epsilon_t\).

## Minimal acceptance sequence

1. Robust statistic with a wild-bootstrap p-value.
2. Dependence stronger than the spread mechanism implies.
3. A lagged, thresholded, cost-charged rule with positive expected net P&L.
4. Out-of-sample confirmation with multiple-testing control.

Anything short of step four is a candidate for further research, not a result.

## Related

- [Single-security price-regularity tests](single-security-price-regularity-tests)
- [Roll bid-ask bounce](roll-bid-ask-bounce)
- [Robust autocorrelation and variance-ratio inference](robust-autocorrelation-inference)
