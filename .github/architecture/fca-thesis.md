# FCA Thesis Architecture

## Status

This document is the architecture and phased roadmap for **FCA Thesis**. It is
the design basis for the CLEO Thesis saga `T143` and its current descendants
through `T178`. The presence of those pending tasks records planned work; it
does not claim that the described services are implemented.

Implemented within this roadmap so far: the strategy-development vertical
workflow (`thesis.workflows`, T160), the hypothesis validation workflow with
verdict evidence (T161), bounded autonomous discovery with budget guards and
candidate accounting (T162, plus `thesis.agent.search_policy`), the RCA and
strategy revision loop with revision lineage (`thesis.workflows.rca`,
`thesis.research`, T163), the statistical-discipline substrate
(`thesis.experiments`: pre-registered experiment contracts, dependency-interval
audits, hash-chained attempt and influence ledgers, evidence grades with
explicit downgrade semantics — T164), and the harness MCP tool gateway
(`thesis.integrations.mcp`, `thesis.agent.harness_tools`, T281). The backend
process boundary (`thesis` entrypoint, `Dockerfile.thesis`) is implemented
(T178).

FCA Thesis is a workspace-scoped chat agent for generating, implementing,
testing, evaluating, and improving stock, option, and crypto hypotheses and
trading strategies. Its backend and UI are separate peers of `market-lake` and
`quant-harness`. Thesis uses their published contracts; it does not absorb
either product's responsibilities.

The working product and agent name is **FCA Thesis**, shortened to **Thesis** in
the UI and this document.

## Product Definition

### Mission

Thesis turns a conversation into an auditable research program. A user can:

- submit a market hypothesis and ask Thesis to test it;
- submit a hypothesis or strategy outline and ask Thesis to implement, test,
  diagnose, and improve it;
- ask Thesis to discover candidate strategies autonomously within configured
  cost, risk, and capability limits;
- upload supporting files and data to a workspace;
- steer autonomous work through chat while it is in progress;
- inspect plans, tool activity, backtests, evidence, limitations, and findings;
- package a strategy for paper, shadow, or live activation; and
- grant narrow, expiring permission for a separate gatekeeper to activate an
  approved strategy through `quant-harness`.

Thesis is not an execution venue and never places an order itself. The harness
owns backtest, paper, shadow, and live strategy runtimes. A deterministic
activation gatekeeper, not the LLM, decides whether an activation request has
valid consent and satisfies policy.

### Non-goals

- Reimplementing market data storage, backtesting, portfolio accounting,
  matching, risk, or broker routing.
- Guaranteeing that a strategy is profitable or that historical performance
  will persist.
- Treating a hidden ticker name as proof that a price series cannot be
  reidentified.
- Exposing hidden model chain-of-thought as an audit mechanism. Thesis records
  decisions, evidence, tool events, and concise rationales instead.
- Supporting multiple human users, organizations, or public multi-tenancy in
  the first release.
- Automatically publishing agent findings into the global knowledge base in
  the first release.

## Product Boundaries

The target FCA system has three product planes and four source/deployable
boundaries. Thesis's backend and UI are separate peers while remaining one
product:

| Product | Proposed path | Responsibility |
| --- | --- | --- |
| `market-lake` | `src/lake` | Point-in-time market data, catalog, coverage, ensure, and query contracts. |
| `quant-harness` | `src/harness` | Deterministic backtests, results, sweeps, paper/shadow/live runtimes, risk, and broker integration. |
| `FCA Thesis` | `src/thesis` | Workspace, chat, agent orchestration, workflows, evidence, knowledge retrieval, protected-test coordination, and activation requests. |
| `FCA Thesis UI` | `src/ui` | SvelteKit chat and workspace application, live execution status, plans, artifacts, evidence, and consent controls. |

`src/thesis` is a Python service so it can use FCA's typed Python clients and
tooling. `src/ui` is a separate SvelteKit/TypeScript application and deployable.
They are peers even though the UI depends on the Thesis API.

The global knowledge library is ultimately an organized collection of Markdown
files with explicit hyperlinks, metadata, and semantic-search representations.
Thesis initially owns the read/search subsystem while its ingestion pipeline is
a separately runnable component/process behind a stable ingestion contract. The
library can become a separate product later without changing workspace or agent
contracts.

Dependency direction is deliberate:

```text
global knowledge ingestion ---> global knowledge index
                                      |
                                      v
user <----> Thesis UI <----> Thesis API and agent runtime
                                  |       |       |
                                  |       |       +--> model and web providers
                                  |       +----------> workspace Git repositories
                                  |
                  +---------------+----------------+
                  |                                |
                  v                                v
             market-lake                     quant-harness
                                                  |
                                                  v
                                  paper/shadow/live strategy activation
```

The lake and harness must remain operable without Thesis. Thesis must consume
published lake and harness APIs, clients, schemas, manifests, and strategy
contracts rather than importing engine internals.

The FCA repository's root `strategies` directory remains reserved for
repository-owned examples and reference strategies. Thesis-generated and user
workspace strategies live only in `<workspace>/strategies`. The harness accepts
an immutable external strategy bundle or sandbox path; Thesis never copies a
workspace strategy into the FCA source checkout as part of normal operation.

## Runtime Modes

Local mode is a first-class supported runtime, not a development-only stub. In
local mode:

- the Python Thesis API and SvelteKit UI run on the operator's machine;
- each workspace is a separate local directory and Git repository;
- the durable queue and workspace coordinator run locally and recover across
  API or UI restarts;
- market-lake and quant-harness endpoints can be local or configured remote
  services; and
- model calls can use Azure AI Foundry or another configured model provider.

Azure AI Foundry authentication belongs to the local Thesis backend. The
preferred flow uses Microsoft Entra developer credentials through
`DefaultAzureCredential`, normally established by an interactive developer
sign-in such as Azure CLI authentication. The exact tenant, Foundry project,
endpoint, and model deployment are non-secret local configuration. Access
tokens remain process-scoped, are refreshed by the Azure identity library, and
are never sent to browser code or committed to a workspace. API-key fallback,
if later supported, uses the local secret store and is disabled by default.

Local startup performs an explicit provider-auth and model-deployment preflight
before launching autonomous work. Authentication failure leaves workspace and
offline UI access available but marks model-backed capabilities unavailable.
In Azure mode, the Thesis backend uses managed identity and the same model
router contracts; workspace Git repositories and execution resources are
separate Azure entities.

The initial UI is loopback-only and binds an operator session to the local
workspace owner. Binding to a non-loopback interface requires an explicit
authenticated/TLS deployment mode. Browser sessions, reconnects, CORS, CSRF,
and workspace authorization remain enforced even for the single-operator
release.

## Core Vocabulary

The UI and API use **workspace**, not project, for the primary user boundary.

| Term | Meaning |
| --- | --- |
| Workspace | A durable, isolated strategy-family repository. Thesis cannot operate outside a selected workspace. |
| Chat | A persistent conversational thread within a workspace. A workspace can contain many chats. |
| Investigation | A durable research objective that can span chats, strategies, experiments, and workflow executions. |
| Workflow execution | One end-to-end autonomous invocation launched from chat using a versioned workflow definition. It can pause, resume, and be steered. |
| Experiment | One evaluation protocol, potentially including a parameter sweep or many backtest jobs. |
| Experiment contract | The pre-result record of what an experiment is intended to test and how its evidence will be interpreted. |
| Backtest job | One deterministic invocation of the quant-harness backtest runner. |
| Strategy lineage | A related sequence of strategy versions and adaptations. It is the unit against which test exposure is tracked. |
| Protected test | An evaluation whose hidden data and release policy are controlled by a sealed-test authority outside the agent reasoning path. |

A strategy family can contain multiple related strategy lineages, including
competing implementations and discovery candidates. The workspace charter
defines the markets, objectives, or research relationship that makes them one
family. Thesis asks the user to create or export to another workspace when a
candidate is materially unrelated to that charter; it does not silently gain
access to another workspace to reuse related work.

The term **experiment contract** replaces the more academic term
"preregistration" in the product UI. Before Thesis sees an experiment's result,
the contract can lock the hypothesis, eligible data, partitions, metrics,
parameter search, market-impact assumptions, costs, success criteria, and
permitted follow-up actions. This constrains post-result changes and makes them
detectable; it cannot eliminate unrecorded external knowledge or every
researcher degree of freedom.

Thesis chooses the rigor of ordinary exploratory contracts. Protected tests
always require a complete, immutable contract. The contract and every revision
are visible to the user.

## Workspace Model

### Isolation Boundary

A user creates a workspace in the UI before starting a Thesis chat. The
workspace represents a strategy family and receives a fresh:

- private Git repository;
- CLEO project and database;
- chat and investigation namespace;
- agent memory and retrieval namespace;
- strategy, experiment, report, and activation history;
- tool capability and consent scope;
- scratch and execution namespace; and
- local or cloud authorization boundary.

No chat, file, memory, plan, strategy, result, credential, or workspace-derived
embedding crosses into another workspace unless the user performs an explicit,
audited export and import. The global knowledge library is the exception: all
workspaces can read it by default.

In local mode, each workspace is a separate directory and Git repository under
an operator-configured workspace root outside the FCA source checkout. In Azure
mode, each workspace is a separate Git-backed entity with isolated execution
and persistence resources. The exact Azure Git hosting mechanism remains an
implementation decision, but a shared branch in one repository is not an
acceptable substitute for a workspace boundary.

### Workspace Repository

All durable workspace domain content is stored in its Git repository, including
uploaded files and generated backtest artifacts. Credentials and secret values
are the only categorical exception: Git stores references to a secret store,
never secrets.

A new workspace should start from a versioned scaffold similar to:

```text
<workspace>/
|-- .cleo/
|   `-- cleo.db                    Workspace-scoped planning state
|-- .thesis/
|   |-- workspace.yaml             Identity, charter, limits, and settings
|   |-- platform.lock              Effective Thesis bundle and model policy
|   |-- events/                    Durable user-visible execution event logs
|   `-- journal/                   Accepted commands and publication outbox
|-- AGENTS.md                      Workspace-local bounded instructions
|-- chats/                         Thread records and compact summaries
|-- investigations/               Objectives, decisions, and finding indexes
|-- strategies/                    Python strategy code and tests
|-- experiments/                   Contracts, analysis, and evidence claims
|-- backtests/                     Manifests, metrics, traces, and reports
|-- uploads/                       Hashed user-provided evidence and metadata
|-- activations/                   Bundles, consent references, and outcomes
`-- README.md                      Workspace purpose and navigation
```

The exact serialization formats are deferred, but records must use stable IDs,
content hashes, and repository-relative references.

Storing every artifact in Git is simple and maximally self-contained, but it
has material consequences. Tick extracts, option traces, uploaded datasets,
and repeated reports can make repositories very large, clones slow, and commits
expensive. Thesis therefore needs workspace/repository size budgets, artifact
deduplication where possible, generated-file compression, and an explicit
future migration path. The first implementation must not silently move data to
an untracked object store, because Git-backed completeness is a chosen product
property.

The harness may initially produce artifacts in its own results store. Before an
experiment completes, Thesis imports every reproducibility-critical artifact
into an immutable content-addressed workspace path and verifies it against the
harness manifest. The harness copy is then a cache, not the workspace system of
record. Reaching a workspace Git budget pauses or rejects new artifact-producing
work; Thesis never silently prunes or externalizes existing evidence.

Operational leases, locks, provider-token caches, and queue indexes may exist
outside Git, but they are non-authoritative and rebuildable. Before acknowledging
an accepted user command, steering event, consent transition, or canonical
agent mutation, Thesis appends and flushes an idempotent record to the workspace
journal. The coordinator periodically publishes journaled transitions and
artifacts as serialized Git commits. Recovery reconciles the journal, Git head,
tool correlation IDs, and rebuildable queue state.

### Git And Concurrency

Multiple chats, investigations, workflow executions, strategies, and backtest
jobs may be active in one workspace. Their mutable scopes must be explicit:

- strategy edits occur on investigation or execution branches/worktrees;
- experiment and backtest artifacts use unique immutable directories;
- completed findings are merged or published into canonical workspace state;
- canonical strategy promotion and activation mutations are serialized; and
- Git commits include the workflow, model, input, and artifact identifiers that
  produced the change.

`.cleo/cleo.db` is intentionally tracked, but SQLite cannot be safely merged as
text. A workspace coordinator is the only CLEO writer. Concurrent agents may
propose plan mutations; the coordinator queues, applies, validates, and
publishes them under a fenced single-writer lease. Investigation branches do
not carry independently modified copies of the CLEO database.

A SQLite transaction and a Git commit are not one atomic transaction. Every
CLEO mutation therefore has an idempotency ID and journaled outbox record. The
coordinator checkpoints and closes WAL state, creates and integrity-checks a
consistent database snapshot, publishes it with its mutation record, and marks
the outbox item complete. Startup reconciliation safely completes or rolls back
an interrupted publication without replaying the logical mutation twice.

The workspace CLEO profile tracks exactly the canonical `.cleo/cleo.db` and
excludes WAL, journal, backup, credential, and process-runtime files. Only the
coordinator's canonical worktree contains a writable database. Agents and
investigation worktrees read plans and submit proposed changes through the
coordinator API.

## CLEO Role

Each workspace owns one durable CLEO project. It sits above all investigations
in that workspace rather than being recreated for each chat, experiment, or
backtest. A new workspace starts with a completely fresh CLEO project; nothing
is inherited from another workspace.

CLEO provides:

- the workspace research and implementation plan;
- dependency-aware decomposition across investigations and strategies;
- visible task state and evidence links in the UI;
- a structured handoff between fresh agent sessions; and
- an auditable planning record committed with the workspace.

Users do not need to know CLEO commands or tell Thesis to use CLEO. Versioned
workflow instructions tell Thesis when to inspect, update, reconcile, and
validate the plan. The UI renders CLEO concepts as the workspace plan and may
offer an optional diagnostic view with native IDs.

CLEO is not the durable job queue and does not own in-flight model or tool
execution. The Thesis execution service owns checkpoints, leases, retries,
cancellation, and recovery. This separation makes CLEO durable planning state
while allowing each model invocation or subagent context to be fresh and
least-privileged.

## Agent Runtime

### Durable Chat And Autonomous Work

A chat message can answer synchronously or launch a workflow execution. An
execution persists independently of the browser connection and reports through
a durable event stream. At minimum, the backend stores:

- the triggering user message and attachments;
- selected workflow and effective workflow version;
- current custom phase and phase history;
- CLEO tasks and investigation links;
- effective instruction/skill bundle hashes;
- model roles, provider model identifiers, and registry records;
- tool submissions, correlation IDs, status, and results;
- budget use and policy decisions;
- resumable checkpoints; and
- final findings, evidence claims, limitations, and artifact links.

The append-only workspace journal is the portable record of accepted execution
transitions. The operational queue is a rebuildable projection over that
journal plus outstanding external tool jobs. Durable execution uses fenced
leases, idempotent submissions, command deduplication, versioned checkpoints,
and explicit failed/dead-letter states from the first vertical slice.

Workflow phases are defined independently for each workflow. Thesis does not
force unrelated research requests through one superficial phase taxonomy. The
UI renders whatever phases the selected workflow declares.

### Chat And Execution Boundary

The chat is the sole messaging surface. User messages and agent narration are
all chat messages on a `ChatThread`; the test for the boundary is whether a
record would be part of the conversation with a human analyst. An execution is
a work unit, not a messaging surface: it owns a machine event timeline (phase
transitions, tool submissions and results, steering acknowledgements) that the
UI renders inline in the chat, plus execution-scoped control-plane commands
(start, pause, stop, cancel). Steer, interrupt, and context requests are chat
messages; start/stop/cancel are control commands; phase, tool, and log detail
belongs to the execution timeline.

Steering is expressed by posting a chat message that optionally carries
structured steering fields (for example `targets_execution_id` and a
steering-intent token). Posting the message is the durable acknowledgement;
the service then percolates the intent down to executions:

- an explicit target must belong to the chat and be in a steerable state
  (queued, running, or paused);
- without an explicit target the intent percolates to every active execution
  of the chat;
- each targeted execution gets exactly one monotonically increasing intent
  epoch per steering message, and a `workflow.steered` event records the
  message link, the new epoch, and the steering intent for the orchestrator;
- the orchestrator observes the event and replans at the next safe model/tool
  boundary; steering does not cancel already submitted backtests, searches, or
  other tool jobs unless the user explicitly requests cancellation.

When an earlier tool completes after steering:

- its result remains attached to the investigation and visible in the event
  timeline;
- Thesis decides whether it is still relevant, supplementary, or superseded;
- the result is never silently discarded; and
- an idempotent job can be reused rather than submitted again.

Every model continuation, tool result, CLEO proposal, Git publication, and
finding carries its intent epoch. A stale epoch may attach a completed result as
evidence but cannot mutate canonical strategy, plan, contract, or findings
until Thesis reevaluates it under the current epoch. Workflow definitions state
their maximum safe-boundary latency and which operations support pause, stop,
or cancellation.

Stop and cancel are separate explicit controls. A UI disconnect or Thesis
service restart does not stop an execution; the durable queue and checkpoints
resume it.

The UI-facing HTTP surface follows this split. `POST
/v1/workspaces/{ws}/chats/{chat}/messages` is the steering entry point and
returns the persisted message plus the percolation result; the execution
route `POST /v1/workspaces/{ws}/executions/{id}/steer` survives only as a
deprecated alias that posts a steering message into the execution's
originating chat so transcripts hold every steering act.

Executions are streamed over Server-Sent Events at `GET
/v1/workspaces/{ws}/executions/{id}/events/stream`. The stream fans out both
surfaces: durable execution machine events (SSE `id` is the replay cursor, so
`Last-Event-ID` resume reuses the existing cursor semantics through
reconnects) and, per chat subscription, chat message events as ephemeral
frames without an `id` that clients deduplicate by message ID. Provider token
deltas are allowed as a documented ephemeral `provider.delta` event kind
during model phases; ephemeral events are never replayed from the event
store.

### Model Router

Thesis is not coupled to Fable 5.1 or any other single model. A policy-driven
model router assigns models to roles such as:

- primary chat and workflow orchestration;
- current or historical web research;
- Python strategy implementation;
- statistical and quantitative critique;
- code and harness-contract review;
- adversarial overfitting review; and
- cutoff-constrained protected-result RCA.

Every invocation records the provider, exact model/version, role, declared
knowledge cutoff, cutoff evidence, effective instruction hashes, allowed tools,
and input/output artifact references. An influence ledger also records the
human messages, retrieval results, uploads, tools, source effective dates, and
prior outputs that influenced hypothesis generation or strategy selection.
Routing policy, not a prompt, enforces contexts that cannot browse, inspect
mappings, deploy, or access workspace history.

The main agent delegates at its discretion during ordinary work. A workflow may
also require separately scoped subagent roles at named phases, such as
statistical critique before a protected test or code review before packaging.
Subagents receive fresh, scoped contexts and return cited findings rather than
sharing an unbounded conversation transcript. A workflow may call a reviewer
independent only when it records pre-result assignment, model/provider
dependence, blinding, evidence access, and conflict-resolution rules.

### Platform And Workspace Instructions

The effective agent bundle has explicit precedence:

1. Signed Thesis platform safety, isolation, evidence, and deployment policy.
2. Versioned built-in workflow and skill definitions.
3. Workspace-local instructions and workspace-trusted knowledge preferences.
4. Chat instructions and uploaded evidence.
5. Untrusted retrieved or external content.

Workspace overlays may select methods, conventions, research preferences, and
additional skills. They cannot weaken workspace isolation, change evidence
grades, grant tools, expand deployment consent, reveal a protected mapping, or
override signed platform policy. Every workflow execution pins its effective
platform bundle so later platform upgrades do not rewrite historical context.

### Tool Surface

Tools are narrow capabilities rather than arbitrary credentials:

| Tool family | Allowed responsibility |
| --- | --- |
| Workspace Git | Read, branch, edit, test, checkpoint, and publish within the selected workspace. |
| CLEO | Read and propose workspace plans; mutations pass through the single-writer coordinator. |
| Market lake | Ensure and query point-in-time equity, option, and crypto data through published contracts. |
| Quant harness | Validate strategy code, launch deterministic backtests/sweeps, and query complete result artifacts. |
| Code sandbox | Run generated Python and tests with bounded CPU, memory, filesystem, and no implicit network or secrets. |
| Knowledge search | Retrieve global versioned pages and citations under trust-tier rules. |
| Web research | Search current or archived sources only in workflow contexts whose policy allows it. |
| Protected testing | Submit immutable experiment and strategy bundles without exposing hidden mappings or windows. |
| Deployment request | Ask the deterministic gatekeeper to validate an artifact and consent grant. It is not a broker tool. |

Global and workspace budgets constrain model tokens, web calls, lake data,
backtest count and concurrency, wall time, compute, repository growth, strategy
classes, and deployment eligibility. Autonomous discovery may choose any
supported market, horizon, and strategy direction only within those bounds.

#### Harness MCP Tool Gateway (implemented)

The quant-harness tool family is implemented as an MCP (JSON-RPC 2.0) tool
gateway rather than a planned capability. The harness publishes its backtest,
sweep, run-query, artifact, and supervised-trader operations through MCP; Thesis
consumes them through `thesis.integrations.mcp` and `thesis.agent.harness_tools`.
The client discovers the catalog with `tools/list` and executes it with
`tools/call`, so a launch can only route through a tool the harness actually
publishes.

The gateway covers all four Thesis/harness topology combinations through one
selectable transport:

| Thesis | Harness | Transport |
| --- | --- | --- |
| local | local | In-process MCP handler for a single-process runtime, else a `StdioMcpTransport` that spawns `harness mcp` and exchanges newline-delimited JSON-RPC over stdio. |
| remote | local | `StdioMcpTransport`; valid only when the Thesis process can reach a local `harness` executable, so operators co-locate the processes or inject an in-process handler. |
| local | remote | `RemoteHttpMcpTransport` posting JSON-RPC to `POST /mcp` with a bearer credential. |
| remote | remote | `RemoteHttpMcpTransport` posting JSON-RPC to `POST /mcp` with a bearer credential. |

A remote harness endpoint (`HARNESS_MCP_URL`) wins over a local command
(`HARNESS_MCP_COMMAND`); `THESIS_LOCATION` names where Thesis runs and
`HARNESS_API_BEARER_TOKEN` supplies the remote credential. The bearer token is
sent only as an `Authorization` header and is excluded from every
representation. `thesis.agent.harness_tools` wraps the client in a
`HarnessToolProvider` that adds typed arguments, structured results, and
agent-visible failures, and the `harness-mcp` skill is registered into the
`SkillRegistry` used to build the model router so role policies resolve the
bundle and permit `HARNESS_TOOL_NAMES`. A `HarnessToolDispatcher` enforces the
resolved `allowedTools` before routing a model tool request to the provider.

Backtest launches route through the MCP `run_backtest` tool via a
`HarnessMcpBoundary` whose capabilities derive from the discovered catalog. The
local composition prefers the MCP boundary and falls back to the direct
`LocalHarnessProcess` process boundary when the MCP server cannot be started;
a remote harness never silently falls back to local execution.

## Research Workflows

Workflow definitions are versioned, have custom phases, specify required
artifacts, and can declare phase-required subagents. They are enforced by the
orchestrator and evidence services, not only described in a system prompt.

### Hypothesis Validation

This workflow starts from a user-supplied claim, including claims that do not
yet define a tradable strategy. A typical workflow can:

1. Operationalize the claim into measurable variables and falsifiable outcomes.
2. Identify assumptions, causal story, alternative explanations, and required
   point-in-time data.
3. Create a visible experiment contract with an agent-selected rigor level.
4. Check data availability, quality, corporate actions, listings, option-chain
   construction, exchange calendars, and leakage hazards.
5. Run the statistical test and robustness checks.
6. Ask a separately scoped statistical critic to challenge the method when required
   by the workflow or chosen by Thesis.
7. Report support, contradiction, uncertainty, limitations, evidence grade,
   and reproducible artifacts.

Before making a nonexploratory claim, the experiment contract classifies it as
descriptive, predictive, or causal. It records the primary estimand and
endpoint, null/alternative where applicable, minimum relevant effect,
uncertainty method, effective sample-size assumptions, missingness policy,
power or precision target, and stopping rule. Causal claims additionally
require an explicit identification design and assumptions; ordinary association
and backtest attribution use diagnostic rather than causal language.

A failed hypothesis is a valid result. Thesis must not automatically mutate a
claim into a different claim merely to produce a positive conclusion.

### Strategy Development And Improvement

This workflow starts from a hypothesis, strategy outline, or existing Python
strategy. A typical workflow can:

1. Clarify the economic premise, instruments, horizon, execution assumptions,
   risk limits, and success criteria.
2. Research relevant methods using the global library and permitted web
   sources, retaining citations and effective dates.
3. Implement the strategy against the public quant-harness strategy contract.
4. Run unit, contract, determinism, and sandbox checks before backtesting.
5. Establish a baseline and verify the backtest manifest and data fingerprints.
6. Run bounded parameter sweeps, robustness tests, and cost/slippage stresses.
7. Diagnose shortcomings without conflating an engine defect, data defect,
   statistical noise, and a genuinely unsupported premise.
8. Create a new strategy version for each material adaptation and preserve the
   complete lineage and experiment exposure history.
9. Use ordinary validation and separately scoped overfitting critique before any
   protected test.
10. Optionally submit a protected test, perform cutoff-constrained blinded RCA,
    and report findings under the protected-test policy.
11. Package an immutable strategy bundle and, only with consent, request paper,
    shadow, or live activation from the gatekeeper.

### Autonomous Strategy Discovery

Thesis may choose the market, hypothesis, and strategy direction without a
user-supplied premise. It remains bounded by the workspace charter and platform
budgets even when the charter is broad. A discovery workflow should separate
cheap candidate generation from expensive backtests, track every attempted
candidate, and correct its conclusions for the size and adaptivity of the
search.

Typical required behaviors include:

- broad research and explicit economic or structural rationales;
- separately scoped novelty and plausibility critique;
- cheap data sanity and placebo tests before implementation;
- bounded candidate triage rather than unreported mass search;
- observed-and-declared-attempt-ledger-aware statistical evaluation;
- parameter stability and regime analysis;
- promotion of negative as well as positive findings; and
- stopping when budgets are exhausted or remaining uncertainty cannot be
  resolved with available evidence.

### RCA And Iteration

Root-cause analysis is a structured diagnosis, not a synonym for parameter
tuning. Before changing a strategy, Thesis should classify evidence among:

- data availability, integrity, or point-in-time defects;
- harness configuration, accounting, risk, fee, or fill-model defects;
- implementation errors or unintended strategy behavior;
- market-regime dependence;
- parameter instability or excessive sensitivity;
- sparse exposure, low statistical power, or random variation;
- execution costs and capacity; and
- failure of the economic premise.

RCA outputs candidate explanations, supporting and conflicting evidence,
confidence,
and discriminating follow-up experiments. A change creates a new lineage
version and must be evaluated on data that the applicable experiment contract
allows.

## Statistical And Backtest Discipline

Thesis must not equate a profitable backtest with validated evidence. Workflow
and skill libraries should support, where appropriate:

- time-ordered train, validation, and holdout partitions;
- purged or embargoed walk-forward validation for overlapping labels and
  positions;
- nested tuning so parameter selection is separated from evaluation;
- accounting of every system-observed and user-declared hypothesis, generated
  or ranked candidate, model sample, aborted job, variant, parameter, metric,
  market/horizon choice, and holdout look;
- corrections or explicit downgrades for multiple and adaptive testing;
- parameter-surface stability rather than selection of a narrow optimum;
- block bootstrap, placebo, permutation, and perturbation tests suitable for
  dependent financial series;
- cross-time, cross-asset, and regime checks when the premise predicts them;
- realistic fees, spreads, latency, fills, assignment, liquidity, and capacity;
- survivorship-free universes and point-in-time option contracts;
- sensitivity to missing data and provider/schema changes; and
- benchmark, attribution, drawdown, tail, turnover, and exposure analysis in
  addition to aggregate return ratios.

For every nonexploratory experiment, the contract predeclares its primary
endpoint, multiplicity family, candidate-selection rule, sequential or reuse
method, release budget, and stopping rule. Fixed-family corrections are not
presented as sufficient for unrestricted adaptive LLM search. Full-result RCA
adaptation against a window receives the strongest selection/reuse modifier and
cannot be described as independent confirmation on that window.

Before validation or protected evaluation, Thesis performs a dependency-
interval audit over feature lookback, label horizon, warm-up, position life,
liquidation, revised data, options/underlier relationships, and adjacent
windows. Purging and embargo are derived from the maximum relevant information
and position interval rather than selected as arbitrary dates.

Steering after outcome-correlated information versions or closes the current
experiment contract and enters the resulting choice in adaptive-selection
accounting. Forward observation additionally locks a horizon or valid sequential
stopping rule, reporting cadence, and missing-data policy. Any adaptation starts
a new strategy and contract version.

The exact default anti-overfitting protocol remains open. The vertical slice can
start with time-ordered exploration and validation, then add a sealed-test
authority. The final policy should be selected empirically and can vary by
asset class and strategy horizon, but its use and version must be recorded.

## Evidence Grades

Historical evidence must describe what was hidden from which actor. Thesis uses
explicit grades and modifiers instead of calling every unseen backtest "clean."

| Grade | Meaning |
| --- | --- |
| Exploratory | In-sample, adaptively inspected, or insufficiently provenance-controlled data used to form or tune the hypothesis. |
| Historical validation | Time-separated data evaluated after all claim-relevant choices were locked, with access provenance supporting non-use for hypothesis, candidate, universe, metric, or parameter selection. |
| Sealed historical | A test authority hid the test during the workflow and released it under its exposure policy. Model cutoff, influence, reuse, and transformation facts appear as modifiers rather than being hidden in the grade name. |
| Forward observation | Paper or later observation generated after the strategy and protocol were fixed. |

Grades are not a universal scalar ranking. A short forward sample can be weaker
than a broad historical test for many questions. They specifically state the
result's exposure and model-knowledge conditions.

These grades are separate from the RCA subagent protocol tiers. The versioned
evidence policy defines RCA subagent tiers and their required protocols. The
second-highest RCA tier uses cutoff-constrained RCA as standard practice: the
assigned RCA subagent uses a registry-verified model whose conservative
knowledge cutoff is before the earliest model-visible observation. This is a
protocol selected by the evidence tier, not a special Thesis mode or optional
feature. The highest RCA tier and the remaining lower tiers are defined by the
policy; a subagent cannot silently select a weaker protocol than the requested
tier.

Modifiers record whether evidence is reused, selection-exposed,
knowledge-exposed, influence-unknown, transformed, underpowered, or otherwise
limited. Current/post-window or unknown human, retrieval, upload, tool, or source
inputs produce an appropriate influence modifier even when model cutoffs are
prewindow. If an experiment contract permits reuse of a previously exposed
protected window, Thesis records every look and adaptation, uses the declared
adaptive-testing controls, and downgrades the evidence. New aliases or an older
RCA model do not restore sealed status.

## Protected-Test Authority

The protected-test authority is a non-LLM service outside the Thesis reasoning
path. It owns hidden window selection, real identity/date mappings, release
policy, protected exposure history, and sealed-status facts. Thesis submits:

- an immutable experiment-contract hash;
- an immutable strategy bundle and configuration hash;
- the submitted strategy-lineage and test-exposure identifiers;
- requested diagnostics; and
- model-registry identities for generators, selectors, and proposed RCA roles.

The authority verifies eligibility, invokes `quant-harness`, stores the real
result, and releases policy-approved artifacts to named actors. Thesis never
receives the mapping table. Repeated access and holdout reuse are governed by
the experiment contract and authority-owned exposure ledger rather than by a
universal one-shot rule.

Protected data comes from a preallocated pool excluded from generator and
selector lake/query tools. Window allocation follows a versioned,
strategy-independent randomization or stratification policy committed before
the authority inspects the submitted outcome. A dependency-interval audit
accounts for warm-up and economically linked adjacent records. The authority
reconciles known prior access before release and records unavoidable public or
unknown exposure as evidence modifiers.

The authority does not trust workspace lineage IDs as proof of novelty. It
derives bundle fingerprints, retains signed ancestry through workspace
export/import, tracks protected exposure for the local operator across
workspaces, and applies conservative code/behavior similarity checks. When
prior external exposure cannot be established, it emits an
`unknown-prior-exposure` fact instead of granting a fresh lineage budget.

The authority is the canonical protected-eligibility and exposure owner. It
emits signed sealed-status, allocation, influence, and reuse facts. The Thesis
evidence service keeps a read-only workspace mirror and deterministically maps
those facts into user-facing grades; it cannot override them. If a generating,
selecting, or RCA model's knowledge cutoff overlaps the evaluation window, the
result remains usable but carries a `cutoff-overlap` modifier. The modifier is
not a substitute for the RCA tier selected by the evidence policy.

A protected runner uses an allowlisted input/output schema rather than releasing
arbitrary harness files. It removes or transforms free-text tags, paths, logs,
errors, filenames, numeric encodings, timing, and resource side channels. Static
and dynamic release checks reject an artifact set that can carry undeclared
identity information.

## RCA Aliasing Strategies

Aliasing and de-identification are not user-facing Thesis modes. They are two
authority-side aliasing strategies that the protected-test system may invoke
when releasing a result to an RCA subagent. The authority selects the strategy
using the requested RCA evidence tier, diagnostic utility, and empirical
reidentification gates.

### Identity Masking Strategy

- Generate a fresh cryptographically random alias set for every RCA release.
- Replace tickers with opaque tokens such as `asset_017`.
- Replace dates and timestamps with neutral counters such as `t=8321` while
  preserving only the intervals needed for analysis.
- Replace sector, venue, and entity labels with independently randomized neutral
  tokens.
- Never derive aliases from a public hash of the real value.
- Keep mapping state inside the authority and outside every model-visible
  prompt, tool, log, and artifact.

This protects the mapping itself. It does not make a distinctive price path,
crash, option surface, trading calendar, or correlation structure anonymous.

### Hardened De-identification Strategy

The hardened de-identification strategy additionally transforms or suppresses
fingerprints such as
calendar alignment, absolute scale, sector labels, known event boundaries,
cross-asset correlation precision, and other quasi-identifiers. Transformations
must preserve the specific relationships needed for RCA and declare any lost
fidelity. Hardened output is best-effort de-identification, not a promise of
information-theoretic anonymity.

The RCA model receives the original strategy source as selected in this design.
Before release, the authority scans it for real tickers, dates, entity names,
comments, filenames, endpoints, and configuration literals. If source itself
reveals or encodes an identity, the authority blocks protected RCA under either
aliasing strategy. It does not rewrite original source or claim mapping secrecy after source
has defeated it.

Fake entities and decoy series are used only in red-team tests of the aliasing
system. They are not mixed into model-visible RCA evidence, where they could
corrupt causal analysis. Reidentification tests should measure attacks based on
price paths, event signatures, calendars, correlations, option surfaces, and
strategy source.

Fresh aliases are not independent privacy resets. The authority maintains a
cross-release disclosure ledger for overlapping windows, underlier/derivative
relationships, correlated assets, repeated traces, and prior model recipients.
Each aliasing strategy has empirical attack and diagnostic-utility thresholds;
RCA release is blocked when either threshold fails, and residual
reidentification risk remains part of the report.

## RCA Subagent Evidence Protocol

Cutoff-constrained RCA is the standard practice for the second-highest RCA
evidence tier. The assigned RCA subagent analyzes a protected result only. It
never generates the original hypothesis, selects the strategy, ranks
candidates, edits strategy code, or decides promotion.

Before every RCA session, a model registry verifies that:

- the exact provider model/version has a signed build/snapshot or provider
  attestation covering training, fine-tuning, augmentation, memory, and
  retention behavior;
- the attested conservative cutoff upper bound is not revoked or unknown;
- that upper bound is strictly before the earliest model-visible real-time
  observation, including warm-up and lookback data; and
- the model route has no current web, real mappings, workspace transcript,
  prior protected outputs, or deployment capability.

No model names or cutoff dates are hard-coded into the architecture. If no
eligible model exists, or an exact provider deployment can change weights
without a new registry identity, Thesis reports that cutoff-constrained RCA is
unavailable rather than silently substituting a newer model.

The RCA context receives the original strategy source plus the full policy-
approved aliased run: transformed market records, signals, orders, fills,
positions, metrics, attribution, and traces. It also receives the
policy-approved aliased experiment contract, strategy/harness configuration,
dependency versions, benchmark, costs, data-quality manifest, and a
transformation manifest explaining preserved and lost semantics. A diagnosis
that depends on suppressed units, calendars, or relationships is marked
unavailable. RCA outputs only:

- candidate causes;
- cited supporting and disconfirming observations;
- uncertainty and alternative explanations; and
- proposed discriminating tests.

The full aliased run is released only to the named RCA model session. A
deterministic egress filter gives the main Thesis agent schema-limited advice
that excludes raw aliases, identifying values, trace quotations, and web-query
material. Exposure is recorded per actor: authority, protected runner, RCA
provider/session, main agent, user, and operator. The main agent may use the
filtered advice to revise a strategy, but revisions return to exploratory or
validation data. If the experiment contract permits another look at the
consumed window, the evidence service marks the reuse and downgrades the
resulting claim.

Using a registry-attested older model constrains one documented source of
hindsight available to that diagnosis. It does not prove what the weights
contain or make a strategy-generation process unbiased. The evidence grade and
influence ledger separately account for the models and external information
involved in hypothesis generation and strategy selection.

## Global Knowledge Library

The knowledge library is global and readable by every workspace by default. It
is a versioned evidence library for trading, market structure, statistics,
backtesting, data quality, risk, execution, and FCA operational knowledge.

The canonical corpus is a directory tree of Markdown files. Each page is a
normal Git-versioned `.md` file; front matter or a sidecar index supplies
structured metadata without moving the knowledge itself into a database. Each
page or source record should carry:

- stable identity and version;
- title, links, tags, and typed relationships;
- source URL or origin and content hash;
- author/publisher where known;
- publication, retrieval, and effective dates;
- asset class, market, method, and applicability metadata;
- supersession and contradiction links;
- curation and trust state; and
- lexical and semantic-search representations, generated as rebuildable index
  data rather than canonical content.

Search should combine hyperlink traversal, lexical retrieval, metadata filters,
semantic embeddings, and optional reranking. The embedding/index implementation
is replaceable; word2vec is one possible baseline, not a contract. Search
results always retain the Markdown path, Git revision, page version, and source
provenance so a workflow can cite the exact corpus state it used.

The separate ingestion pipeline fetches, parses, deduplicates, links, embeds,
and proposes Markdown revisions. Curation and publication are explicit Git
operations; a fetched page does not automatically become trusted knowledge.

Each workspace has full read access to the global Markdown corpus by default.
The agent can retrieve pages directly through the knowledge service. The
recommended initialization behavior is not to load the full corpus into CLEO:

- CLEO `Nexus` is intended for code intelligence and cross-project code/task
  relationships, not a general trading-knowledge corpus.
- CLEO `Brain` is a memory/runtime surface. It can consume Markdown memory
  files, but its database is not the canonical global library and bulk-loading
  the corpus would pollute workspace memory and context.
- Thesis may seed the workspace CLEO project with a compact, version-pinned
  knowledge manifest or selected reference records that link back to global
  Markdown paths and Git revisions.

That projection is optional, rebuildable, and never a second canonical copy.
The exact CLEO projection mechanism remains a later implementation decision;
the repository currently contains no FCA contract that names it `Nexus`,
`Brain`, or another facility.

Trust tiers are:

| Tier | Behavior |
| --- | --- |
| Signed platform policy | May instruct Thesis within its declared scope and has highest knowledge-tier precedence. |
| Workspace-promoted page | May guide methods or preferences in that workspace, but cannot override platform invariants or grant capabilities. |
| Curated evidence | Can support cited factual or methodological claims but is not an instruction source. |
| Untrusted source | Treated only as quoted external data and isolated from agent instructions. |

Workspace promotion changes interpretation, not global content or other
workspaces' trust. A workspace can mark selected global pages as preferred
references for its agents, but cannot change the source Markdown or its global
trust tier. The first roadmap does not allow agent findings to publish directly
into the library. A later design can add a proposal, review, provenance, and
conflict-resolution pipeline.

## Uploads And Untrusted Content

Chat supports file upload. Every upload is workspace-scoped untrusted evidence
by default. Thesis:

- stores the original with a content hash and provenance record;
- scans and parses it in a sandbox;
- distinguishes data, prose, code, and archives;
- never treats embedded prompts or tool instructions as platform instructions;
- prevents paths, links, macros, and code from escaping the workspace sandbox;
- does not forward secrets to model providers; and
- records which model and claim consumed which version.

A user can deliberately convert material into a workspace instruction or
workspace-trusted knowledge reference through a separate, visible action, still
bounded below platform policy.

## Quant-Harness Integration And Activation

Generated strategies are Python implementations of the public quant-harness
strategy contract. A strategy bundle has immutable content identity from the
first backtest and contains at least:

- strategy source and tests;
- dependency and platform versions;
- harness configuration and canonical hashes;
- risk and capability declarations;
- data and backtest manifests;
- evidence summary and known limitations; and
- content-addressed integrity record and, when activation-eligible, an artifact
  signature or equivalent attestation.

Paper, shadow, and live activation are disabled by default. Natural-language
chat can initiate and explain a consent proposal, but ambiguous agreement cannot
authorize activation. Grant creation requires an explicit structured
confirmation in chat or the UI/API that displays the exact bundle hash and
scope. A scoped, revocable, expiring consent grant
names:

- the exact strategy bundle and version;
- paper, shadow, or live environment;
- permitted activation action;
- capital, instruments, risk limits, and other applicable bounds;
- grant creation and expiration times; and
- whether the grant is one-shot or valid for a bounded interval.

Pre-authorization and just-in-time authorization use the same grant model. Live
confirmation can additionally require operator reauthentication. A separately
owned deterministic gatekeeper validates the grant, bundle integrity, tests,
harness capabilities, environment, and risk policy before invoking harness
strategy activation. It rejects ambiguity and cannot ask the LLM to reinterpret
the grant. Thesis receives neither broker nor gatekeeper credentials. All
proposals, confirmations, requests, decisions, revocations, and activation
outcomes are committed to the workspace audit record.

Only the harness runtime receives broker capability. Thesis can monitor and
explain an activation through published status and result APIs, but it does not
receive an order-placement tool or independently deploy strategy compute.

### Harness-Owned Runtime

The quant-harness product owns portfolio tracking, OMS behavior, strategy
runtime shells, risk controls, broker integration, state recovery, runtime
supervision, and the compute process that executes a strategy. Thesis owns the
research workflow, bundle, consent request, and evidence; it asks the harness
or its activation gatekeeper to run the bundle.

The existing quant-harness plan specifies these Azure deployment boundaries:

| Harness capability | Planned Azure shape | Responsibility |
| --- | --- | --- |
| Quant API | Azure Container App with scale-to-zero behavior | Authenticated backtest launch, result queries, manifests, metrics, artifacts, and status. |
| Backtest and parameter sweep jobs | Azure Container Apps Jobs | Finite deterministic backtests and parallel or seeded sweep executions, launched through the quant API using managed identity. |
| Paper/trader runtime | Scheduled Azure Container App trader workload | Long-running paper or shadow process using live feeds, the shared simulation venue, virtual books, risk, recorder, state restore, and supervision. |
| Optional crypto trader | Always-on Azure Container App workload when explicitly enabled | Continuous crypto paper/live-data processing under additive budget and capability controls. |
| Live broker boundary | Harness broker adapter behind capability and go-live gates | Broker orders, execution reports, reconciliation, kill switch, and live eligibility. |

Current repository status matters here: the market-lake Azure Container Apps
deployment is implemented, but the quant-harness Azure resources are not yet
implemented. The harness `api`, `backtest-job`, and `trader` commands currently
define process boundaries and are scaffolded for the later runtime work. Thesis
must report harness capability as unavailable until the corresponding harness
contracts, process, and deployment gates are actually present.

The Azure infrastructure extension is planned in quant-harness tasks T112-T114:
Bicep extends the shared FCA resources with trader apps, backtest jobs, quant
API, results/state storage, kill-switch state, managed identities, separate
paper/live secrets, monitoring, and additive cost controls. The deployment
workflow uses the existing immutable image, GHCR/ACR image flow, GitHub OIDC,
and environment-protected Azure deployment path. T114 adds restart
supervision, health/status reporting, state recovery, graceful shutdown, and
operator procedures. T102 specifically plans to launch backtests through Azure
Container Apps Jobs.

This is a quant-harness deployment design, not a Thesis deployment design.
Thesis should submit an immutable bundle and activation request to the harness
API/gatekeeper, then consume harness status and artifacts. It should not select
between containers and VMs. VMs are not part of the current harness plan; if a
future strategy requires one, that is a new harness infrastructure decision with
its own security, cost, and operational gates.

### Harness Local Mode

Quant-harness must support a first-class local running mode in addition to its
Azure deployment. The same strategy implementation and public contracts should
run locally through the shared process boundaries established by T078:
`api`, `backtest-job`, and `trader`. The current command implementations are
scaffolds, so local runtime behavior is a required harness roadmap item rather
than an already-supported capability.

Local mode should provide:

- deterministic backtests and parameter sweeps as local processes;
- local paper trading through `PaperShell` with live feeds, simulated fills,
  virtual books, risk, recording, state snapshots, and recovery;
- local shadow operation with observe-only broker routing when the user has
  explicitly configured the required credentials and consent;
- a capability-gated local live shell only if the harness live boundary permits
  it, with the same kill switch, reconciliation, and go-live controls as Azure;
- local filesystem results/state and a local catalog or equivalent adapter,
  while preserving the same manifests and artifact contracts as Azure;
- runtime credentials from the operator's environment or local secret store,
  never from Git; and
- no Azure CLI, Azure subscription, managed identity, Key Vault, or Azure
  credential requirement for local harness execution unless the selected
  external dependency explicitly needs one.

Local and Azure are deployment adapters around the same harness runtime
contracts, not different strategy implementations. Thesis local mode invokes
the local harness process or API; Thesis Azure mode invokes the authenticated
remote harness API. Azure AI Foundry authentication remains a Thesis backend
concern for model calls and is not a substitute for the harness's market-data
or broker credentials.

## UI Experience

The first release is for one authenticated local operator. The data model still
uses explicit workspace ownership so future authorization does not require
collapsing existing boundaries.

The Thesis UI should provide the chat-first workspace experience described here,
but its visual design and detailed interaction design are intentionally deferred
to a later UI design effort. This architecture calls out the UI boundary and
required capabilities without locking layouts, navigation, or component
styling.

The Thesis UI should eventually provide:

- workspace creation, selection, archive, and policy/budget settings;
- multiple persistent chat threads per workspace, with create, rename, archive,
  search, unread/running state, and investigation linkage;
- drag-and-drop file uploads with provenance and scan status;
- streamed assistant messages and concise execution events;
- custom workflow phase timelines with pause, resume, steer, stop, and cancel;
- tool and backtest job status, including results that finish after steering;
- ordered cursor-based reconnect/replay with duplicate suppression and visible
  message/upload retry states;
- safe workspace switching while background executions continue;
- the workspace CLEO plan and dependencies without requiring CLEO knowledge;
- investigation, strategy lineage, experiment, and artifact navigation;
- experiment-contract cards and change history;
- evidence grades, cutoff overlap notices, exposure history, and limitations;
- strategy diffs, tests, backtest comparisons, and RCA reports;
- global knowledge search with source/version citations and trust controls;
- model role/version/cutoff provenance;
- explicit consent-grant and activation-gatekeeper controls; and
- workspace repository archive, export, restore, and deletion controls.

The standard view shows an event timeline plus periodic synthesized findings.
Diagnostic views may expose prompts, model outputs, tool inputs, logs, and
policy decisions subject to secret redaction. They do not expose hidden
chain-of-thought.

## Conceptual Thesis Components

`src/thesis` should be organized by stable capabilities, not provider SDKs:

| Component | Responsibility |
| --- | --- |
| Workspace service | Create/open local or Azure workspace repositories, enforce isolation, and manage the scaffold. |
| Git coordinator | Branch/worktree ownership, checkpoints, publication, repository budgets, and immutable artifact references. |
| Chat service | Threads, messages, attachments, summaries, and execution launch/steering. |
| Authentication service | Loopback/local operator sessions, Entra provider authentication, workspace authorization, and browser security. |
| Execution service | Durable queue, leases, checkpoints, retries, cancellation, and event stream. |
| Workflow registry | Versioned custom-phase workflows, required artifacts, subagent roles, and gates. |
| Agent runtime | Prompt assembly, model routing, context boundaries, subagents, skills, and budgets. |
| CLEO coordinator | Workspace plan projection and queued single-writer mutations. |
| Tool gateway | Least-privilege lake, harness, sandbox, web, Git, knowledge, protected-test, and activation-request tools. The harness family is implemented as the MCP tool gateway described under Agent Runtime; the remaining families are roadmap work. |
| Experiment service | Contracts, strategy lineages, observed/declared attempt ledger, and a read-only mirror of protected eligibility/exposure. |
| Evidence service | Claims, grades, modifiers, citations, limitations, and report assembly. |
| Knowledge service | Global Markdown corpus, hybrid search, links, citations, and workspace trust overlays. |
| Model registry | Exact model identity, role eligibility, cutoff evidence, and per-session verification. |
| Protected-test client | Immutable submission and policy-approved aliased result retrieval. |
| Consent service | Scoped grant creation, expiration, revocation, and gatekeeper requests. |

Protected-test mapping and transformation should run in a separately authorized
service/process, even if its first implementation is deployed from this
repository. The reasoning runtime must have no route to its mapping store.
The activation gatekeeper is likewise a separately authorized deterministic
service. It is not part of the model tool process and does not expose its
credentials to Thesis.

## Key Records And Contracts

The architecture should define versioned schemas for at least:

- `WorkspaceManifest`
- `WorkspacePolicy`
- `OperatorSession` and `ProviderAuthStatus`
- `ChatThread` and `ChatMessage`
- `Investigation`
- `WorkflowDefinition` and `WorkflowExecution`
- `ExecutionEvent` and `ToolJob`
- `IntentEpoch` and `WorkspaceJournalEntry`
- `ExperimentContract`
- `StrategyLineage` and `StrategyBundle`
- `BacktestReference` and the harness reproducibility manifest
- `AttemptLedger`, `InfluenceLedger`, and the protected exposure mirror
- `EvidenceClaim`, `EvidenceGrade`, and `EvidenceModifier`
- `ModelRegistryRecord` and `ModelInvocationRecord`
- `AliasedResultManifest`
- `KnowledgePageVersion`, `Citation`, and `WorkspaceTrustOverlay`
- `ConsentGrant` and `ActivationDecision`

Every record needs a stable ID, schema version, creation time, source actor,
workspace/investigation links where applicable, and content hash. Model-written
conclusions should reference evidence records rather than embedding orphaned
metric values in prose.

## Security And Policy Invariants

- Default deny all tools, network routes, secrets, protected mappings, and
  activation actions not required by the active role.
- Enforce workspace identity at every API, queue, Git, artifact, model-context,
  and tool boundary.
- Run generated code and uploaded active content in a resource-bounded sandbox
  without implicit network or credentials.
- Treat web, upload, backtest text fields, and ordinary knowledge content as
  untrusted data against prompt injection.
- Make evidence grading, cutoff eligibility, consent, and activation decisions
  deterministic service responsibilities rather than LLM self-attestations.
- Preserve complete strategy lineages, attempt counts, test exposure, tool
  events, and effective policy versions.
- Keep current web access in generation/research contexts only. Protected-test
  and cutoff-constrained RCA contexts receive frozen, policy-approved inputs.
- Never expose alias mappings through prompts, logs, traces, source paths, error
  messages, or correlated tool access.
- Never store credentials, model-provider keys, lake tokens, activation tokens,
  or broker secrets in a workspace Git repository.
- Require structured, scoped consent before paper, shadow, or live activation and
  route execution through a separate deterministic gatekeeper.

## Phased Roadmap

### Phase 0: Contracts And Scaffold

- Add `src/thesis` and `src/ui` product scaffolds and path-focused tooling.
- Define local loopback and Azure runtime profiles, including Entra/Foundry
  authentication, browser-session security, and offline/auth-failure behavior.
- Define workspace, chat, investigation, workflow execution, event, experiment,
  evidence, strategy bundle, and consent schemas.
- Define the public Thesis-to-lake and Thesis-to-harness client boundaries.
- Create the versioned workspace repository template and local workspace
  provider.
- Initialize one CLEO project per workspace and implement the queued
  fenced single-writer coordinator, journal/outbox, and crash reconciliation.
- Define immutable content-addressed strategy bundles and use their identity for
  every backtest and evidence record from the start.
- Define platform bundle signing/versioning and bounded workspace overlays.
- Build isolation, untrusted-upload, secret-redaction, and Git audit tests.

### Phase 1: Thin Vertical Slice

Deliver the shortest complete user outcome:

```text
create workspace
  -> open chat and upload files
  -> request strategy implementation
  -> see a durable workflow timeline and CLEO plan
  -> generate and test Python strategy code
  -> launch quant-harness backtest jobs
  -> inspect results and evidence report
  -> steer work without losing in-flight results
  -> commit the complete workspace record to Git
```

- Implement durable chat, execution events, checkpoints, and reconnect.
- Implement minimum durable execution semantics: fenced leases, idempotent tool
  submission, intent-epoch writes, command deduplication, checkpoint-version
  validation, restart recovery, and explicit failed/dead-letter states.
- Implement the model router with a configurable primary, coder, and reviewer.
- Implement sandbox, workspace Git, CLEO, market-lake, and quant-harness tools.
- Integrate harness strategy validation, manifests, metrics, and result queries.
- Integrate both harness adapters required by the local-first slice: local
  `api`, `backtest-job`, and `trader` processes with filesystem/state adapters,
  and the authenticated remote harness API used by Azure once T102/T112-T114
  are available.
- Expose strategy diffs, jobs, artifacts, experiment cards, and final reports
  through the backend/API contracts. Detailed UI rendering remains deferred
  to the UI design and implementation work.
- Define the API requirements for multiple persistent threads, explicit
  investigation links, ordered event replay, workspace switching, and
  upload/message recovery.
- Support one strategy-development workflow with agent-selected ordinary
  experiment rigor. All Phase 1 historical conclusions are labeled exploratory.
- Use a fake harness adapter only until the published quant-harness contracts
  required by the slice exist; do not couple Thesis to planned internals.

### Phase 2: Research Workflows And Iteration

- Add first-class hypothesis validation and autonomous discovery workflows.
- Add workflow-specific custom phases and phase-required subagent roles.
- Add investigation and strategy-lineage navigation across multiple chats.
- Support concurrent investigations with isolated branches/artifacts and
  serialized canonical/CLEO mutations.
- Add parameter sweeps, attempt ledger, robustness skills, statistical critique,
  and ordinary RCA loops.
- Add evidence claims, grades, modifiers, and claim-to-artifact traceability.
- Add budget enforcement for models, web, data, backtests, time, concurrency,
  and Git growth.
- Consume a minimal version-pinned read-only knowledge API if available; these
  workflows otherwise report global-library retrieval as unavailable rather
  than silently replacing it with current web results.

### Phase 3: Global Knowledge Library

- Implement the global Git-versioned Markdown corpus and hyperlinked page model.
- Implement hybrid lexical, metadata, embedding, and optional reranked search.
- Preserve source/version/effective-date citations in agent outputs.
- Implement separate ingestion, deduplication, link extraction, embedding,
  curation, and Markdown Git publication stages.
- Define the optional workspace-initialization projection into CLEO, including
  its global Markdown revision, rebuild behavior, and whether the selected CLEO
  facility is called Nexus, Brain, or something else.
- Implement workspace trust overlays bounded below platform policy.
- Red-team retrieved content for prompt injection and provenance confusion.
- Defer direct agent publication; support reviewed proposals only in a later
  design.

### Phase 4: Protected Testing And Blinded RCA

- Build the model registry and per-session cutoff eligibility verifier.
- Build the sealed-test authority, exposure ledger, and immutable submission
  protocol.
- Implement identity masking and hardened de-identification as authority-side
  RCA aliasing strategies selected by evidence tier and release gates.
- Build reidentification threat tests; keep decoys out of RCA inputs.
- Add cutoff-aware RCA evidence tiers and apply cutoff-constrained RCA as the
  standard practice for the second-highest RCA tier.
- Route eligible older-cutoff models into RCA-only contexts with no web,
  mapping, prior-result, edit, selection, or activation capabilities.
- Provide the original strategy source and full policy-approved aliased run,
  blocking release when source defeats mapping secrecy.
- Add actor-specific release/egress filters, protected-runner information-flow
  checks, influence and cross-release disclosure ledgers, and measured
  reidentification/diagnostic-utility gates.
- Enforce and display protected-window reuse downgrades.

### Phase 5: Consent-Gated Paper And Shadow Activation

- Add signatures and activation attestations to the immutable strategy bundles
  already used by backtests.
- Implement chat-initiated scoped, revocable, expiring consent grants with
  explicit structured confirmation in chat or the UI.
- Build or integrate the deterministic strategy-activation gatekeeper.
- Enable paper and then shadow activation, both default-off and consent-gated.
- Keep live activation disabled through this phase.
- Confirm that Thesis has no broker/order capability in architecture and
  integration tests.

### Phase 6: Azure And Operational Hardening

- Implement the Azure workspace provider with separate Git-backed entities and
  isolated execution resources.
- Harden queue recovery, checkpoint migration, dead-letter operations, and cost
  controls beyond the minimum durable vertical-slice behavior.
- Test workspace cross-access, prompt injection, secret exfiltration, tool
  confused-deputy, and alias-mapping attacks.
- Add backup, restore, repository integrity, workspace export/import, and
  platform-bundle upgrade procedures.
- Measure Git performance with realistic option and tick artifacts and make the
  chosen all-in-Git limits explicit.
- Enable live activation only after paper/shadow qualification, rollback,
  recovery, backup/restore, security, audit, and operator controls pass their
  explicit gates.
- Add multi-user authorization only as a separately designed milestone.

## Dependencies On Existing FCA Plans

The vertical slice depends on public quant-harness capabilities already planned
in this repository, especially:

- the strategy contract and context facade;
- strategy sandbox validation;
- canonical typed configuration and hashes;
- deterministic backtest runner and reproducibility manifest;
- standard metrics, attribution, result storage, and query/launch APIs; and
- reproducible parameter sweeps.

It also depends on market-lake point-in-time query/client contracts and on
listings-aware, option-contract, and crypto coverage as each asset class is
enabled. Thesis should not mark an asset workflow supported merely because a
model can write plausible code; the corresponding lake and harness capability
must be available and declared.

## Open Design Decisions

The following decisions should be resolved before their owning phase begins:

- Exact primary, coding, research, critique, and RCA models and providers.
- The default anti-overfitting protocol by asset class and horizon.
- Calibration corpus of simulations, null strategies, known-signal benchmarks,
  and frozen historical studies used to validate each protocol version before
  applying it prospectively.
- Whether consuming protected-test budget requires a separate user confirmation
  in addition to the visible experiment contract.
- Hidden-window allocation and reuse defaults within the test authority.
- The target transformations and measured fidelity/privacy threshold for
  hardened de-identification.
- The Azure service used to represent and host each workspace Git entity.
- Local durable queue implementation and Azure queue/workflow implementation,
  provided both satisfy the same journal, checkpoint, and recovery contracts.
- Practical Git repository size, compression, clone, backup, and retention
  limits while preserving the all-in-Git requirement.
- The exact workflow-specific required subagent phases and independence rules.
- Knowledge ingestion sources, curation roles, and later agent-finding proposal
  process.
- Multi-user ownership, collaboration, and approval roles after the local
  single-operator release.

## Success Criteria

Thesis reaches its intended architecture when:

- a local operator can create two workspaces and automated tests prove their
  files, memory, CLEO state, tools, and artifacts cannot cross;
- local Thesis and its UI can reopen workspace history while offline, acquire
  Azure AI Foundry access through backend-only Entra credentials when online,
  and never expose provider tokens to the browser or Git;
- multiple chats and concurrent investigations can safely advance one
  workspace plan without merging SQLite databases or losing Git history;
- an autonomous workflow survives disconnect/restart, accepts immediate
  steering, and retains in-flight tool results;
- a generated Python strategy runs through the same published quant-harness
  contract used by paper and live runtimes;
- every material claim links to a reproducible experiment, harness manifest,
  observed/declared attempt history, influence/protected-exposure history,
  model provenance, and honest evidence grade;
- the global knowledge library returns versioned, cited, trust-aware results to
  every workspace without becoming an instruction-injection channel;
- protected tests keep mappings outside all reasoning contexts, report model
  cutoff overlap accurately, and cannot regain sealed status through relabeling;
- cutoff-constrained RCA cannot generate, select, edit, activate, or browse; it
  receives no direct mapping and each release remains below declared empirical
  reidentification thresholds with residual risk reported; and
- neither paper, shadow, nor live activation can occur without a valid scoped
  consent grant and deterministic gatekeeper approval.
