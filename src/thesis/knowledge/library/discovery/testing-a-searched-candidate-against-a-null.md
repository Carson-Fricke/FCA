---
id: testing-a-searched-candidate-against-a-null
title: Testing a searched candidate against a null
version: 1
created: 2026-10-13
retrieved: 2026-10-13
source:
  url: https://github.com/Carson-Fricke/FCA
  publisher: FCA Thesis
  content_hash: f4daf762384ab496347915670b7e23fc9e4462bce306043cfebcea01f98a16e1
tags:
  - discovery
  - strategy-construction
  - multiple-testing
collections:
  - discovery
  - validation
links:
  - discovery-study-design
  - exploratory-search-and-multiple-attempts
relationships:
  - type: relates
    target: discovery-study-design
trust:
  tier: curated
  state: published
---
# Testing a searched candidate against a null

First identify the claim and null. A statistic of a fixed policy and the best
statistic after searching many policies are different quantities.

For a fixed rule, declare the null transform, dependence unit, statistic, cost/fill
model, seeds and treatment of failures. run_null_batch executes one fixed parameter
assignment over transformed inputs. It supplies conditional evidence for that rule.
If the rule was chosen on these same outcomes, label selection uncertainty explicitly.

For a searched family, run the SAME candidate search and selection on every null:
same ranges, objective, eligibility, budgets and any adaptive procedure included in
the claim. Compare the observed selected statistic with the null selected statistics.
A one-sided Monte Carlo rank can use (1 + count(null >= observed))/(B + 1) when
exchangeability and the chosen randomization null are justified. Its resolution is
1/(B+1); few replicates cannot support tiny p-values. Null choice must preserve
relevant temporal/session structure and disrupt the relationship being claimed.
A bootstrap uncertainty interval and a randomized null test are not interchangeable.

The current fixed-assignment run_null_batch DOES NOT rerun an external search.
Do not invent search-adjusted evidence from it. If a reproducible search-aware
procedure was not implemented within the budget, return conditional evidence and
the unresolved selection problem for formal validation. Never silently drop failed
replicates or change selection after seeing their results.

For comparisons sharing sessions or setups, resample joint session/episode records,
keeping both arms aligned and retaining their actual denominators. Independent-arm
resampling discards covariance; its error direction is not always anti-conservative.
For positive covariance, Var(A-B)=Var(A)+Var(B)-2Cov(A,B) is smaller than the
independent-arm expression. Reproduce the intended statistic, not a slogan.
No training test establishes out-of-sample profitability; forward data stays sealed.
