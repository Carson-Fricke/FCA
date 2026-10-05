---
id: roll-bid-ask-bounce
title: Roll bid-ask bounce
version: 1
created: 2026-10-01
retrieved: 2026-10-01
source:
  url: https://github.com/Carson-Fricke/FCA
  publisher: FCA Thesis
  content_hash: 754ea87c01e395851b79a279da111ed014e3fc51965289ae0eac72c4abe7cc93
tags:
  - bid-ask-bounce
  - roll-model
  - microstructure
  - autocorrelation
  - variance-ratio
collections:
  - model-cards
  - inference
links:
  - single-security-price-regularity-tests
  - robust-autocorrelation-inference
  - tradability-break-even-and-oos
relationships:
  - type: relates
    target: single-security-price-regularity-tests
  - type: relates
    target: robust-autocorrelation-inference
  - type: relates
    target: tradability-break-even-and-oos
trust:
  tier: curated
  state: published
---
# Roll bid-ask bounce

Negative first-order autocorrelation in trade returns is the default outcome of
a market with a spread, even when the efficient price is a pure random walk.
Treating it as evidence of mean reversion is a measurement error.

## The mechanism

Let the observed trade price equal an efficient price plus half the effective
spread times the trade direction (Roll, 1984):

\[
p_t = m_t + \frac{s}{2} Q_t, \qquad Q_t \in \{+1, -1\}
\]

If \(m_t\) is a random walk with zero drift, trade-direction flipping induces

\[
\operatorname{Cov}(\Delta p_t, \Delta p_{t-1}) = -\frac{s^2}{4} < 0
\]

and the variance ratio over horizon \(q\) is mechanically below one:

\[
VR(q) - 1 \approx 2 \sum_{j=1}^{q-1} \left(1 - \frac{j}{q}\right) \rho_j < 0
\]

A negative \(\rho_1\) and \(VR(5) < 1\) are therefore the *prediction* of the
friction model, not a departure from the null.

## Worked magnitude

With a 2bp effective spread and 1-minute volatility near 10bp, the variance is
about 100 and the Roll covariance about \(-1\), giving
\(\rho_{\text{Roll}} \approx -0.01\). With \(n = 50{,}000\) observations the
IID-style statistic is about \(-2.2\): conventionally significant, and purely
mechanical. A naive contrarian taker earns a gross expected reversion near

\[
E[-\operatorname{sign}(r_{t-1}) r_t] \approx |\rho| \sqrt{2/\pi}\, \sigma
\approx 0.08\text{bp}
\]

per minute while crossing roughly 1bp of spread plus fees and impact. The
strategy is structurally unprofitable.

## What a negative estimate does and does not show

- It **does** quantify friction: it is a lower bound tied to the spread and is
  useful as a data-quality and cost sanity check.
- It **does not** show a recoverable taker edge. A taker needs predictability
  that survives costs and the execution lag.
- It says nothing about maker profitability, which depends on queue position,
  fill rates, and adverse selection.

## When trade-close autocorrelation is informative anyway

Test against the friction bound rather than zero:

\[
\hat{\rho}_1 < -\frac{(s_{\text{eff}}/2)^2}{\operatorname{Var}(r)}
\]

A dependence more negative than the spread mechanism can generate is evidence
of something else, and should be investigated before it is traded. Prefer
mid-quote returns for price discovery questions, or a signal observed at \(t\)
with a fill at the \(t+1\) bid or ask.

## Related

- [Single-security price-regularity tests](single-security-price-regularity-tests)
- [Robust autocorrelation and variance-ratio inference](robust-autocorrelation-inference)
- [Tradability, break-even, and out-of-sample](tradability-break-even-and-oos)
