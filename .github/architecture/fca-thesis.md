# FCA Thesis Architecture

## Runtime and FCA extension boundary

`src/thesis_runtime/` owns the generic host for conversations, tool dispatch,
subagents, workflow execution and durable workers. `src/thesis/application/`
selects providers, persistence and FCA policy. Concrete tools and workflows live
under `src/thesis/extensions/`; research, workspace and harness submission
services remain FCA-owned. Dependencies point from FCA into the runtime.

Workflow extensions cross a versioned JSON endpoint. Recovery requires the
durable workflow version to match the current registration; version tags do not
require retention of historical implementations. Tools receive trusted context
separately from model arguments. Runtime records retain opaque namespaced
references instead of fixed FCA fields. API replicas accept durable work; fenced workers own execution
independently of replica count. The current implementation remains Python with
local FCA extensions; Rust service and MCP tool hosting remain future deployment
work. The package tree, protocols, invariants and executable verification paths
are specified in [Runtime architecture](thesis-agent-workflow-runtime.md#runtime-and-fca-extension-boundary).

Application-owned formats follow
[Current contracts and data cutovers](repository.md#current-contracts-and-data-cutovers):
one current contract, deletion of obsolete development data by default within
the authorized scope, and only a necessary one-time conversion outside runtime
reads/startup/execution. Compatibility paths still in source are cleanup debt;
this documentation update does not claim their removal.

## Status

Unified inline study lifecycle cards and independently published dataset semantics
are implemented. Returned-data mapping policies and preview coverage are shared
by production chat and the study demonstration.

Market-input authoring now uses SDK ABI v5: studies, strategies and authored
indicators implement `MarketConsumer` with `on_quote`, `on_trade`, `on_bar`
and explicit subscriptions. Studies can combine bars, quotes and trades;
tick-only studies need no global interval. Owned indicator dependencies compose
their exact input union before acquisition and update before the parent.
Thesis prompts, schemas, model compiler, `validate_study` feedback and harness
launches preserve the subscription echo. The compiled union must match before
any lake ensure/read. A study requesting quotes and bars fetches those two
types only; the lake does not fetch trades on behalf of bars. Backtests always
add quote/trade execution feeds for every declared input symbol, separately
from consumer subscriptions, because `simple_touch` fills from ticks. Bar frames remain atomic
and available at interval end. Only one bars interval per run is currently
supported; conflicting intervals fail validation before lake acquisition rather
than being mixed. The native built-in indicator registry has not migrated;
the shared interface applies only to SDK-authored consumers and their owned
indicator dependencies.

This document is the architecture and phased roadmap for **FCA Thesis**.
Roadmap provisions record planned work; they do not claim that the described
services are implemented.

The current concrete workflow (version `4.1.0`) lives behind the
`thesis.extensions.workflows.hypothesis_development` public facade: discovery,
model creation, trials, verdict, and the
strategy/backtest/RCA/optimization/null-testing/variation/forward-validation
chain are phases of that workflow. The older standalone strategy-development,
discovery, and RCA workflows are retired, not separate current packages.
`thesis.experiments` implements experiment contracts and research mandates
(time-ordered development/holdout partitions with purge/embargo vocabulary),
dependency-interval audits, attempt/influence ledgers with durable
per-execution attempt records, and evidence grades. The hosted harness MCP gateway,
backend process boundary (`thesis`, `Dockerfile.thesis`), and operations/security
substrate are implemented. Broader roadmap provisions below are not a claim that
all planned orchestration or deployment capabilities exist.

Workspace program authoring is implemented through `thesis.workspaces.WorkspacePrograms`
and context-bound `thesis/extensions/tools/program.py`. Authoritative files live under
`studies/<program_id>/`, `strategies/<program_id>/`, or `indicators/<program_id>/`
as `program.rs`, `manifest.json`, and, after validation, `validation.json`.
Agent-facing list/read/validate/run use latest committed contents, never working-tree
source or caller paths. Updates require the current content version and targeted
exact-match patches. Content versions hash source/manifest digests including
transitive indicators; pinned snapshot loads resolve all dependencies at the same
reachable commit. Harness compilation is authoritative, and runs require accepted
validation matching content and SDK identity. Completion carries artifact references,
not inline Rust; completion-time checks verify stored validation without compiling
or repairing source. The phase agent owns SDK lookup, validation, and repair.

The shared minimal templates in `workspaces/programs.py` back `read_sdk_reference` (including the `decimal` kernel topic)
and `template=basic` authoring; they are SDK contracts, not substantive research
implementations. Wheels and source distributions carry canonical `strategy-sdk`,
`harness/contracts`, `harness/num`, and `lake/contracts` Cargo manifests/Rust sources with portable relative
dependencies. Canonical SDK discovery uses the sibling package layout shared by
source and wheel installations, without searching for a repository checkout.
The wheel and Thesis image builds exercise installed SDK reference/template
reads under isolated Python. Deployment smoke requires the latest API and
worker revisions to run the published digest with ready containers; API probes
must pass, and an old healthy revision never satisfies the rollout check.
The public UI publishes only after this runtime smoke succeeds.
SDK version and ABI are read from packaged metadata; SDK identity
hashes all four crates' manifests and source files using canonical LF bytes on
Windows and Linux; Git attributes and the installed SDK build smoke enforce
that byte contract. Compiler provenance covers
the same complete dependency sources. The shared image verifies the portable
SDK Cargo dependency graph offline before publishing. Remote harness access does not
replace these local reference assets. Shared `prompts/sdk.py` asks for operator
input after three unsuccessful compiler repairs; there is no runtime repair cap.

Root, child, tool-enabled, and model-only execution use `AgentSession` through
`Conversations.run_node_turn`, one `seed_messages` history decoder, and one
`_persist_turn` transcript writer, including runtime-internal tools. The
`SessionReplyProvider`, `reply_for`, and legacy executor paths are removed;
model-only composition uses an empty registry in the same session. Durable
`CompletionMode`, return anchor, and phase-executor authority do not derive from
input kind at runtime. Persistence decoding requires complete explicit durable metadata and rejects
historical inference.
Discriminated runtime activity serves every
anchor. Per-conversation intake positions persist consumed high-water marks in
`ChatThread.input_consumed_positions`; bounded recent-ID memory and paired
durable cursor callbacks permit idle worker retirement without reviving consumed
wakes. Child sends use anchored input admission. Provider adapters normalize concrete
`httpx`/SSL transport errors rather than teaching the session transport heuristics.

Every incoming producer converges on `thesis_runtime/conversations/incoming.py::write_incoming` through
the conversation service or injected inbox port; `_persist_turn` writes outgoing
envelopes only. Live and replay share pure `NativeSessionAdapter` projection.
Before provider execution, the session saves the actual opaque `provider_request`
and its hash with ordered reference-only `request_messages` (`message_id`,
`direction`, `position`) in a `STARTED` invocation. Canonical message bodies remain
in the neutral transcript rather than a full duplicate audit. Cosmos forces the
opaque request into immutable content-addressed chunks before its invocation
manifest; reads verify and hydrate the complete JSON audit. Terminal updates
preserve it, and persistence failure prevents provider execution. Historical full
message audit formats are rejected under the current-contract policy. See runtime `REQ-DEL` for executable enforcement.

Workflow session policies and completion services are FCA ports in
`thesis/extensions/workflows/bindings.py`, implemented by the hypothesis
extension, not HTTP serving or the generic host. Spec-only
services preserve schemas and retrieval restrictions without a harness; without
an execution runtime they cannot accept phase completion. One phase declaration
derives topology, policy, and piping. Live acceptance and replay share the pure
`fold_transition` reducer behind the JSON extension endpoint, applying each pipe once.
Acceptance rechecks intent epoch, executor and attempt, commits with store revision
CAS, projects the phase, and closes the executor under the same guard used by
steering, cancellation, and job attachment. Events and successor entry are outside
that guard. Deterministic executor failures fail the workflow, never become
agent-recoverable tool errors.

Cosmos keeps one authoritative conversation store in the workspace-partitioned
catalog: records, messages, events, and per-chat sequence counters share the
container, and a message or event create commits the counter CAS and the item
in one transactional batch. Messages and immutable first-save execution
snapshots are the same documents the timeline reads, so there is no projection
outbox, repair scan, or second container. Timeline readers wake on an
in-process post-commit notification or the API change-feed watcher and resume from their durable sequence
cursor. Stack bootstrap creates the database and catalog container
if absent; `open_cosmos_chat_stack` and `open_execution_store` are not
side-effect-free, and chunk storage uses the catalog container.
The local launcher pins Cosmos emulator `vnext-EN20260907`, retains its named
`/data` volume across container replacement, and binds gateway, health, and
explorer ports to loopback. Local clients default to `http://127.0.0.1:8081` to
bypass that image's TLS request-size stalls. `EN20261008` fixes TLS on fresh
databases but fails to query this retained catalog, so it is not the local
deployment target. Configured cloud Cosmos endpoints continue using HTTPS.
The UI's `TimelineController` is the single owner of chat history/live sequence
and thread indexes; scoped navigation has no duplicate transcript cache or
execution SSE subscription. See `thesis-agent-workflow-runtime.md` and
`thesis-ui-design.md` for these implemented boundaries.

`/v1` and `/api/v1` use the same FastAPI application state, services, handlers,
and authentication. Defaults are fresh per factory and composed once; request
dependencies never create replacement services. Production explicitly disables
in-memory defaults and missing services return 503. Stream brokers are process-local hints; production scheduling and mutation
ownership use durable fenced worker claims across replicas.

`thesis.http.schema` exports OpenAPI 3.1 plus source-derived runtime tree,
message, activity, datum, and tool-result schemas. The UI generates TypeScript
and JSON Schema from that export; endpoint decoders and one AJV registry validate
HTTP, SSE, and fixture timeline records before presentation adaptation. Complete
node-input receipts, steering acknowledgements, and structured errors are part
of that contract. Full chat and copilot share host/timeline/navigation controllers;
fixtures use the same record shape, not another agent runtime.

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
- Supporting organizations or public multi-tenancy. Invite-only named users are
  approved for the public release (`docs/adr/adr-001-invite-only-public-multi-user-access-for-thesis.md`);
  open signup and organization/multi-tenant collaboration remain non-goals.
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

A harness backtest never fails merely because a requested symbol was not
ingested before: the harness asks the published market-lake ensure contract to
fetch the run window's configured symbols, then polls coverage until it is
satisfied before reading the window over the lake's `GET /v1/read` and
reference routes through the `harness-lakeclient` crate; the harness never
reads lake files or links lake storage. One ensure request covers the
whole window — the lake plans, resumes, and retries its own fetches — and the
lake pages every fetch until the provider cursor is exhausted. A closed
session left `partial`, or an errored fetch, is a repairable gap the lake
re-fetches inside the running job, so the harness keeps polling while that job
is in flight and fails only once it settles, naming the sessions the lake
could not complete. The lake owns provider credentials and the fetch; the
harness only issues the ensure request.

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
| Workflow execution | One end-to-end autonomous invocation launched from chat using a versioned workflow definition. It is steered conversationally: a phase agent asks the operator when it needs direction. |
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
mode, each workspace is a separate Git repository hosted by an open-source
Gitea instance that Thesis runs inside the customer's own Azure subscription —
not Gitea Ltd's paid hosting — with isolated execution and persistence
resources. Thesis provisions each workspace repository through the Gitea API
and records the repository mapping on the workspace itself. Gitea serves
repository data from a POSIX filesystem (an Azure Files share or an attached
managed disk), so repository growth remains subject to the workspace size
budgets; Git LFS objects and attachments may use the forge's object-storage
backends. Gitea's MIT license keeps the forge embeddable in a commercial
product. A shared branch in one repository is not an acceptable substitute for
a workspace boundary.

The implemented repository adapter is
`thesis.workspaces.gitea.GiteaWorkspaceProvider`, behind the same structural
`WorkspaceRepositoryProvider` boundary as native local Git. Remote manifests
are read from verified checkouts; Blob copies and secret-free repository
locators are catalog metadata. Creating a workspace seeds and pushes its
initial scaffold before returning success. Later publication pushes commits
created by the accepted publication path; it does not commit arbitrary dirty
files. Checkout conflicts fail explicitly without resetting agent work.

Azure hosting is opt-in through `enableGitea` and an official digest-pinned
image. The initial host is one internal-ingress Container App and an isolated
Azure Files share for repositories, with a Burstable PostgreSQL database over
verified TLS and one forge replica. SQLite on SMB failed initial write-locking
and is removed. PostgreSQL retains seven days of backups; repository backups
must be coordinated with database recovery. Admin,
organization, and token bootstrap remain operator-managed. Existing artifact
writers are not all connected to remote commit/publication yet; the provider
does not imply that every workflow artifact is already durable in Gitea.

### Workspace Repository

Workspace deletion is a durable, workspace-wide lifecycle operation.
`DELETE /v1/workspaces/{workspace_id}` (also under `/api`) returns `202` with
`status: deletion_pending`. The catalog first persists a retained marker covering
**all** data owned by the workspace ID: Git files and history, remote repositories
and checkout caches, Blob objects, Cosmos records/chunks/timeline/queue state,
attachments, application state, activity, and workspace rows in shared stores.
The global knowledge library and other workspaces are outside that scope.
Local markers live at `.catalog/deletions/<sha256(workspace_id)>.json`; hosted
markers live at `catalog/deletion.json` in the workspace container. Markers retain
the original summary/owner so retries remain authorized without reopening Git.
Catalog reads hide marked workspaces and creation never reuses a marked ID.

The HTTP composition rejects all other routes for a marked workspace and injects
the same durable lifecycle read into `ChatService`. All roots, including archived
chats, enter existing conversation deletion and bounded asynchronous cleanup;
their nested conversations share the root fence. Startup resumes this operation
from catalog markers after interruption. Repeated DELETE requests are idempotent.
Conversation cleanup runs now; physical erasure of repository, Blob and other
non-conversation storage is deferred. No automatic collector for those resources
is implemented yet. A future collector must remove the entire marked storage
scope and retain the deletion fence rather than merely deleting the catalog row.

All durable workspace domain content is stored in its Git repository, including
uploaded files and generated backtest artifacts. Credentials and secret values
are the only categorical exception: Git stores references to a secret store,
never secrets.

A new workspace should start from a versioned scaffold similar to:

```text
<workspace>/
|-- .thesis/
|   |-- workspace.yaml             Identity, charter, limits, and settings
|   |-- platform.lock              Effective Thesis bundle and model policy
|   |-- events/                    Durable user-visible execution event logs
|   `-- journal/                   Accepted commands and publication outbox
|-- AGENTS.md                      Workspace-local bounded instructions
|-- chats/                         Thread records and compact summaries
|-- investigations/               Objectives, decisions, and finding indexes
|-- strategies/                      Rust strategy code and tests
|-- experiments/                   Contracts, analysis, and evidence claims
|-- jobs/                          Imported job manifests, metrics, traces, and reports
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

The workspace coordinator serializes canonical mutations and Git publication
under a fenced single-writer lease. Each accepted mutation has an idempotency ID
and journaled outbox record. Publication verifies the committed artifacts before
marking the outbox item complete. Startup reconciliation safely completes an
interrupted publication without replaying the logical mutation twice.

The Thesis execution service owns in-flight model and tool execution,
checkpoints, leases, retries, cancellation, and recovery. Each model invocation
or subagent context can remain fresh and least-privileged.

## Agent Runtime

### Durable Chat And Autonomous Work

A chat message can answer synchronously or launch a workflow execution. An
execution persists independently of the browser connection and reports through
a durable event stream. At minimum, the backend stores:

- the triggering user message and attachments;
- selected workflow and effective workflow version;
- current custom phase and phase history;
- investigation links;
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

### Conversation Anchoring And Datum Rendering

Every conversation has an anchor: the root chat for the top-level agent, and a
sub-agent node for anything nested. A sub-agent is the same conversation
abstraction as the top-level agent — same chat surface, same transcript semantics,
same tool-result projection, same streaming, same operator input — parameterized by
anchor. The only permitted differences are the anchor, the tool policy, and the
lifecycle that creates and terminates the agent.

Tools return typed datums; a single call may return several datums of different
types asynchronously. The surface selects each datum's rendering from its declared
type, with the generic tool card as the fallback, and renders it at the caller's
anchor. Tools never carry rendering instructions and no tool is special-cased by
name.

Workflow executions are owned by the anchor that launched them, and each execution
has a durable workflow-node record parented to that anchor which owns its phase
agents. Node input is governed by a per-node operator policy whose default is
human-only, so future autonomous orchestration is a policy change rather than a
structural one. The normative requirements are REQ-018 through REQ-040 in
`thesis-agent-workflow-runtime.md`, with the accepted design record at
`thesis-chat-surface-and-subagent-anchoring-v1`.

### Chat And Execution Boundary

Authenticated `GET /v1/workflows/{workflow_id}` (also under `/api/v1`) exposes
the registered workflow definition for transition-graph clients. The response
contains `workflow_id`, `version`, `title`, ordered `phases`, `terminal_phases`,
and `transitions` mapping each phase to sorted legal target names, including
empty lists for terminal nodes. It reads the launcher's registry in serving
compositions; chat-only apps expose the built-in definitions without execution
services. Unknown ids return HTTP 404 with error code `unknown_workflow`.
The endpoint never launches a workflow or serializes executable callbacks.

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

Every model continuation, tool result, workspace mutation, Git publication, and
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

Incremental model output uses the runtime-owned invocation lifecycle and durable
`output` timeline records. Provider adapters normalize reasoning and answer
fragments; partial output never enters the session inbox or authorizes dispatch.
The final canonical message closes the same output identity before success and
input acknowledgement. API replicas observe worker commits through the composed
timeline notification port and independent Cosmos change-feed watchers. See
`thesis-agent-workflow-runtime.md` for lifecycle and recovery details.

The implemented chat UI streams `GET
/v1/workspaces/{ws}/chats/{chat}/timeline/stream`, one durable sequence of
messages and events. SSE `id` is `seq`, resumed with `Last-Event-ID`; backward
history reads the same timeline with `thread` and `before` until exhaustion.
The existing execution event endpoint remains a backend surface, not a second
UI subscription. Provider deltas are ephemeral, not durable timeline history.
Each tool result is a durable anchored `tool` message carrying call identity,
success/failure, structured output, and typed datum metadata. Root and child
transcripts use the shared projection, including multiple datums from one call.
Scope state controls navigation and capabilities only. Re-entry reconciles
attempt identities and snapshot/event freshness rather than keeping a prior
terminal node permanently closed. Graph edges come only from a fetched current
workflow definition matching the execution version; missing/mismatched topology
is explicitly unavailable, not synthesized.

### Model Router

Thesis is not coupled to Fable 5.1 or any other single model. A policy-driven
model router assigns models to roles such as:

- primary chat and workflow orchestration;
- current or historical web research;
- Rust strategy implementation;
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
| Market lake | Ensure and query point-in-time equity, option, and crypto data through published contracts. |
| Quant harness | Validate program code, launch deterministic backtests, studies, and screens, and query job status and result artifacts. |
| Code sandbox | Run generated Rust programs and their tests with bounded CPU, memory, filesystem, and no implicit network or secrets. |
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
study, screening, job-query, artifact, and supervised-trader operations through
MCP; Thesis consumes them through `thesis.integrations.mcp` and
`thesis.agent.harness_tools`.
The client discovers the catalog with `tools/list` and executes it with
`tools/call`, so a launch can only route through a tool the harness actually
publishes.

The gateway uses the harness-hosted MCP endpoint for local and remote serving:

| Thesis | Harness | Transport |
| --- | --- | --- |
| local | local | HTTP JSON-RPC to the local harness's `POST /mcp`. |
| remote | local | HTTP JSON-RPC to a reachable hosted harness endpoint; no Thesis-spawned process. |
| local | remote | `RemoteHttpMcpTransport` posting JSON-RPC to `POST /mcp` with a bearer credential. |
| remote | remote | `RemoteHttpMcpTransport` posting JSON-RPC to `POST /mcp` with a bearer credential. |

`HARNESS_MCP_URL` selects the hosted endpoint; `THESIS_LOCATION` names where Thesis runs and
`HARNESS_API_BEARER_TOKEN` supplies the remote credential. The bearer token is
sent only as an `Authorization` header and is excluded from every
representation. `thesis.agent.harness_tools` wraps the client in a
`HarnessToolProvider` that adds typed arguments, structured results, and
agent-visible failures, and the `harness-mcp` skill is registered into the
`SkillRegistry` used to build the model router so role policies resolve the
bundle and permit `HARNESS_TOOL_NAMES`. The unified session registry enforces
the resolved `allowedTools` before a model tool request reaches the provider;
there is no second policy-enforcing dispatcher.

Backtest launches route through the MCP `run_backtest` tool; capabilities derive
from the discovered catalog. The local harness HTTP `/mcp` endpoint composes
`NativeBacktestSubmitter` over the same authoritative job store as REST. Thesis
does not spawn a harness process or fall back to local execution when MCP is
unavailable. Workflow execution is composed only with the harness tool provider;
spec-only session policy and knowledge tools still work without it.

`McpBacktestRunner` and `McpStudyRunner` send configuration mappings inline;
explicit path strings name a file on the harness host. A confirmed domain
not-found response permits a new launch; transport/auth/protocol failures and
unknown states do not count as absence. Pending jobs are joined and polled, and
a terminal job's stored result is returned under its submitted `job_id` rather
than relaunched; an explicit rerun allocates a fresh job. Submission
composition allocates the id and durably retains the recoverable submission
reference with immutable program attribution before dispatch; a transport
interruption after possible dispatch is an unknown outcome reconciled by that
retained id, never an automatic replay. Polling uses a monotonic deadline,
bounded backoff, and cancellation checks. Deterministic run failures fail the
owning workflow rather than sending it into an agent recovery loop. The
"Execution Job Contract" in `.github/instructions/copilot-instructions.md` is
the normative policy for submission identity, admission, acknowledgement, and
recovery.

The harness owns study validation reuse. Its local `StudyValidator` uses the
JIT service's bounded 64-entry full-input cache and compiler identity, with
source/configuration invalidation affecting the actual artifact. MCP accepts an
injected validator's optional opaque identity and forwards `reused`, `cacheable`,
and `deterministicCompilerDiagnostic` metadata from Cargo JSON diagnostics. MCP
does not inspect compiler files or probe tools; Python keeps no validation LRU,
identity probes, or regex diagnostic inference. Infrastructure errors are never
cached as deterministic compiler errors. See `rust-native-authoring-plan.md`.

The local harness submitter is asynchronous like the remote one: `run_backtest`
returns an accepted `job_id` and state immediately and completes on a worker,
so the caller follows `get_job_status` to a terminal state, then `get_job_result`
and `list_artifacts`. A job's result set (`backtest.json`, `metrics.json`,
`report.html`, and the recorded Parquet trail such as `artifacts/equity.parquet`)
is registered as one authoritative job record (`jobs/<job_id>.json`) with
artifact payloads under the `jobs/<job_id>/` object prefix, and `read_artifact`
returns any artifact's bytes (UTF-8 or hex) through the same boundary in both
local and remote modes. The public job vocabulary is `queued`, `running`,
`succeeded`, `failed`, and `cancelled`; engine-internal completion states are
translated once per adapter and never surface as public aliases. A failed or
cancelled job publishes its terminal record with the truthful message in
`error_message` and may carry no artifacts, and no synthetic failure artifact
is fabricated. Thesis's workspace-scoped tracking records reuse the same
`job_id` while remaining attribution, submission certainty, and late-result
observation, not a second execution-state authority; non-harness tool jobs are
explicitly classified and are never presented as harness executions. The
result digest is domain-separated over a payload keyed by
`job_id` (`fca:harness-results:job:v1` / `harness.results.job.v1`); screening's
run digest uses its own job-based domain (`fca:harness-screen-job:v1`).
`metrics.json` carries a canonical `trades` block — trade count, hit rate,
net, gross and fee totals, per-slice `rows` with exact nanosecond timestamps, and
a profit factor that is undefined (with a reason), never a gross-profit
substitute, when there are no net losses — and an `equity` block whose Sharpe is
published with its own reason. Import and candidate selection read this one
authority instead of recomputing statistics or rebuilding trades from fills.
Artifact bytes are read through the harness boundary when available and
otherwise via the artifact's published SAS/object URL, so one importer serves
local and Azure jobs. A materialization
route (`GET /v1/workspaces/{ws}/backtests/jobs/{job_id}`) lets the UI fetch the
card data once the tool returns, so neither side blocks on a long poll;
`GET /v1/workspaces/{ws}/backtests/{job_id}` serves the imported detail.
`thesis.backtests` coordinates this lifecycle: on a terminal success it imports
the job into the workspace backtest store as a `BacktestRecord` keyed by
`(workspace_id, job_id)` carrying the equity curve, the canonical closed trades
(`closed_trades.v1` rows as the harness published them), and the published stats.
A statistic the harness did not publish is stored as `null` with a reason; the
importer never recomputes a statistic, rebuilds trades from fills, or zero-fills.
The record exposes `capital_change` (final minus initial capital) in place of a
net-PnL figure, because fees and trade PnL are reported by the trade rows. The
record is linked to the originating `chat_id` and `execution_id`. The chat tool
loop attaches the `job_id` to the tool message so the UI renders the report
inline where the job ran, and the backtests page surfaces the job with a link
back to its chat and workflow.

## Research Workflows

Workflow definitions are versioned, have custom phases, specify required
artifacts, and can declare phase-required subagents. They are enforced by the
orchestrator and evidence services, not only described in a system prompt.

### Hypothesis Research Rigor

The older standalone `hypothesis-validation` workflow and its HTTP/UI surface
have been retired; the falsification-first **Falsification-First Hypothesis
Development** pipeline below is the canonical research workflow. The rigor
requirements once enforced by the validation workflow are preserved there and
by the experiment-contract substrate. A research run starts from a
user-supplied claim, including claims that do not yet define a tradable
strategy, and can:

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

### Falsification-First Hypothesis Development

This workflow captures the falsification-first research pipeline: a user
brings an intuition about a trading opportunity, and the agent turns it into a
*statistical model* of the assets involved, asks whether a regularity matching
the intuition exists in historical data, makes a real effort to destroy it
before designing a strategy, and only then moves into strategy design and
optimization. The model is general: it may describe the relationship between
several assets (co-movement, cointegration, persistent relative
underperformance), the structure of one asset's price action (trendiness,
periodicity), or the pricing of options and volatility in a given regime.

The model serves two falsification layers:

- **Layer A** runs inside this pipeline, on history. The hypothesis is refuted
  when no such model can be built, when the fitted model shows no regularity
  that beats a no-structure null (independence, random walk, fairly priced
  volatility), or when a regularity exists but does not match the intuition
  (wrong direction, far too weak). A refuted hypothesis is adjusted and tried
  again.
- Layer A thresholds account for market noise and test whether a stable
  regularity survives a no-structure null and negative controls. Modest effects
  with the predicted direction count as successful research when they survive
  those checks; aspirational strategy metrics such as a high Sharpe, hit rate,
  R-squared, or conventional significance cutoff are not themselves grounds for
  refutation. Borderline evidence is inconclusive rather than refuted.
- **Layer B** runs while a position is open. The strategy applies the same
  model's frozen Layer A fit to a trailing window and exits when observations
  leave the calibrated range: the idea behind the entry has been refuted. It
  does not re-estimate the model or replacement parameters. A strategy
  therefore separates its trading exits from assumption monitors that re-check
  the entry's premises: each monitor names a trailing window, minimum sample,
  and breach action, while aggregate or offline statistics remain monitoring
  duties rather than per-trade exits. PnL is only a guardrail.

The discovery agent runs targeted studies on the starting intuition, records
every attempt including negative results, and hands model creation one
candidate thesis: signal, information timing, direction, holding period,
profit source, expected effect, main competing explanation, entry and exit
ideas, necessary assumptions, named building blocks, validation questions, and
exploratory evidence. Composable representations — event-time methods,
path-shape extraction, duration statistics, and learned representations —
are legitimate exploration directions; simple statistical families are
baselines, not the search-space limit. When the attempts show nothing worth
pursuing, discovery concludes with an explicit no-candidate reason instead of
manufacturing an opportunity, and the run ends `completed`. The
model-creation agent proposes the model itself -- assets, estimator,
sampling frequency, windows, sample period, expected estimates versus the
null, falsifiers, and negative controls -- and infers rather than interrogates.
It never proposes a forward-looking experiment, and it does not ask about
sizing, costs, borrow, financing, or execution, which belong to strategy
design.

Indicators and strategies are separate contracts. The immutable global
indicator registry contains platform implementations such as `sma`, `rsi`,
and `realized_vol`. Authored program dependencies live under
`indicators/<program_id>/` with committed source/manifests.
The accepted study program lives under
`studies/<program_id>/`; its completion reference pins content and Git commit.
Research execution also publishes the resolved statistical model independently under
`models/<model-id>/<content-sha>/` as an authored Rust program whose source
implements `MarketConsumer<Context = ObservationContext>` plus the SDK
`StudyProgram` lifecycle (`new(parameters)`, `on_stop(&mut MetricPublisher)`).
Its `subscriptions()` declares exact bars (`1min` or `1d`), quote, and trade
inputs; `indicators() -> Vec<&mut dyn Indicator>` exposes owned nested
dependencies. Requirements and topology remain stable after construction;
cycles and zero-sized indicator dependencies are rejected. Dependencies update
before their parent, with consumer-local routing. Common callbacks are
`on_quote`, `on_trade`, and `on_bar(&BarFrame, &mut ObservationContext)`;
there is no study raw-bar callback. It publishes Layer A observables as metrics,
and `fits/<fit-sha>.json`
contains the immutable numeric Layer A parameters and calibration bounds. The
fitted reference is workspace-local and is the only model state a Layer B
strategy may consume; the model is never represented as a strategy.

The workflow phases use no operator workflow gates, gate-resolution schemas, or
gate tools. Phase agents ask through `request_workflow_input` at their own anchor
and request completion after operator acceptance. The engine does not author or
relay operator questions.

Every request for operator input or direction is model-authored from a structured reason, never a canned workflow string; only mechanical text (phase names, counts, digests, ledger statements, activity labels) is deterministic. See `thesis-operator-text.md` for the rule and the current inventory.

1. `discovery` -- a durable conversation in which the research agent runs
    targeted studies on the launching premise, logs every attempt, and returns
    one candidate thesis with its entry/exit ideas, necessary assumptions,
    building blocks, validation questions, competing explanation, the data it
    left untouched, and its limitations -- or an honest no-candidate conclusion
    when nothing is worth pursuing (completed). Discovery is deliberately
    weaker than validation: it never runs the final model, and its trusted tool
    policy excludes the backtest execution tools (`run_backtest`,
    `backtest_config_template`) so it can only run studies.
2. `model_creation` -- a durable conversation in which the agent turns the
    accepted candidate into the statistical model, states the Layer A
     falsifiers, and authors the Rust study. It asks for review through
     `request_workflow_input`; confirmation is a conversational instruction,
     not an engine-enforced approval gate. After acceptance it submits the
     structured hypothesis and plan through `request_phase_completion`, with a
     committed study reference plus description, outputs, and tests. The agent
     validates the latest study ID before completion. Contract/evidence errors
     and explicit compilation-tool failures return to that agent for repair;
     completion itself checks stored validation without recompiling.
     Prose alone never advances the phase. The transcript records concise
    finalization summaries. The hypothesis is projected into a workspace
     immutable artifact under `hypotheses/<slug>/<sha256>.md`.
   **Gate (falsifiability):** the front matter must carry a non-empty
   disagreement (what the no-structure null predicts instead) and at least one
   falsifier with `{observable, condition, threshold, test}`. No departure
   from the null or no falsifier cannot advance; without a regularity there is
   nothing to trade. **Gate (model):** the separate research model must declare
   every falsifier observable as a finite named output and declare its
   `harnessConfiguration` window (`start`/`end`) and exact `subscriptions`,
   including owned indicator requirements. Only bars declare an interval
   (`1min` or `1d`); tick-only inputs need no global interval. **Gate (plan):**
   at least one falsifier test bound to a declared
   falsifier and at least one negative/placebo control.
3. `falsification_trials` -- the planned trials run through the harness and
   bind every falsifier to a metric from one historical model execution. The
   model's authored Rust study program runs once as a `study`-shell replay over
   the exact inputs it declares in `harnessConfiguration.subscriptions`
   (preserved as `data.subscriptions` in the study configuration). The compiled
   ABI v4 JSON union of source and dependencies must canonically equal that
   echo before any ensure/read; `validate_study` returns the union for author
   feedback. Interval-only configuration is rejected, and authored sources use
   the current SDK API. The study observes
   subscribed ticks and `BarFrame`s, and publishes Layer A observables in
   `on_stop` through
   `MetricPublisher::publish`; the `fit.<parameter>` metrics are frozen as the
   content-addressed fitted-model artifact. Model source has no order, position,
   sizing, portfolio, or venue capability. Study acquisition fetches only the
   effective study union; backtests additionally fetch quotes and trades for
   execution on every input symbol. The manifest distinguishes effective inputs,
   execution feeds, and their canonical acquisition source union. Mixed
   replay orders by availability, with ticks, completed frames, then generated
   execution events at equal time. Bars are available at interval end, not bar
   start. Thesis writes a workspace evidence
   record under `models/runs/` containing the model ref, `jobId`, state,
    metrics, and result artifacts. This is a deterministic phase in topology
    `3.0.0`: `run_falsification_trials` resolves the recorded result and returns
    the trials contract without creating a phase agent or model turn. The report
    projection uses observed metrics and declared thresholds;
    distribution and time-series charts render only when the evidence artifact
    contains real samples, never client-generated placeholder data. Missing
     evidence is an execution/data problem, not permission to rewrite the premise.
     Agent tool failures can be repaired within the turn; deterministic executor
     failures instead fail the workflow durably.
4. `verdict` -- a durable conversation in which the agent interprets the
    accepted research evidence envelope (per-role assumptions, controls,
    trials, run states, and plan) and proposes `supported`, `refuted`, or
    `inconclusive` with cited evidence, distinguishing a measured falsifier
    breach from missing evidence or execution failure. A supported verdict
    advances to strategy design; a refuted verdict reopens the persistent
    `model_creation` conversation for a revised model; when the premise itself
    is not worth pursuing the agent loops back to `discovery` with the
    superseding verdict. The engine validates the typed verdict contract and
    the declared target only; operator review stays conversational through
    `request_workflow_input`.
5. `strategy_design` -- a conversational phase. The agent consumes the
    discovery thesis, the research evidence envelope, and the verdict; the
    agent designs the strategy — trading exits plus assumption monitors that
    name a trailing window, minimum sample, and breach action — explains it to
    the operator in prose, and authors it
    through the `author_strategy` tool as pure Rust source against
    `harness-strategy-sdk` (the harness JIT-compiles it). The strategy
   references the surviving workspace model and fitted-model hashes instead of
   copying their definitions, implements `MarketConsumer<Context = OrderSink>`
   for exact subscriptions and common quote/trade/frame callbacks, and keeps
   `new`, `on_start`, `on_fill`, and `on_stop` in `StrategyProgram`. It feeds
   trailing events to the model monitors and
   applies each monitor's declared breach action (warn, disable entries,
   de-risk, or flatten) as authored. The strategy declares every meaningful
   tunable in the committed manifest `parameter_declaration` (name, type,
   default, bounds or levels; `source` and `symbols` stay reserved), which
   later phases use to compose validated parameter assignments. The current
   authoring path commits `strategies/<program_id>/program.rs` and `manifest.json`,
   then writes the accepted `validation.json` receipt through `validate_strategy`.
   The older `WorkspaceStrategyArtifacts` service and Strategies HTTP projection
   remain separate domain surfaces, not this program-bound completion path.
     **Completion validation:** submit `workspaceId`, `kind=strategy`, `programId`,
     `gitCommit`, and content-hash `version` from successful validation. Acceptance
     verifies pinned/latest committed content, stored validation, and SDK identity;
     it does not compile, execute tests, rewrite source, or provide a security sandbox.
6. `strategy_backtesting` -- a `OneShotPhase` agent, not a deterministic executor.
   It reads the handed-off `strategy.programId` with `read_strategy`, calls
   `run_backtest {program_id: ...}` using latest committed content and its current
   accepted validation, optionally with a `parameters` assignment validated
   against the committed declaration before submission, records the actual run
   receipt, and requests completion.
   Stale or rejected validation is reported as a blocker, not bypassed with inline
   source or caller paths.
   A successful terminal run advances to optimization. A failed run advances to
   `strategy_rca`; no economic threshold is invented unless a future strategy
   contract explicitly declares one.
7. `strategy_rca` -- the critique agent diagnoses the failed strategy run and
   posts concrete recommendations into the persistent `strategy_design`
   conversation. The design agent revises and re-enters strategy backtesting.
   Optimization failures use the same back-edge. This strategy loop is bounded
   to five designed-and-tested attempts.
8. `optimization` -- a `OneShotPhase` agent that evaluates a bounded parameter
   search for the designed strategy: it composes explicit parameter assignments
   from the committed declaration, submits them in one `run_backtest_batch`
   call, follows each run to a terminal state, records honest states and
   observed metrics, and completes with a typed candidate evaluation carrying
   the batch identity, per-run evidence, a selection rule, and a shortlist
   chosen on net return, profit factor, drawdown, trade count, turnover and
   diversity — not top return alone. Unknown-outcome runs are recorded, never
   relaunched automatically.
9. `null_testing` -- a `OneShotPhase` agent that tests the selected candidate
   against signal-destruction nulls: it fixes the observed run and statistic,
   freezes sufficient unique seeds for the declared resolution and submits them in bounded chunks through `run_null_batch` (the
   harness permutes individual or block per-symbol bar returns and rescales the feed), polls
   every run to a terminal state, records honest states and metrics, and
   completes with a typed evaluation carrying the observed value, replicate
   values, exceedance count, and the empirical `(1+k)/(1+n)` p-value. Unknown
   runs are recorded, never relaunched automatically.
10. `variation` -- a `OneShotPhase` agent that runs a deterministic
    one-at-a-time parameter neighborhood around the selected candidate within
    its declared bounds (one differing
    parameter per point, or declared joint experiments) through `run_backtest_batch`, records honest points
    and metrics, and completes with a typed variation evaluation plus a
    stability assessment. The UI renders per-parameter value-versus-total-
    return/profit-factor charts from the phase report artifact.
11. `forward_validation` -- a `OneShotPhase` agent that repeats the frozen
    observed run, the null design, and the variation points on the protected
    forward partition. The phase receives a trusted forward-window policy from
    the research mandate; the run boundary rewrites the committed window to
    the holdout bounds (agent tools cannot supply windows), each holdout job is
    recorded in the durable exposure ledger at submission, before dispatch, and the
    completion may report only jobs that provably ran the frozen design on the
    forward window. Development and forward evidence are then compared without
    retuning.
12. `completed`, plus `paused` (a failed gate, exhausted loop budget, or steering pauses with a
    recorded reason) and `failed`.

Each repeated phase uses one durable top-level phase node. Re-entry appends new
activity, handoff, tool, and model events to that node's existing chronological
conversation. Attempt boundaries and cycle counts remain explicit in durable
handoff messages and phase-transition events without fragmenting the operator's
phase conversation across multiple nodes.

The falsification planner and the strategy author are prompted with the
finalized model, not only the claim: the claim, the null/disagreement, the
horizon and universe, every declared falsifier with its id, observable,
condition, threshold and computation, the workspace model reference, and the
Markdown model specification.

`WorkspaceHypothesisStore` resolves every path through the selected local or
Gitea repository provider and requires a workspace ID for save/load/list. New
versions publish canonical bytes at `hypotheses/<slug>/<sha256>.md`, selected by
`.thesis/hypotheses/<slug>.json`. A workspace command journal and fenced Git
publication preserve immutable accepted content and exclude unrelated index or
worktree edits; reads verify identity, digest, canonical bytes, and committed
content. Loads require the publication manifest; legacy `hypotheses/<slug>.md`
has no compatibility read. There is no shared-root fallback or guessed legacy
ownership migration.

The hypothesis artifact is a workspace Markdown document whose small,
strictly validated YAML front matter (rendered in the deterministic JSON
subset of YAML 1.2) carries `schema_version`, `hypothesis_id`, `status`,
`claim`, `disagreement`, `risk_factor`, `horizon`, `universe`, `falsifiers[]`,
`sizing`, and `links`; the Markdown body carries the full model specification
(variables and transformations, estimator, expected estimates versus the null,
negative controls, the Layer B monitoring rule, and limitations). The artifact
is a projection: the agent owns the prose fields, while the workflow and the
hash-chained experiment ledger own `status` and `links`, and
`links.runs`/`links.evidence` are populated from executed backtests rather
than hand-edited. The workflow emits a durable `workflow.phase` execution
event for every transition so the UI execution timeline renders the pipeline.

### Strategy Development And Improvement

This workflow starts from a hypothesis, strategy outline, or existing Rust
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

## Shared And Workspace Knowledge

The shared library lives under `src/thesis/knowledge/library/` and ships as
Thesis package data. Every workspace also owns a separate `knowledge/` corpus
inside its verified repository checkout. Both hold versioned research evidence;
workspace content never crosses another workspace boundary.

Thesis model sessions expose `search_knowledge` and `read_knowledge`, including
the discovery, model-creation, verdict, and strategy-design phase agents. Search
queries both sources by default; the model can select `shared`, `workspace`, or
`both`. `WorkflowSpec.knowledge_scope` restricts workflow retrieval, including
helpers carrying workflow attribution. Serving configures this through
`THESIS_KNOWLEDGE_SCOPE` (default `both`). The requested scope intersects the
trusted scope; a disjoint request fails rather than falling back to both.
`WorkflowSpec.phase_knowledge` additionally restricts individual phases to
named collections: discovery may retrieve only `discovery`, `data-quality`,
`trading-costs`, and `model-cards`; model creation only `model-cards`,
`inference`, `validation`, and `trading-costs`; every other phase is
unrestricted. The allowlist filters
the searched page set before lexical, semantic, or hyperlink expansion, and is
re-checked on read, so an excluded page cannot be reached by any retrieval
path. Workspace identity comes from runtime context, not tool arguments.
Knowledge tools also work when the harness is unavailable.

Search builds separate in-memory hybrid indexes over actual corpus bytes and
returns bounded excerpts with source-qualified references. Reads resolve those
references against the same byte snapshot and reject changed content. Results
preserve page versions, provenance and typed relationship annotations; their
`snapshot_id` is a SHA-256 content identity, not a claimed Git commit. Serving
embeds with `Xenova/all-MiniLM-L6-v2` (384-dimension sentence encoder)
through onnxruntime, downloaded and cached on first
use; the hashed bag-of-words baseline remains an automatic offline fallback and
`THESIS_EMBEDDING_DISABLE=1` forces it. Retrieved content remains data, never
instruction authority.

Embedding artifacts are pinned to commit
`751bff37182d3f1213fa05d7196b954e230abad9` with verified model/tokenizer checksums.
Loading and downloads are single-flight; successful vectors use a bounded cache
keyed by backend identity and text, with no failure publication. One search
operation binds a coherent embedding backend for its indexes and query. If
primary inference fails, the whole operation retries once on the lexical
fallback; it never mixes semantic and fallback vector spaces in one result.

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
results always retain the Markdown path, byte snapshot identity, page version, and source
provenance so a workflow can cite the exact corpus state it used.

The separate ingestion pipeline fetches, parses, deduplicates, links, embeds,
and proposes Markdown revisions. Curation and publication are explicit Git
operations; a fetched page does not automatically become trusted knowledge.

Each workspace has full read access to the global Markdown corpus by default,
subject to the retrieval restrictions above. The agent retrieves pages directly
through the knowledge service rather than loading the full corpus into agent
memory or context. The Markdown corpus remains the canonical library; search
indexes are rebuildable representations, not a second canonical copy.

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

Generated strategies are Rust programs authored against the public
quant-harness strategy SDK (the harness JIT-compiles and loads them at run
time). A strategy bundle has immutable content identity from the first backtest
and contains at least:

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

The local release is for one authenticated operator. The data model already uses
explicit workspace ownership, and the public release extends it to invite-only
named users with per-request workspace authorization and a seeded owner account
(`docs/adr/adr-001-invite-only-public-multi-user-access-for-thesis.md`).

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
- custom workflow phase timelines with steer, stop, and cancel;
- tool and backtest job status, including results that finish after steering;
- ordered cursor-based reconnect/replay with duplicate suppression and visible
  message/upload retry states;
- safe workspace switching while background executions continue;
- investigation, strategy lineage, experiment, and artifact navigation;
- experiment-contract cards and change history;
- evidence grades, cutoff overlap notices, exposure history, and limitations;
- strategy diffs, tests, backtest comparisons, and RCA reports;
- global knowledge search with source/version citations and trust controls;
- model role/version/cutoff provenance;
- explicit consent-grant and activation-gatekeeper controls; and
- workspace repository archive, export, restore, and deletion controls.

The workspace-state API and UI expose only `lineages`, `evidence`, and
`consentAudit`; they do not include a planning DAG or planning demo.

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
| Tool gateway | Least-privilege tools. Harness MCP and scoped shared/workspace knowledge search/read are implemented; sandbox, web, Git, protected-test, and activation-request families remain roadmap work. |
| Experiment service | Contracts, strategy lineages, observed/declared attempt ledger, and a read-only mirror of protected eligibility/exposure. |
| Evidence service | Claims, grades, modifiers, citations, limitations, and report assembly. |
| Knowledge service | Global Markdown corpus, hybrid search, links, citations, and workspace trust overlays. |
| Model registry | Exact model identity, role eligibility, cutoff evidence, and per-session verification. |
| Protected-test client | Immutable submission and policy-approved aliased result retrieval. |
| Consent service | Scoped grant creation, expiration, revocation, and gatekeeper requests. |
| Operations hardening | Mode-independent workspace/event/artifact/consent/audit boundary checks, documented recovery for checkpoint incompatibility, queue interruption, Git publication interruption, and partial tool completion, and repository size, backup, restore, and retention enforcement. |

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
- `ExperimentContract` and `ResearchMandate`
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
- Define the public Thesis-to-harness client boundary.
- Create the versioned workspace repository template and local workspace
  provider.
- Implement the queued fenced single-writer workspace coordinator,
  journal/outbox, and crash reconciliation.
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
  -> see a durable workflow timeline
  -> generate and test Rust strategy code
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
- Implement sandbox, workspace Git, market-lake, and quant-harness tools.
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

- Add first-class falsification-first hypothesis-development and autonomous
  discovery workflows.
- Add workflow-specific custom phases and phase-required subagent roles.
- Add investigation and strategy-lineage navigation across multiple chats.
- Support concurrent investigations with isolated branches/artifacts and
  serialized canonical workspace mutations.
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
- Harden current-contract queue/checkpoint recovery, dead-letter operations, and
  cost controls beyond the minimum durable vertical-slice behavior. Outdated
  checkpoints are rejected; necessary conversion is a one-time cutover operation.
- Test workspace cross-access, prompt injection, secret exfiltration, tool
  confused-deputy, and alias-mapping attacks.
- Add backup, restore, repository integrity, workspace export/import, and
  platform-bundle upgrade procedures.
- Measure Git performance with realistic option and tick artifacts and make the
  chosen all-in-Git limits explicit.
- Enable live activation only after paper/shadow qualification, rollback,
  recovery, backup/restore, security, audit, and operator controls pass their
  explicit gates.
- Deliver invite-only named-user authorization as the designed public milestone
  (`docs/adr/adr-001-invite-only-public-multi-user-access-for-thesis.md`);
  organizations and public multi-tenancy remain out of scope.

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

The following decisions should be resolved before their owning phase begins.
Decisions recorded after this list was authored are tracked in the accepted design
records; conversation anchoring, datum rendering, anchor-owned executions,
workflow-node ownership, and per-node operator policy were settled in
`thesis-chat-surface-and-subagent-anchoring-v1` and are specified as REQ-018
through REQ-040 in `thesis-agent-workflow-runtime.md`. The working rules for
authoring workflows are in `thesis-workflow-authoring-guide`; the target
conversation abstraction is `thesis-conversation-abstraction-v1`.

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
- Organization/multi-tenant collaboration and approval roles. Invite-only named
  users are decided
  (`docs/adr/adr-001-invite-only-public-multi-user-access-for-thesis.md`).

## Success Criteria

Thesis reaches its intended architecture when:

- a local operator can create two workspaces and automated tests prove their
  files, memory, tools, and artifacts cannot cross;
- local Thesis and its UI can reopen workspace history while offline, acquire
  Azure AI Foundry access through backend-only Entra credentials when online,
  and never expose provider tokens to the browser or Git;
- multiple chats and concurrent investigations can safely publish canonical
  workspace state without overwriting unrelated work or losing Git history;
- an autonomous workflow survives disconnect/restart, accepts immediate
  steering, and retains in-flight tool results;
- a generated Rust strategy runs through the same published quant-harness
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


### Stateless API and durable execution ownership

HTTP replicas accept canonical input and read durable state. Production composes
`thesis_runtime/execution/worker.py` with `thesis_runtime/adapters/cosmos/work.py` and `thesis_runtime/adapters/cosmos/ownership.py`; the
`thesis worker` entrypoint executes work independently of `thesis serve`.
API replicas may scale to zero. The worker Container App retains one warm replica
and permits multiple replicas; correctness does not depend on replica count.
Local `thesis_local_api.py` embeds the same durable worker for convenience.

Canonical incoming persistence remains `thesis_runtime/conversations/incoming.py::write_incoming`.
The catalog transaction creates the input, timeline sequence, and runnable
reference together. Input positions, consumption acknowledgements, chat updates,
spawn admission, and execution acceptance use storage ownership rather than
process locks in production. Local coordinator state is derived bookkeeping.
Timeline notifications remain hints; cross-process readers poll durable cursors.

Workers acquire a renewable resource claim with a token and increasing fence.
Every authoritative worker mutation checks that claim in the SAME Cosmos batch
as its data write. All runtime adapters share one catalog ContainerProxy and
workspace partition for execution documents and work. Immutable FCA submission
references are retained in their own container before dispatch.
A stale owner cannot publish success, failure, tool results, or input ACKs.
External dispatch checks current authority; a call already in progress can still
finish remotely after lease loss, so retained job IDs remain essential.
Expired running attempts become interrupted and are never automatically replayed.
New operator input is independent work. Missing phase dispatch is repaired from
durable launches and accepted completion contracts, without rerunning completed
or interrupted phase work. The engine still never requests operator input.

`thesis_runtime/storage/ownership.py` and the WorkStore port contain execution authority;
Cosmos adapters implement transactions, not conversation policy. The serving
composition binds model, conversation, and workflow handlers. These boundaries
allow a future Rust core to own the inbox, claims, and phase contracts while
workspace and harness tools remain injected transports, including MCP hosts.

### Immutable submissions and tool contracts

Every serving harness mutation retains its allocated job_id and frozen inputs
before dispatch, independently of workflow ToolJob tracking. The Cosmos journal
is shared by API and worker replicas; local workspace composition uses SQLite.
Workspace submissions are inspectable at `/v1/workspaces/{workspace_id}/submissions`.
Unknown outcomes retain their original ID and are reconciled without replay.
HTTP backtest submission accepts inline `config`, returns 202, and never accepts
a mutable harness-host path. Screening submission returns 202; the caller waits
and explicitly materializes a completed retained result.

Session tools reject duplicate identities and malformed Draft 2020-12 schemas
at registration. Local references resolve without network retrieval; unsupported
remote references fail closed. Native harness operations which overlap workspace
program operations use stable `harness_` names. Workspace tools keep their domain
names. The optional path-only native `launch_trader` is withheld from session
catalogs until it supports immutable cross-host submissions.
Synchronous HTTP storage, authentication, and MCP work runs in FastAPI's thread
pool; streaming endpoints retain their asynchronous boundary.

Deployment cutover: workflow documents now live in the workspace catalog rather
than the old executionId partition. Drain active executions before deploying this
shape; no automatic replay or migration is introduced. Existing old-container
data is not deleted by runtime startup.


## Inline study evidence and dataset semantics

Study runs render one `StudyToolCard` execution header and shared research surface.
The existing read-only job observer supplies lifecycle state; completion reveals
research tabs in place. Reads select their requested output. Revision and validation
receipts remain compact. This changes no canonical message or submission path.

`research/ResearchExplorer.svelte` loads selected output evidence lazily through the
existing immutable artifact API. All outputs and receipt metrics are inspectable
in Returned data. `ReturnedData`, `EvidencePanel`, and the renderer registry share
recommendations, compatible field controls, linked evidence, and in-place expansion
between live chat and the demo. Renderer registrations own optional mapping policies.

The SDK publishes independent `StudyOutput::DatasetSemantics` descriptors (schema
1) with dataset bindings, row meaning, complete grain, field roles, ordering and
preferred views. The host checks complete-output grain and agreement with exhibit
bindings. UI checks and filters cover loaded observations only (5,000 rows or about
2 MB per output); complete artifact export remains authoritative. No silent client
aggregation, guessed financial statistics, or automatic historical reruns are added.
