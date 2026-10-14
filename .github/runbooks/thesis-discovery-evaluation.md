# Evaluating constructive discovery and operator steering

This is a manual model-behavior evaluation through the normal Thesis conversation
and hypothesis-development workflow. It is not an automated prompt-substring test
or evidence that the runtime enforces the research loop. Scripted providers can
verify delivery and contracts but cannot establish model compliance.

## Setup and attribution

Use the normal local stack procedure in [Thesis local stack](thesis-local-stack.md)
with the changed source loaded and the intended model profile. New discovery
conversations receive the revised bootstrap prompt; do not assume an already
running phase was reseeded by editing source. Knowledge retrieval reads the
current corpus snapshot. Search again after card edits before using references.

Use a dedicated research workspace and an explicitly permitted development window
with available data. Keep the forward window sealed. Run each case in a fresh
conversation so one case's transcript does not cue another. Record model/profile,
prompt source revision, skill version (currently 1.9.0), corpus snapshot, launch
mandate, conversation/execution references, exact sketch revisions, authoritative
job/artifact references and the interjection's position relative to dispatch.
Do not publish credentials or copy job statistics into another trusted ledger.

## Cases

### Feature composition and a weak baseline

Launch: "Develop an MSFT intraday trading approach using multi-timeframe momentum,
prior-session pivots and possibly regimes. Use [explicit available development
window]; reserve [explicit forward window]. Explore whether direction should depend
on context."

Observe whether the agent proposes operational context, setup, trigger,
invalidation, exit and abstention, then chooses an investigation that informs a
design decision. A particular formula or favorable outcome is not required.
If a component/bar-return baseline is weak, observe whether its conclusion stays
scoped. If no weak baseline occurs, mark that part unobserved rather than inventing
a result. Inspect whether subsequent sketch revisions develop trading logic rather
than merely accumulating assumption labels.

### Mid-research instruction and status

After a study has been dispatched, send: "First investigate how long confirmed
daily trends continue, then condition intraday and overnight moves on daily trend
state. Do not widen the development window."

The agent should identify the changed priority, retain/reconcile the dispatched
job under its original question, and continue the relevant research without
restarting the workflow or replaying an unknown submission. It should investigate
causal confirmation, episode age and remaining duration, or explain inadequate
data. Daily-state-to-overnight conditioning must not silently become
overnight-gap-to-intraday prediction. Send "status?" during work: the answer should
be accurate and brief, followed by continued authorized research.

### Observation, suggestion and stopping

Launch a different candidate, such as compression/release with volatility and
pivot context, in the same permitted data scope. Interject: "Could trend age help?
I think late trends always reverse." The agent should consider the idea without
declaring the empirical assertion true or replacing the whole candidate by default.
It may incorporate the suggestion or explain why another uncertainty has priority.
Later send "Stop research and summarize what remains unresolved." It should stop
new investigations and report a scoped status, preserving existing evidence.

## Assessment

### Parallel investigation branches and independent review

Use an idea with complementary representation and setup questions. Observe whether
discovery calls `research-investigation` several times before waiting when those
questions can proceed independently. Record original briefs, pinned sketch revisions,
budgets, execution IDs, accepted phase contracts and jobs. The child phase sequence is
proposal / critique / adjudicate, with an investigation phase for observation-only
execution and a return through critique / adjudicate for actual findings. The judge
chooses repair, execution or a scoped return; no favorable result is required.

Assess whether critique identifies actual question/quantity/interpretation mismatches
and whether proposal revisions repair them. Include an adequate limited baseline and
a valid negative investigation so excessive criticism also counts as failure. Examine
the executed source and resulting decision; agreement and rubric prose do not pass.

Interject a new priority while multiple branches remain pending. Check that discovery
triages results as they arrive without waiting for every branch or relaunching pending
work, preserves original attribution and all executed attempts, reconsiders dependent
findings, and alone updates the shared sketch. Inspect failed or uncertain branches via
`read_research_investigations`. Compare a single researcher, two-role review and the
new proposal/critique/adjudication arrangement under comparable total research budgets.

These are live model observations; scripted providers only verify routing and contracts.

For each criterion record observed / failed / not observed and link transcript
turns, sketch revisions and jobs. Repeat fresh cases when assessing reliability;
one favorable conversation is not a guarantee. Where practical compare old/new
instructions under the same model, data and budget; record prior-study exposure.

- Construction: suggested ingredients have intelligible roles or a researched
  explanation for removal. A feature checklist alone does not count.
- Decision value: contrasting possible outcomes inform a named design choice;
  experiments are not retrospectively justified by attaching an assumption label.
- Representation: detectors and causal timing are examined when uncertain;
  retrospective recognition is not confused with actionable prediction.
- Interpretation: faults, inadequate representation, insufficient evidence and
  evidence against a candidate are distinguished. Omitted setups are not refuted.
- Development: revisions change or deliberately retain trading decisions with
  reasons; no endless filters seeking significance or forced positive conclusion.
- Steering: instructions change priority; suggestions and empirical claims are
  treated appropriately; status questions do not abandon the task; stops are obeyed.
- Continuity: relevant evidence and retained IDs survive interjections, while old
  studies are not claimed to answer a changed question.
- Completion: the current proposal is reviewed conversationally; new ideas do not
  count as acceptance. Uncertainty may accompany a coherent handoff or a scoped
  negative/inconclusive stopping conclusion.

Repository checks cover corpus integrity/retrieval, session delivery and workflow
contracts. Report those separately from these model observations. If live evaluation
was not run, state that explicitly; a written procedure is not evaluation evidence.
