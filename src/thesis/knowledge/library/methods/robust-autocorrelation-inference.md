---
id: robust-autocorrelation-inference
title: Robust autocorrelation and variance-ratio inference
version: 1
created: 2026-10-01
retrieved: 2026-10-01
source:
  url: https://github.com/Carson-Fricke/FCA
  publisher: FCA Thesis
  content_hash: 43c3adba226253fbdfd42ccbaf9bde4dbf422601dceb020cea55dbec8276bf5c
tags:
  - hac
  - heteroskedasticity
  - variance-ratio
  - wild-bootstrap
  - intraday
  - time-of-day
  - sample-selection
collections:
  - model-cards
  - inference
links:
  - single-security-price-regularity-tests
  - roll-bid-ask-bounce
  - tradability-break-even-and-oos
relationships:
  - type: relates
    target: single-security-price-regularity-tests
  - type: relates
    target: roll-bid-ask-bounce
  - type: relates
    target: tradability-break-even-and-oos
trust:
  tier: curated
  state: published
---
# Robust autocorrelation and variance-ratio inference

One-minute returns violate every assumption behind the textbook
autocorrelation and variance-ratio standard errors: intraday U-shape, volatility
clustering, heavy tails, diurnal non-stationarity, discrete and stale prices,
and zero returns. Inference that ignores this over-rejects.

## In brief

- Use heteroskedasticity-robust statistics everywhere: HAC/White \(t\) for
  \(\rho_1\) and Lo-MacKinlay \(z^\*(q)\) for variance ratios.
- Get the null distribution from a wild bootstrap (Rademacher
  \(r_t^\* = e_t W_t\)), not from shuffling and not from an IID formula.
  A single deterministic shuffle is one draw, not a p-value; use 2,000 to
  10,000 bootstrap draws and report the bootstrap p-value.
- Clean the sample first: one contiguous series, no overnight pooling, no
  auctions or halts, time-of-day standardization.

## Regression HAC standard error

Let \(e_t = r_t - \bar r\). Under the martingale-difference null the
heteroskedasticity-robust variance of the AR(1) coefficient is

\[
\widehat{\operatorname{Var}}(\hat{\rho}) =
\frac{\sum_{t=2}^{n} e_t^2 e_{t-1}^2}{\left(\sum_t e_t^2\right)^2},
\qquad
t^\* = \frac{\hat{\rho}}{\sqrt{\widehat{\operatorname{Var}}(\hat{\rho})}}
\]

This is the White correction restricted to the AR(1) case. It removes the
homoskedasticity assumption but still assumes no serial correlation in the
squared innovations at the tested lag; for longer dependence use a HAC
(Newey-West) estimator with a bandwidth tied to the horizon.

## Variance ratio: use Lo-MacKinlay, not an IID formula

For overlapping \(q\)-period returns, the heteroskedasticity-consistent
statistic is

\[
z^\*(q) = \frac{VR(q) - 1}{\sqrt{\phi^\*(q)}},
\qquad
\phi^\*(q) = \sum_{j=1}^{q-1}
\left[\frac{2(q-j)}{q}\right]^2 \hat\delta_j,
\qquad
\hat\delta_j = \frac{\sum_{t=j+1}^{n} e_t^2 e_{t-j}^2}{\left(\sum_t e_t^2\right)^2}
\]

The common IID forms are wrong in opposite directions: a non-overlapping
variance of \(2(q-1)/n\) is not the overlapping variance, and the overlapping
homoskedastic variance is approximately \(2(2q-1)(q-1)/(3qn)\). Using a
non-overlapping denominator while ignoring heteroskedasticity is conservative
under IID and anti-conservative in practice. Report overlapping,
bias-corrected, heteroskedasticity-robust variance ratios.

Also note that \(VR(5)-1\) is a weighted sum of \(\rho_1\) through \(\rho_4\).
Testing \(\rho_1\) and \(VR(5)\) is not linear versus multi-horizon evidence; it
is two correlated views of the same linear lags. Add genuinely different
statistics: rank or sign autocorrelation, a portmanteau test (for example
Escanciano-Lobato), and a joint maximum across horizons (Chow-Denning).

## Sampling and stationarity hygiene

- Exclude overnight close-to-open moves; never pool them with intraday returns.
- Treat the open auction, halts, and the first and last minutes separately, or
  exclude them.
- Demean and volatility-standardize by time of day using an estimated
  \(\hat\sigma_{\text{TOD}}(t)\), or diurnal heteroskedasticity alone will
  distort \(\hat{\rho}\) and \(VR\).
- Handle splits, dividends, bad ticks, and zero returns explicitly; one bad
  outlier can manufacture a significant negative \(\rho_1\).
- Use one contiguous trade-time or quote-time series. Folding bars from
  different aggregations or overlapping windows destroys the dependence
  structure.
- Choose horizons and lag counts from the hypothesis. \(q = 5\) and block
  length 20 are researcher degrees of freedom that must be declared, not tuned
  after seeing results.

## Null distribution: wild bootstrap, not shuffling

A naive shuffle destroys volatility clustering; it preserves the marginal
distribution, not the MDS-with-GARCH null. The right placebo is the wild
bootstrap:

\[
r_t^\* = e_t W_t, \qquad W_t = \pm 1 \text{ Rademacher}
\]

This preserves \(|e_t|\) and therefore the volatility path while destroying
mean predictability. Compute the full distribution of \(\hat\rho^\*\) and
\(VR^\*(q)\); one deterministic shuffle is a single draw, not a p-value. Use a
bootstrap that adapts the statistic to the data (for example Kim's
wild-bootstrap automatic variance ratio), 2,000 to 10,000 draws, and a
data-driven or stationary block length when a block bootstrap is used. Report
the bootstrap p-value alongside the robust statistic.

## Related

- [Single-security price-regularity tests](single-security-price-regularity-tests)
- [Roll bid-ask bounce](roll-bid-ask-bounce)
- [Tradability, break-even, and out-of-sample](tradability-break-even-and-oos)
