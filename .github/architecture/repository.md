# Repository Architecture

<!-- markdownlint-disable MD013 -->

## Status

This document describes the target repository and deployment boundaries. The
initial skeleton, market-lake process package, shared image, foundational
storage, identity, and Container Apps modules, and the market-lake
resource-group infrastructure entry point are implemented. Versioned lake
configuration, partition/schema/calendar contracts, and reusable image
publication are also implemented. The quant-harness capability and process
scaffold, core records, deterministic event ordering, single-leg OMS, and
typed configuration are implemented; broader simulation, CI/deployment
orchestration, and deployment remain planned.

## Product Boundaries

| Boundary | Path | Responsibility |
| --- | --- | --- |
| `market-lake` | `src/lake` | Provider ingestion, stable lake schemas, storage and catalog access, coverage planning, and the lake API/client surface. |
| `quant-harness` | `src/harness` | Deterministic simulation, OMS and portfolio behavior, results, paper and shadow runtimes, and capability-gated live execution. |
| `lakeclient` | `src/lakeclient` | Consumer-facing authenticated ensure, polling, file-query, and Parquet access without provider or Azure credentials. |
| `thesis` | `src/thesis` | Workspace-scoped research, coordination, persistence, authorization, and model-provider boundaries. |
| Strategies | `strategies` | User-authored implementations of the public harness strategy contract. |

Dependency direction is from the harness and strategies toward published
contracts, never from the lake into harness runtime code. The harness may use
lake schemas, catalog records, APIs, and clients, but the lake must remain
operable without the harness. Strategies depend on the harness contract rather
than engine internals.

## Market-Lake Packages

`src/lake` is organized by capability rather than by deployment layer. Public
package facades expose deliberate contracts; provider SDKs, FastAPI, Azure
adapters, and orchestration details stay behind those boundaries.

| Package | Responsibility |
| --- | --- |
| `lake.catalog` | Coverage, job, contract, and front-set records; repository ports; memory and Azure Table adapters. |
| `lake.cli` | Console dispatch and API, ingest, or local process commands. |
| `lake.configuration` | Strict YAML loading, typed settings, environment overrides, and normalized universes. |
| `lake.contracts` | Stable partition paths, PyArrow schemas, and exchange or UTC session contracts. |
| `lake.coverage` | Calendar-aware coverage classification, gap planning, and ensure-oriented application behavior. |
| `lake.http` | FastAPI composition, authentication, request models, and thin catalog, ensure, and query controllers. |
| `lake.ingestion` | Shared ingest ports plus equity, option, and crypto planning, transactions, and worker construction. |
| `lake.local` | Workstation-only API, Alpaca ingest, filesystem, and catalog composition. |
| `lake.providers.alpaca` | Async Alpaca transport, pagination/retry behavior, and normalized provider records. |
| `lake.query` | File-query orchestration and bounded DuckDB execution over ensured objects. |
| `lake.queue` | Durable delivery policy, Azure queue adapter, dead-letter envelopes, and explicit replay. |
| `lake.storage` | Storage ports, Parquet serialization, Azure blob/SAS adapters, capacity guards, and leases. |

```text
src/lake/
|-- app.py                         Published Uvicorn facade (`lake.app:app`)
|-- catalog/                       Records, repository ports, and adapters
|-- cli/                           `lake` command implementation
|-- configuration/                 Settings and universe loading
|-- contracts/                     Paths, schemas, and sessions
|-- coverage/                      Coverage domain and application services
|-- http/
|   `-- routes/                    Thin authenticated HTTP controllers
|-- ingestion/
|   |-- equities/                  Equity transaction and backfill behavior
|   |-- options/                   Front-set selection and option ingestion
|   `-- crypto/                    UTC-day planning and crypto ingestion
|-- local/                         Workstation-only composition and state
|-- providers/
|   `-- alpaca/                    Provider records and async HTTP client
|-- query/                         File and bounded SQL query services
|-- queue/                         Worker, Azure adapter, and dead letters
`-- storage/                       Ports, Parquet, Azure, capacity, and leases
```

Dependency direction flows inward. HTTP routes and CLI commands invoke
application services; application services depend on catalog, queue, storage,
and provider ports; Azure, Alpaca, FastAPI, and DuckDB implementations remain
at adapter boundaries. Domain contracts do not import HTTP or CLI code.

Tests mirror the capability layout under `tests/lake`:

```text
tests/lake/
|-- catalog/
|-- cli/
|-- configuration/
|-- contracts/
|-- coverage/
|-- http/routes/
|-- ingestion/{equities,options,crypto}/
|-- providers/alpaca/
|-- query/
|-- queue/
`-- storage/
```

## Quant-Harness Packages

`src/harness` mirrors the product-first structure under `tests/harness`.

| Package | Responsibility |
| --- | --- |
| `harness.configuration` | Strict API, backtest, and trader configuration; runtime-only secret references; canonical cross-machine hashes. |
| `harness.core` | Decimal-safe records, deterministic IDs, explicit clocks, and the synchronous event queue and bus. |
| `harness.oms` | Execution-report-driven single-leg lifecycle, exact fill ledgers, replacements, and session directives. |
| `harness.data` | Live/replay market-data boundaries and synchronous normalized event inboxes. |
| `harness.fills` | Configuration-selected exact fee and commission models. |
| `harness.portfolio` | Strategy virtual books and shared-account aggregate accounting. |
| `harness.venues` | Fire-and-forget execution boundary and deterministic scripted venue. |

The event ordering contract is timestamp, source sequence, fixed serialized
event-class priority, then incoming feed order. `SimClock` advances only from
explicit events; `WallClock` is an adapter boundary and is not used by the
synchronous simulation kernel. OMS status and fill-ledger changes occur only by
applying validated execution reports. Configuration hashes normalize mapping
order, exact decimals, and UTC timestamps, and reject resolved secret values.

The remaining `results`, `risk`, `runtime`, `strategy`, and `testing` facades
reserve capability boundaries for later tasks. Thin `harness`, `api`,
`backtest-job`, and `trader` command entrypoints make the future process
boundaries explicit and fail clearly until composed.

## Thesis Packages

Thesis remains a peer product. `thesis.workspaces` owns isolated workspace
identity and repository scaffolds; `thesis.cleo` serializes workspace-local
planning mutations; `thesis.persistence` journals accepted commands and fences
Git publication; `thesis.auth` binds operator sessions to workspaces; and
`thesis.providers` keeps model credentials behind backend-only protocols.
Workspace identity remains `.thesis/workspace.yaml`; publication state is
written separately to `.thesis/publication.json`.

## Shared Foundation

The products share repository-level mechanics where divergence would create
operational drift:

- `pyproject.toml` defines the Python project and common quality tooling.
- `Dockerfile` builds one immutable image with distinct runtime commands.
- `config` contains versioned, non-secret YAML grouped by product or concern.
- `infra` contains reusable Bicep modules, environment parameters, identity,
  storage, observability, and Container Apps definitions.
- `.github/workflows` provides path-filtered checks, image publication, and
  OIDC-based deployment.
- `tests/lake` and `tests/harness` mirror their product capability boundaries.
  `tests` also holds explicit cross-product contract tests where integration is
  intentional.

## Market-Lake Deployment

`infra/main.bicep` is the resource-group entry point for the market-lake dev
slice. `infra/parameters/dev.bicepparam` selects `westus2` and the `dev`
environment. Names use the `fca-ml-dev` prefix; the storage account and Key
Vault add deterministic suffixes derived from the deployment resource group,
while the remaining resource names are stable from the prefix and environment.

The composition deploys storage, managed identities and GitHub OIDC, Key
Vault, runtime access, Container Apps, and cost controls together. The dev
parameters explicitly enable the Key Vault public endpoint because no private
endpoint module is composed yet; the vault remains Entra-only and RBAC-based.
The private GHCR image is mirrored by digest into a private Basic ACR during
deployment. Container Apps pulls the mirrored digest with the runtime managed
identity; the ACR admin account is disabled. The budget alert contact
placeholder must be replaced with a valid user-supplied email before Azure
operations. No secrets are stored in Bicep or parameter files.

Container Apps logs use the `PerGB2018` Log Analytics SKU with its required
30-day minimum retention. The application operations setting reports the same
effective retention guardrail.

Sharing these roots does not imply unrestricted cross-package imports. New
shared code should be introduced only when a stable contract has more than one
real consumer.

## Runtime Shape

```text
market data providers
        |
        v
market-lake ingest jobs --> Azure lake + catalog --> market-lake API/client
                                      |                       |
                                      +-----------+-----------+
                                                  v
                                quant-harness backtest jobs

live market feeds --> quant-harness paper/shadow/live runtime --> broker API
```

Market-lake APIs and ingest jobs, harness backtest jobs, and harness trading
runtimes are separate commands from the shared image. Azure resources use
managed identity and least-privilege access. GitHub Actions uses OIDC and
immutable image references; secrets remain in environment-specific secret
stores rather than source control.

## Delivery Rules

- Land package, image, infrastructure, and workflow behavior only in the CLEO
  task that owns it; reserved paths do not imply that planned behavior exists.
- Validate product changes with path-focused checks plus contract tests at
  product boundaries.
- Keep deterministic engine code independent of wall-clock time, unseeded
  randomness, and direct network access.
- Gate deployment progressively: local tests, development infrastructure,
  bounded data soaks, paper and shadow qualification, then explicit live
  eligibility.

The authoritative implementation sequence and cross-product dependencies are
in `.cleo-export.json`, rooted at `T001` for `market-lake` and `T068` for
`quant-harness`.
