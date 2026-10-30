# Research investigation

Discovery calls `start_workflow` with `workflow: research-investigation` to delegate
one bounded research direction. This is the default home for most substantive
strategy implementation and testing. Discovery owns directions, allocation,
integration and the evolving shared sketch. Several instances can run concurrently and return
independently to their launching discovery conversation. This is a separate workflow;
the hypothesis-development phase table stays unchanged.

## Design and phase table

| Phase | Owns | Requested next phase |
| --- | --- | --- |
| `proposal` | Design, implement and execute an initial policy/study before critique; repair from evidence. | `critique`, `focused_critique` |
| `implementation_critique` | Check source fidelity, units, boundaries, timing, fills, state and executable controls. | `collect_critiques` |
| `experimental_critique` | Check comparisons, confounding, denominators, selection, support and feasibility. | `collect_critiques` |
| `alignment_critique` | Compare original operator intent with branch scope, alternatives and evidence-driven adaptation. | `collect_critiques` |
| `collect_critiques` | Pure deterministic collection of all three reports; no verdict or vote. | `adjudicate` |
| `focused_critique` | Verify the narrow repair and specialty requested in the accepted adjudication. | `adjudicate` |
| `adjudicate` | Resolve objections, make bounded repairs, assign proportionate independent review or delegate substantial work. | `critique`, `focused_critique`, `proposal`, `investigation`, `completed` |
| `investigation` | Author/validate/execute a training strategy or targeted diagnostic; inspect artifacts and interpret findings. | `critique`, `focused_critique` |
| `completed`, `failed` | Terminal outcomes. | None |

Proposal, reviews, adjudication and investigation are model-backed; collection is deterministic.
The `critique` route fans out to all three reviewer input ports. Each reviewer
feeds a distinct required port on `collect_critiques`; there is no split phase. The engine routes accepted contracts; it chooses
no statistic, research direction or scientific verdict. Review/research budgets are
declared in the brief and visible in accepted state. Runtime caps bound resource use;
they do not select studies or scientific conclusions. The execution cap (1..512) counts actual retained jobs, including individual batch
points, null replicates and technical repairs. Scientific variants are recorded
separately in accepted interpretation; a repair is not automatically a new hypothesis.
The same cap also bounds investigation entries, including blocked no-job rounds.
Scientific sufficiency remains
the judge's responsibility. Discovery and its branches progress autonomously;
mandatory approval in later hypothesis phases remains unchanged.

The current runtime contract is `research-investigation@6.0.0`. Older executions are rejected
for execution rather than decoded under the new contracts. Proposals include retained execution `attempts`, artifact `evidence`, and `repairs`
with original issue IDs and responses. Critics supply concrete `evidence` for every
issue and each `verifiedRepairs` entry (`issueId`, `evidence`). New issue IDs have
specialist prefixes, avoiding collisions in the merged ledger. The join preserves
all reports and does not decide whether a claim is valid. Only the model adjudicator
resolves findings. The accepted issue ledger carries unresolved and
repair-required objections across revisions; adjudication can explicitly accept a
limitation. Settled issues require `reopenReason` with new contradictory evidence
to be raised again under the original ID. `escalate` assigns a discovery-owned
decision without keeping it in the local repair queue; the completion outcome
returns those escalations to discovery. These fields record model decisions,
not engine-selected scientific rules.

Adjudication supplies `review` (`target`, `scope`, `issueIds`) for every continuing
action, including repair delegated to proposal or investigation. Completed
adjudication uses `review=null`. Narrow changes go directly to focused review;
substantive changes fan out to all three independent critics. Repair phases are
prompted to follow this assignment, broadening only for material changes with
an explanation. Adjudicate owns the same branch author/validate/run capabilities
and budget as the construction phases. It records its own `repairs`, `attempts`
and `evidence` in an immutable handoff; `reviewHandoff` identifies the changed
product and accepted state retains earlier adjudicator repairs. The prompt
requires independent verification for changes to code, calculations or empirical
claims; editorial clarification need not restart review. There is no new review
round cap, scientific gate or completion wake policy.

Seeds contain bounded original-brief and active-issue excerpts, pinned sketch and job
references, and exact accepted handoff indices. The existing
`read_research_investigations` tool reads complete accepted products by default
(up to 16,384 cl100k_base tokens). Required selectors are `execution_id` and
`handoff`; zero selects the brief, a positive index selects an immutable handoff,
and null selects current state or discovery branch inspection. `token_offset` and
`max_tokens` are optional. Large reads use exact token continuation via
`nextArguments`, preserve UTF-8 boundaries, and bind continuation to the content
SHA-256 so changing state cannot be spliced into an earlier page. `complete`,
`tokenCount`, `totalTokens` and `nextTokenOffset` expose read completeness. There
are no character pages or new section selectors.
A phase can read only its own branch. This is accepted-contract retrieval,
not transcript reading or search. Reentered phases retain their own conversations.
Critique compares original intent, approved construction, executed source and claims;
it distinguishes omitted tests from adverse evidence. Job-specific `read_study` and `read_strategy`
retrieves the immutable executed source rather than a subsequently edited program.

The original operator message is selected by explicit source ID and delivered once
as neutral initial context before each child's first runnable assignment. Revisited
phases receive only the changed assignment. Critics review plans or exact existing
implementations according to their specialty; candidate development belongs to the
proposal/investigation roles. Agent program tools return retained job IDs
immediately. Completion evidence is delivered to the owning conversation; agents
can continue other work or end their response without polling or resubmission.

The public brief contains direction, question, intended decision, constraints,
sketchReference, prior evidence references, revisionBudget and executionBudget. Runtime
composition derives parent identity, phase attempt and frozen data mandate from the
launching discovery agent. No model-supplied execution identity or window overrides.
Branches own separate program IDs and may replace/extend provisional implementations;
only discovery edits the shared strategy sketch. Launch snapshots the original
operator trigger message into `operatorRequest`, independently of the discovery-authored
brief. Every phase receives it, and handoff zero returns it alongside the brief.
Hard constraints and their origins must be distinguished from provisional choices.
Accepted handoffs carry the original brief and phase products, never private sibling
transcripts. Specialist attempts see identical accepted ancestors and cannot read
the current siblings' reports before the join. Substantive changes use the full
review fan-out; assigned narrow repairs use `focused_critique` without restarting three
reviews. No fixed strategy quota, experiment sequence, or scientific gate is added.
Execution uses existing committed-program tools and retained job IDs.
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
- `contracts.py`: Current launch schema, phase payload schemas and named routes.
- `definition.py`: Specialist fan-out and required-input specification, pure report collection, JSON extension, accepted-state fold and seeds.
- `prompts.py`: Proposal, three specialist critiques, focused repair verification, adjudication and constructive training guidance.
- `runtime.py`: Trusted original-request snapshot, scoped session capabilities and accepted reads with required selectors and stateless continuations, job retention, completion and asynchronous returns.

The package depends on shared experiment/program capabilities and runtime ports.
It imports no other concrete workflow. Parent exposure recording is an injected port.


Plan reviews do not verify later implementations. Investigators can return an early
working prototype through the existing review loop before a large search, with exact
program/version/job attribution. Focused correctness review checks consequential
mechanics; full specialist review remains for substantive construction or findings.
SDK building blocks cover exact partial-fill accounting, entry-notional returns,
empirical quantiles, OLS log-price slope and the defined Haar random-walk anchor.
Diagnostics keep diagnostic scope, and execution allocation follows observed cost.

Proposal, investigation and adjudicate own authoring tools and retain immutable job
submissions before dispatch. They annotate selected retained branch jobs;
this is attribution validation, not an execution-before-review
gate. The prompt directs first execution and diagnostic-only branches run their
assigned study. Async completion stays conversational and polling is prohibited.

Repair rounds can cite prior immutable branch jobs or exact launch-brief evidence
without rerunning them; reused branch evidence is checked against immutable
submission attribution.
