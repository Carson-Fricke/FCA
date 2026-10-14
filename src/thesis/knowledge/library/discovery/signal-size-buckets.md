---
id: signal-size-buckets
title: Signal size buckets
version: 2
created: 2026-10-02
retrieved: 2026-10-13
source:
  url: https://github.com/Carson-Fricke/FCA
  publisher: FCA Thesis
  content_hash: 2095a2e987b5df396f6e73cf92450014bc38eaa71dad6afd856d0222bf0a4bed
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

- Explore useful size groups and thresholds on development data.
- Measure the later return in each group.
- Report every group, not only the winner.
- Evaluate promising thresholds inside the proposed trading policy.
- Interpret the selected result with its full search history.

## Explore thresholds during discovery

Bucket edges and thresholds are legitimate training parameters. Start with
interpretable choices and explore useful alternatives within the research
budget. Large down, small down, small up and large up are possible diagnostic
groups, not a mandatory partition. More choices increase selection optimism;
retain all choices and their results. See [Exploratory search and multiple
attempts](exploratory-search-and-multiple-attempts).

## Measure later returns per group

For group \(g\), average the later return:

\[
\bar r_g = \frac{1}{n_g} \sum_{i \in g} r_{i,t:t+h}
\]

Report \(\bar r_g\), the count \(n_g\), an uncertainty interval, and the
spread. Groups with few observations carry wide intervals and should not be
highlighted.

## Develop the selected policy and assess robustness

A bucket table usually has a best-looking row. Show the whole table and use
promising thresholds as candidates to develop, with the requested feature
interactions, entries, exits and costs. Investigate neighboring thresholds,
independent support and suitable null evidence. A tail result may motivate a
selective strategy; sparse support is uncertainty, not automatic disqualification.
Neither the selected row nor a failed pooled baseline settles policy viability.

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
