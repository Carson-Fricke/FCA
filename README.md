# FCA

<!-- markdownlint-disable MD013 -->

FCA is a planned financial data and quantitative trading platform. One
repository contains two products that share Python tooling, configuration,
infrastructure patterns, a container image, and delivery workflows while
retaining clear product boundaries.

## Products

| Product | Purpose | Status |
| --- | --- | --- |
| `market-lake` | Build and serve an on-demand Azure lake for equities, options, and crypto market data. | Dev deployment and local workstation mode available |
| `quant-harness` | Run deterministic backtests, paper and shadow trading, and risk-controlled live trading over lake and live data. | Package and process scaffold available; runtime capabilities planned |

The market lake is a data product. The quant harness consumes its published
schemas, catalog, API, and client contracts; it does not make the lake an
internal implementation detail. Shared repository assets should remove
duplication without merging the two products into one package.

## Status

The repository currently contains the CLEO roadmap, project guidance, the
tracked directory skeleton, an installable market-lake package, a shared
runtime image, versioned market-lake configuration, core partition, schema,
and calendar contracts, build-validated Azure foundation modules, a reusable
GHCR publication workflow, a protected main-branch deployment workflow, the
quant-harness package scaffold, and a supported local market-lake mode. The
quant-harness runtime capabilities remain assigned to later CLEO tasks.

## Architecture

The planned runtime has two connected product planes:

1. `market-lake` ingests provider data into Azure storage, records catalog and
   coverage state, and exposes authenticated ensure and query operations.
2. `quant-harness` uses point-in-time lake data for deterministic simulation
   and live feeds for paper, shadow, and live shells under one strategy
   contract.
3. Root configuration, infrastructure conventions, image construction, and
   path-filtered workflows are shared. Product source and tests remain
   independently identifiable.

See [`.github/architecture/repository.md`](.github/architecture/repository.md)
for boundaries, dependency direction, and the planned deployment shape.

## Repository Map

Paths marked `planned` are intentionally empty or represented by a marker;
later tasks own their implementation.

```text
.
|-- .cleo-export.json              Portable CLEO roadmap export
|-- .cleo/                         Allow-listed CLEO context and RCASD records
|-- .github/
|   |-- architecture/              Project architecture documentation
|   |-- instructions/              Repository-scoped agent guidance
|   |-- scripts/                   Repository validation utilities
|   |-- verification/              Tracked task validation records
|   `-- workflows/                 Shared CI, image publication, and deployment
|-- config/                        Versioned, non-secret product configuration
|-- infra/                         Shared Azure Bicep modules (foundation and runtime)
|-- src/
|   |-- lake/
|   |   |-- catalog/               Records, repositories, and table adapters
|   |   |-- cli/                   API, ingest, and local command entrypoints
|   |   |-- configuration/         Typed settings and universe loaders
|   |   |-- contracts/             Paths, schemas, and market sessions
|   |   |-- coverage/              Coverage classification and planning
|   |   |-- hosted/                Hosted Azure composition for the API process
|   |   |-- http/                  FastAPI composition and thin routes
|   |   |-- ingestion/             Equity, option, and crypto workflows
|   |   |-- local/                 Workstation API, ingest, and persistence
|   |   |-- providers/             External market-data adapters
|   |   |-- query/                 File and bounded SQL query services
|   |   |-- queue/                 Durable worker and dead-letter behavior
|   |   `-- storage/               Parquet, Azure, capacity, and leases
|   |-- harness/                   quant-harness capabilities and processes
|   |-- lakeclient/                Consumer-facing market-lake Python client
|   `-- thesis/                    Workspace-scoped research platform
|-- strategies/                    Harness strategy namespace
|-- tests/
|   |-- harness/                   Tests mirroring quant-harness capabilities
|   |-- lake/                      Tests mirroring market-lake capabilities
|   |-- lakeclient/                Market-lake client contract tests
|   `-- thesis/                    Thesis capability tests
|-- Dockerfile                     Shared runtime image definition
|-- pyproject.toml                 Shared Python project and tools
`-- README.md                      Repository entry point
```

## Python Tooling

Use `uv` for Python environments, dependency resolution, command execution,
and package builds. Ruff is the repository linter and formatter, and `ty` is
the type checker; do not add a parallel MyPy configuration.

```powershell
uv run --python 3.12 --extra dev ruff check .
uv run --python 3.12 --extra dev ruff format --check .
uv run --python 3.12 --extra dev ty check
uv run --python 3.12 --extra dev pytest
```

The quant-harness backtest runtime is implemented; the API and trader commands
remain scaffolded while their roadmap tasks own their implementations:

```powershell
uv run harness --help
uv run api --help
uv run backtest-job --help
uv run trader --help
```

Launching a scaffolded harness process without `--help` fails explicitly until
its owning implementation task lands.

## Local Backtest Benchmark

The committed backtest example runs end to end on the deterministic synthetic
benchmark fixture, offline and without credentials, and produces byte-identical
artifacts on every run:

```powershell
uv run backtest-job --config config/harness/backtest.yaml
```

Artifacts are written under `.local/backtests/<run_id>/backtest.json` and
include the reproducibility manifest hash and run artifact digest. Pass
`--data-dir <path>` to replay a user-supplied local Parquet lake directory
(`date=/asset_class=/data_type=/SYMBOL.parquet`) instead of the built-in
fixture.

## Local Market Lake

The supported workstation mode uses local files and real bounded Alpaca data;
it does not require Docker, Azure CLI, Azure resources, or Azure credentials:

```powershell
uv sync --python 3.12 --extra dev
uv run lake local api --host 127.0.0.1 --port 8000
uv run lake local ingest --symbol AAPL --max-records 100
uv run lake local reset --yes
```

See [`docs/local-market-lake.md`](docs/local-market-lake.md) for prerequisites,
required environment variables, authenticated smoke checks, safe cleanup, and
the local-versus-Azure boundary.

## Development Principles

- Keep `src/lake` and `src/harness` as separate product namespaces in one
  Python project. Put reusable behavior behind explicit contracts rather than
  a general-purpose shared package by default.
- Keep strategy code in `strategies` and depend only on the harness's public
  strategy contract.
- Keep versioned, non-secret YAML under `config`. Use environment variables or
  the deployment secret store for credentials; local overrides and secrets are
  ignored.
- Mirror product boundaries in `tests`. Determinism, schema compatibility, and
  integration behavior require focused gates as their implementations arrive.
- Treat local lake data, backtest output, coverage reports, caches, and build
  artifacts as generated data, not source-controlled inputs.

## Deployment Principles

- Define Azure resources and environment parameters under `infra` with Bicep;
  avoid portal-only infrastructure changes.
- Build one commit-addressed image with product-specific API, ingest, backtest,
  and trader commands rather than maintaining divergent images.
- Use path-filtered GitHub Actions and OIDC for validation and deployment. Do
  not store cloud or broker credentials in GitHub workflow files or committed
  configuration.
- Prefer managed identity, least-privilege access, scale-to-zero services, and
  explicit cost and rollout gates. Development validation precedes broader
  data ingestion or live-trading eligibility.

The only repository-triggered Azure mutation path is
[`.github/workflows/deploy-main.yml`](.github/workflows/deploy-main.yml). A
push to `master` publishes one digest-addressed image, waits for approval on the
selected protected GitHub Environment, deploys the resource-group Bicep
composition, verifies the API and ingest job image references, and runs the
probe-safe API health smoke. Required repository and Environment values are
recorded in [`.github/verification/T031-validation.json`](.github/verification/T031-validation.json);
manual or alternate production mutation workflows are not part of this
repository path.

The one-time identity, immutable OIDC subject, GitHub Environment, and recovery
procedure is documented in
[`.github/runbooks/github-azure-oidc-bootstrap.md`](.github/runbooks/github-azure-oidc-bootstrap.md).

## Roadmap

The portable roadmap is [`.cleo-export.json`](.cleo-export.json). The
`market-lake` roadmap is rooted at CLEO task `T001`; the `quant-harness`
roadmap is rooted at `T068`. Tracked research and lifecycle material lives
under [`.cleo/rcasd/`](.cleo/rcasd/). The local CLEO database and runtime state
are intentionally excluded from Git; the export is the reviewable project
roadmap.
