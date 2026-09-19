# Thesis Operator-Facing Text

Thesis shows the operator two kinds of text: **agent prose** that the model
authored, and **workflow text** the workflow itself emits. Because a canned
workflow sentence can masquerade as the agent's voice, this document fixes the
rule for which text is model-generated, which is deterministic, and why.

## Rule

1. **Requests for operator input or direction are always model-generated.**
   A workflow that needs a decision asks the model to author the request from a
   structured reason. It never emits a canned question. The single mechanism is
   `HypothesisDevelopmentWorkflow._operator_question(purpose, detail, active)`;
   callers supply the purpose and the concrete reason, never the phrasing.
2. **Status prose that is read as the agent's voice should be model-generated
   when it states a judgment, and state-derived when it only narrates.** A
   phase summary that reports "3 trials ran" may be deterministic because it is
   a restatement of typed state. A phase summary that characterizes a result
   belongs to the model.
3. **Mechanical and structural text is deterministic.** Phase names, status
   codes, counts, digests, ledger statements, schema field names, activity
   labels, transition reasons, and Markdown scaffolds are data. They are not
   response prose and do not go to the model.
4. **A fallback is a diagnostic, not a fabricated prompt.** When the model is
   unavailable the workflow surfaces the raw reason (for example the validator
   failure text) so the operator still sees the concrete problem. It must never
   invent conversational phrasing.
5. **Errors are surfaced, never hidden.** A harness or data failure is passed
   into the model's trial evaluation prompt, so the agent's prose explains what
   went wrong instead of a generic pause.

## Where model generation is wired

Operator requests are now authored by the asking phase agent through the wrapped
`request_workflow_input` tool; the engine never authors or relays one. The former
engine callers and what covers them now:

| Former caller | Now |
| --- | --- |
| `_transition_checked` | the phase agent's own completion-contract repair via the tool result |
| `_pause_for_compose` | the phase agent re-derives and retries the completion in-turn |
| `_hypothesis_creation` | the creation phase agent asks with `request_workflow_input` |
| `_emit_verdict_review` / `_resume_verdict` | the verdict phase agent asks, then advances via `request_phase_completion` |
| `_stop_for_cycle_budget` | the phase agent reports the exhausted budget in its completion or question |

The falsification trials prose is authored by the research sub-agent from the
typed trial results, including each run's state and failure detail.

## Hardcoded operator-facing text that remains

This is the current inventory. "Deterministic" means the rule permits it;
"migrate" means it should route through `_operator_question` or an
agent-narrated prompt.

| Location | Text | Classification | Action |
| --- | --- | --- | --- |
| `hypothesis_development._phase_summary` | per-phase one-line summaries | state narration | keep deterministic; revisit if read as judgment |
| `hypothesis_development._post_phase_handoff`, `_premise_handoff`, `_strategy_handoff` | handoff notes between phases | state narration, agent-voice | candidate to model-generate |
| `hypothesis_development._emit_orchestrator_text` on finalization | "Hypothesis finalization recorded. N trial(s) planned." | status | deterministic |
| `hypothesis_development._fail` | `Phase failed: {error}` | diagnostic | deterministic |
| `hypothesis_development._PHASE_TASKS` | activity labels | internal progress | deterministic |
| `hypothesis_development._append_attempt`, `_transition` reasons | ledger and transition reasons | mechanical | deterministic |
| `chat.responder._development_summary` | started/paused/failed/finished recap | operator-facing prose | migrate: model-generate or derive as a typed-state template |
| `agent.hypothesis_skills` | `falsification-rca` skill instructions | prompt text | the id still names falsification but the skill now serves strategy RCA; consider renaming the public id |
| `workflows/rca.py`, `workflows/strategy_development.py`, `workflows/discovery.py` | sibling-phase questions and summaries | operator-facing | migrate to the same `_operator_question` mechanism |
| `src/ui/.../chat/*` | labels and status badges | presentation | deterministic |

## Open design space

- Whether `_phase_summary` and the handoff notes should become model narration
  or stay deterministic typed-state templates.
- Where the fallback reason should be presented as "the workflow could not
  reach the model" versus the raw validator text.
- Whether the sibling workflows (`rca`, `strategy_development`, `discovery`)
  share one operator-text helper or each owns a thin wrapper.
