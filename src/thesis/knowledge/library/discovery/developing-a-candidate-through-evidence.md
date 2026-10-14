---
id: developing-a-candidate-through-evidence
title: Developing a candidate through evidence
version: 3
created: 2026-10-11
retrieved: 2026-10-13
source:
  url: https://github.com/Carson-Fricke/FCA
  publisher: FCA Thesis
  content_hash: 016960a0fd551d5e83cabb9c429beb28ccc556732cd52b0539b46bf35a2f3004
tags:
  - discovery
  - research-loop
  - strategy-construction
  - operator-steering
  - negative-evidence
  - feature-comparison
  - inconclusive-evidence
collections:
  - discovery
links:
  - from-market-idea-to-trading-construction
  - investigating-representations-and-episodes
  - return-continuation-and-reversal
  - discovery-study-design
  - exploratory-search-and-multiple-attempts
  - cost-and-delay-sensitivity
relationships:
  - type: relates
    target: return-continuation-and-reversal
  - type: relates
    target: discovery-study-design
trust:
  tier: curated
  state: published
---
# Developing a candidate through evidence

The research loop is orient, construct, explore, assess and revise. It is model
directed, not a deterministic checklist. Root discovery owns the current sketch,
operator conversation and triage; branches own bounded independent investigations.

A worked example: start with daily trend context, wavelet structure, an intraday
pullback trigger and a time/structure exit. Implement the composed strategy.
Explore a bounded set of lookbacks, thresholds and holding rules, inspect losing
variants, then investigate whether a useful region survives higher costs and
different sessions. Compare feature ablations on suitable opportunity sets.
A weak unconditional return is not a veto over this composed policy.

A single critic checks both fidelity (does the test answer the original question,
and does the conclusion follow?) and correctness (source, timing, costs, signs,
support and statistical estimand). It reconstructs the policy before checking old
repairs. The judge independently resolves material disputes and chooses revision,
further investigation or a scoped return. No loop is owed merely for agreement.

Operator steering changes the CURRENT question. If the operator asks to prioritize
failed pullbacks while jobs run, retain those jobs under their original question
and IDs, revise priorities and investigate failures next. A status question receives
a brief answer and work continues. Do not restart by default or finish an obsolete
plan solely because it was announced. Stop/replacement instructions take priority.
An empirical suggestion is a hypothesis, while an instruction directs work.

Launch complementary branches concurrently when useful; triage each asynchronous
return without awaiting all others. Only discovery edits the shared sketch. Preserve
failures, technical repairs, scientific alternatives, conditional null limitations
and pending work. Hand off sketch.prototype with the exact tested strategy version,
selected parameters, candidate job and search evidence. Later phases refine this
prototype, retain its selection history and perform formal validation.

Propose the current integrated revision and call request_workflow_input in the same
response; complete only after operator acceptance. A new research idea is not acceptance.
No candidate is an honest possible outcome, but omitted interactions, impossible
sample floors and budget exhaustion are not evidence that the feature family fails.
