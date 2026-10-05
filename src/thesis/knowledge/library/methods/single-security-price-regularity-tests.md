---
id: single-security-price-regularity-tests
title: Single-security price-regularity tests
version: 1
created: 2026-10-01
retrieved: 2026-10-01
source:
  url: https://github.com/Carson-Fricke/FCA
  publisher: FCA Thesis
  content_hash: 57d818e5b93b50637159778fb16363d71769633bc96cac92f2f168095d77f61b
tags:
  - single-security
  - price-regularities
  - market-microstructure
  - falsification
  - method-selection
  - statistics
collections:
  - model-cards
  - inference
links:
  - roll-bid-ask-bounce
  - robust-autocorrelation-inference
  - tradability-break-even-and-oos
relationships:
  - type: relates
    target: roll-bid-ask-bounce
  - type: relates
    target: robust-autocorrelation-inference
  - type: relates
    target: tradability-break-even-and-oos
trust:
  tier: curated
  state: published
---
# Single-security price-regularity tests

Use this method cluster when a hypothesis claims a predictable regularity in one
security's price series: short-horizon reversion, continuation, seasonal
intraday structure, or dependence that a no-structure null should not produce.
It also applies when the claimed regularity is cross-asset but is measured on a
single series (for example a spread or an idiosyncratic residual).

## Translate the claim into a null

The statistical translation of "no exploitable regularity" is the martingale
difference null:

\[
E[r_t \mid \mathcal{F}_{t-1}] = 0
\]

where \(r_t\) is the return over the measurement interval. This is stronger
than zero unconditional autocorrelation: it says past information does not
predict the conditional mean. Most single-series tests only probe linear
dependence in the mean, so a failure to reject is not proof of the null.

State four things before choosing a test:

1. **The series.** Mid-quote returns, trade returns, bar closes, or residuals.
   The choice changes the answer. See [Roll bid-ask bounce](roll-bid-ask-bounce)
   for why trade-close autocorrelation is usually measurement, not signal.
2. **The horizon and sampling.** Interval length, session handling, and
   whether overnight returns are pooled.
3. **The null's shape.** IID, MDS with conditional heteroskedasticity, or a
   specific parametric null (random walk, constant expected return).
4. **The economic question.** A rejected point null is not a tradable edge.
   See [Tradability, break-even, and out-of-sample](tradability-break-even-and-oos).

## Decision checklist

- [ ] Use one contiguous trade-time or quote-time series. Never pool bars from
      different aggregations or fold overlapping windows into one series.
- [ ] Estimate and report the effective spread. Compare observed dependence
      with the mechanical bound the spread implies.
- [ ] Use heteroskedasticity-robust inference. IID \(z\)-statistics over-reject
      on intraday data.
- [ ] Handle sessions explicitly: exclude the overnight close-to-open move and
      treat the open auction, halts, and first and last minutes as their own
      regime or exclude them.
- [ ] Standardize by time of day before testing.
- [ ] Choose lags and horizons from the hypothesis, not from the data.
- [ ] Build the null distribution by wild bootstrap or an equivalent
      MDS-preserving resampling, not by shuffling.
- [ ] Map every statistic to costs, an execution lag, and an out-of-sample
      check before calling anything an edge.
- [ ] Pre-register direction and thresholds, or use a family-wise
      multiple-testing correction.

## Recommended pipeline

1. Data: cleaned mid-quotes and trades, one contiguous series, ex-overnight,
   time-of-day standardized, with the effective spread recorded.
2. Robust statistics: HAC \(t\) for \(\rho_1\), overlapping
   heteroskedasticity-robust variance-ratio statistics for several horizons,
   plus rank/sign autocorrelation and a portmanteau check.
3. Null: wild bootstrap (Rademacher) over the MDS-with-GARCH null; p-values
   from the bootstrap distribution, not from a single shuffle.
4. Economics: compare the estimated covariance with \(-(s_{\text{eff}}/2)^2\);
   backtest a lagged, cost-aware rule with a threshold; compute break-even
   spread.
5. Out-of-sample: estimate in one period, trade the next; report net
   performance, turnover, and a multiple-testing-adjusted significance level.

## Spokes

- [Roll bid-ask bounce](roll-bid-ask-bounce): why negative first-order
  autocorrelation in trade returns is mechanical, and what it does and does not
  prove.
- [Robust autocorrelation and variance-ratio inference](robust-autocorrelation-inference):
  HAC, Lo-MacKinlay, wild bootstrap, and the sampling hygiene intraday data
  requires.
- [Tradability, break-even, and out-of-sample](tradability-break-even-and-oos):
  turning a statistic into a decision rule and testing it against costs.

## Platform note

A study can subscribe to bars, quotes, and trades, so quoted-spread and
trade-based measures are observable. A bars-only study can compute close-to-close
statistics but cannot measure the effective spread or mid-quote returns; declare
the inputs the falsifier actually needs.
