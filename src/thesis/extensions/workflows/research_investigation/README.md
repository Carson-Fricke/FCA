# Research investigation

Discovery calls `start_workflow` with `workflow: research-investigation` to delegate
one bounded research direction. Several instances can run concurrently and return
independently to their launching discovery conversation. This is a separate workflow;
the hypothesis-development phase table stays unchanged.

## Design and phase table

| Phase | Owns | Requested next phase |
| --- | --- | --- |
| `proposal` | Design/repair an investigation of the original brief and pinned construction. | `critique` |
| `critique` | Independently inspect question/design alignment, implementation and interpretation; recognize sound limited work. | `adjudicate` |
| `adjudicate` | Resolve specific objections against evidence; choose a useful action within the budget. | `proposal`, `investigation`, `completed` |
| `investigation` | Author/validate/execute an training strategy or targeted diagnostic study; inspect artifacts and interpret actual findings. | `critique` |
| `completed`, `failed` | Terminal outcomes. | None |

Every active phase is model-backed. The engine routes accepted contracts; it chooses
no statistic, research direction or scientific verdict. Review/research budgets are
declared in the brief and visible in accepted state. Runtime caps bound resource use;
they do not select studies or scientific conclusions. The execution cap (1..512) counts actual retained jobs, including individual batch
points, null replicates and technical repairs. Scientific variants are recorded
separately in accepted interpretation; a repair is not automatically a new hypothesis.
The same cap also bounds investigation entries, including blocked no-job rounds.
Scientific sufficiency remains
the judge's responsibility. Operator acceptance of discovery stays in discovery.

The current runtime contract is `research-investigation@2.0.0`; the phase flow,
single critic and model policies are unchanged. Older executions are rejected
for execution rather than decoded under the new contracts. Proposals include `repairs`
with original issue IDs and responses. Critics include `verifiedRepairs` only after
independently checking the repair. The accepted issue ledger carries unresolved and
repair-required objections across revisions; adjudication can explicitly accept a
limitation. These fields record model decisions, not engine-selected scientific rules.

Seeds contain bounded original-brief and active-issue excerpts, pinned sketch and job
references, and exact accepted handoff indices. The existing
`read_research_investigations` tool reads full accepted products in bounded pages:
handoff zero is the original brief; positive indices identify immutable accepted
transitions. A phase can read only its own branch. This is accepted-contract retrieval,
not transcript reading or search. Reentered phases retain their own conversations.
Critique compares original intent, approved construction, executed source and claims;
it distinguishes omitted tests from adverse evidence. Job-specific `read_study` and `read_strategy`
retrieves the immutable executed source rather than a subsequently edited program.

The public brief contains direction, question, intended decision, constraints,
sketchReference, prior evidence references, revisionBudget and executionBudget. Runtime
composition derives parent identity, phase attempt and frozen data mandate from the
launching discovery agent. No model-supplied execution identity or window overrides.
Branches own separate program IDs; only discovery edits the shared strategy sketch.
Accepted handoffs carry the original brief and phase products, never private sibling
transcripts. Execution uses existing committed-program tools and retained job IDs.
Every branch submission is also recorded as exposure in discovery before dispatch.
Completion delivers a compact scoped finding, brief, job IDs and handoff indices
at the caller anchor, including late outcomes after steering. Complete attempts,
source and unresolved issues remain available through explicit bounded reads. Discovery triages relevance
against the current question/revision and preserves adverse and superseded findings.
`read_research_investigations` inspects this discovery's own branches and retained
jobs, including unknown launches and execution faults, without private transcripts
or automatic mutation replay. Completed outcome delivery is idempotent; version-pinned
resume reconciles delivery without resubmitting studies. Host fault delivery is reused.

## Files

- `__init__.py`: Public workflow factory.
- `contracts.py`: Current launch schema, phase payload schemas and transition table.
- `definition.py`: General host specification, JSON extension, accepted-state fold and seeds.
- `prompts.py`: Proposal, critique, adjudication and constructive training execution guidance.
- `runtime.py`: Trusted launch/session capabilities, job retention, completion and asynchronous returns.

The package depends on shared experiment/program capabilities and runtime ports.
It imports no other concrete workflow. Parent exposure recording is an injected port.
