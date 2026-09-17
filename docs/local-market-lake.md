# Local Market Lake

<!-- markdownlint-disable MD013 -->

Local mode is the supported developer path for running the market-lake API and
one bounded equity ingest on a workstation. It uses Alpaca's real market-data
API, local Parquet files, a local JSON coverage catalog, and bounded DuckDB
queries. It does not require Docker, Azure CLI, an Azure subscription, Azure
resources, or Azure cloud credentials, and it never constructs an Azure
credential unless a non-local command is selected.

Local mode is not an offline demo. There is no supported fixture provider. Real
Alpaca credentials with access to the configured SIP feed are mandatory for
`lake local ingest`; fake providers are used only by automated tests.

## Prerequisites And Install

- Python 3.12. Other Python minor versions are outside this repository's
  supported range.
- `uv` 0.12 or newer available on `PATH`.
- Real Alpaca market-data API key and secret with SIP entitlement.
- Network access to `https://data.alpaca.markets` for ingest only.

From the repository root, create the locked environment and install contributor
tools:

```text
uv sync --python 3.12 --extra dev
```

The locked project installs DuckDB, exchange-calendars, FastAPI, HTTPX, orjson,
PyArrow, Pydantic Settings, tenacity, and Uvicorn for local behavior. It also
installs the repository's Azure Data Tables, Azure Identity, Azure Blob Storage,
and Azure Queue Storage SDK dependencies used by non-local deployment adapters;
local commands do not compose those adapters and do not require Azure CLI or
Azure authentication. The optional `dev` extra installs pytest, Ruff, and ty.
Setuptools is used only as the package build backend.

## Environment

Local mode reads secrets only from the process environment. `.env.example`
lists names but contains no values; `.env` and `.env.*` secret files are ignored
by Git. `uv run` does not load `.env.example` automatically.

| Variable | Required by | Purpose |
| --- | --- | --- |
| `LAKE_API__BEARER_TOKEN` | `local api` | Non-empty token for all `/v1/*` routes. Use a random development-only value. |
| `LAKE_PROVIDER__API_KEY` | `local ingest` | Real Alpaca API key ID. |
| `LAKE_PROVIDER__API_SECRET` | `local ingest` | Real Alpaca API secret. |
| `LAKE_LOCAL_DATA_DIR` | Optional | Local root; defaults to `.local/market-lake` under the repository. `--data-dir` takes precedence. |
| `LAKE_CONFIG_ROOT` | Optional | Repository root or `config/market-lake` directory containing the versioned YAML configuration. |

Do not put secrets on command lines, in shell history, in catalog JSON, or in
captured logs. The commands never print credential values. Missing or empty
credentials fail before an Alpaca client is created.

### Windows PowerShell

```powershell
$env:LAKE_API__BEARER_TOKEN = "replace-with-a-random-local-token"
$env:LAKE_PROVIDER__API_KEY = "replace-with-real-alpaca-key-id"
$env:LAKE_PROVIDER__API_SECRET = "replace-with-real-alpaca-secret"
```

### POSIX Shell

```sh
export LAKE_API__BEARER_TOKEN='replace-with-a-random-local-token'
export LAKE_PROVIDER__API_KEY='replace-with-real-alpaca-key-id'
export LAKE_PROVIDER__API_SECRET='replace-with-real-alpaca-secret'
```

Unset these variables when the session is finished. Rotating a secret is the
correct response if it is pasted into a terminal transcript, issue, or commit.

## Start The API

Run the foreground server from the repository root:

```text
uv run lake local api --host 127.0.0.1 --port 8000
```

The bind address defaults to `127.0.0.1`; do not use `0.0.0.0` on an untrusted
network. Health endpoints are public at `http://127.0.0.1:8000/health` and
`/healthz`. API documentation is at `/docs`. All `/v1/*` routes require exactly
`Authorization: Bearer <LAKE_API__BEARER_TOKEN>`.

### Authenticated API Smoke

PowerShell, in a second terminal:

```powershell
Invoke-RestMethod -Uri "http://127.0.0.1:8000/health"
$headers = @{ Authorization = "Bearer $env:LAKE_API__BEARER_TOKEN" }
Invoke-RestMethod -Uri "http://127.0.0.1:8000/v1/auth/check" -Headers $headers
```

POSIX:

```sh
curl --fail --silent --show-error http://127.0.0.1:8000/health
curl --fail --silent --show-error \
  -H "Authorization: Bearer ${LAKE_API__BEARER_TOKEN}" \
  http://127.0.0.1:8000/v1/auth/check
```

Both responses have `{"status":"ok"}`. A missing or incorrect bearer token
returns HTTP 401 without disclosing the configured value.

### Python Client Smoke

`lakeclient` is the consumer-facing API client used by applications and the
planned harness lake adapter. It sends the bearer token only to the configured
market-lake API, polls ensure jobs, consumes API-issued file URLs, and never
calls Alpaca or constructs Azure credentials.

```python
from datetime import date

from lakeclient import AssetClass, DatasetRequest, DataType, EnsureAccepted
from lakeclient import LakeClient, nbbo_spread

request = DatasetRequest(
    asset_class=AssetClass.EQUITY,
    data_type=DataType.QUOTE,
    symbol="AAPL",
    start_date=date(2024, 1, 19),
    end_date=date(2024, 1, 19),
)
with LakeClient(
    "http://127.0.0.1:8000",
    "replace-with-LAKE_API__BEARER_TOKEN",
) as client:
    result = client.ensure(request)
    if isinstance(result, EnsureAccepted):
        client.wait_for_job(result.job_id)
    files = client.query_files(request)

print(nbbo_spread(files.files).fetchall())
```

Supply the bearer value through runtime secret handling in real applications;
do not commit it or hard-code it in source.

## Real-Alpaca Smoke

With the real Alpaca variables set, run one configured equity session. The
default is the latest closed XNYS session and the first soak symbol (`AAPL`).
The lake paginates the fetch internally, so there is no record-count flag:

```text
uv run lake local ingest --symbol AAPL
```

To make the request reproducible, provide a closed trading date:

```text
uv run lake local ingest --symbol AAPL --session-date 2026-09-01
```

The command emits one JSON object containing `symbol`, `session_date`, coverage
summaries, catalog path, and deterministic object paths. It returns nonzero when
credentials are absent, Alpaca rejects the request, provider data is malformed,
or local persistence fails. Coverage reflects the session window the lake
observed: a closed session is recorded as `complete`, while a still-open or
erroring fetch is recorded honestly as `partial` or `error`.

With the API running, inspect the persisted coverage using the date printed by
the ingest result. PowerShell:

```powershell
$headers = @{ Authorization = "Bearer $env:LAKE_API__BEARER_TOKEN" }
$date = "2026-09-01"
$uri = "http://127.0.0.1:8000/v1/catalog/coverage?asset_class=equity&data_type=trade&symbol=AAPL&start_date=$date&end_date=$date"
Invoke-RestMethod -Uri $uri -Headers $headers
```

POSIX:

```sh
date='2026-09-01'
curl --fail --silent --show-error \
  -H "Authorization: Bearer ${LAKE_API__BEARER_TOKEN}" \
  "http://127.0.0.1:8000/v1/catalog/coverage?asset_class=equity&data_type=trade&symbol=AAPL&start_date=${date}&end_date=${date}"
```

The SQL query endpoint is available for complete local catalog partitions and
uses the existing read-only scan and result limits. The bounded smoke above is
partial by design, so it validates provider, Parquet, catalog, and coverage
behavior rather than claiming a complete SQL-queryable session.

## Data Lifecycle

The default `.local/market-lake` tree is covered by the repository `.gitignore`:

```text
.local/market-lake/
|-- .market-lake-local
|-- objects/                 # deterministic partitioned Parquet files
`-- state/catalog.json       # deterministic coverage catalog
```

Repeated ingestion of the same symbol and session replaces the same Parquet
paths and catalog keys. Run only one local ingest writer at a time. The API can
read during atomic catalog replacement. Job state is in-memory and resets when
the API process stops; coverage catalog state persists until reset.

Stop the foreground API with `Ctrl+C`, then remove generated state:

```text
uv run lake local reset --yes
```

`reset` deletes only a directory bearing the local-mode ownership marker. It
refuses an existing non-empty unmarked directory, which protects an accidental
`LAKE_LOCAL_DATA_DIR` value. To inspect an alternate tree, pass the same
`--data-dir <path>` to every local command or set `LAKE_LOCAL_DATA_DIR`.

## Troubleshooting

| Symptom | Resolution |
| --- | --- |
| `LAKE_API__BEARER_TOKEN is required` | Set a non-empty process variable before starting the API. |
| Alpaca key/secret required | Set both provider variables in the same shell that runs ingest. `.env.example` is not loaded automatically. |
| Alpaca HTTP 401 or 403 | Verify the key pair, market-data account, and SIP feed entitlement. No Azure login can fix an Alpaca authorization error. |
| Alpaca HTTP 429 or timeout | Wait for the provider limit to recover and rerun the same bounded command. Do not remove the record cap. |
| Symbol is outside the configured universe | Use a symbol in `config/market-lake/universes.yaml`; local smoke does not bypass universe policy. |
| Session date rejected | Choose a closed XNYS trading session, not today while open, a weekend, a holiday, or a future date. |
| Non-empty directory not created by local mode | Correct `LAKE_LOCAL_DATA_DIR` or choose a new empty directory. Do not add the marker manually. |
| Local catalog is invalid | Stop the API, preserve the tree if diagnosis matters, then run `lake local reset --yes` and ingest again. |
| Port already in use | Stop the old process or select another `--port`, then use that port for smoke URLs. |
| Query reports incomplete coverage | A session that is still trading is `partial`; a closed session is `complete` or `empty`. A closed session that stays incomplete means an ingest was interrupted, so re-run the ensure for that window. |

## Local Versus Azure

Local mode supports a single developer process, one bounded equity session per
ingest command, filesystem Parquet, persistent local coverage JSON, local file
URLs, and bounded DuckDB SQL. It does not provide Azure Blob or Table storage,
SAS URLs, queues, leases, managed identity, Key Vault, autoscaling, lifecycle
tiers, distributed writers, or deployment observability. Production `lake api`
and `lake ingest` remain separate commands and may compose Azure resources.
Never use local mode as evidence that Azure identity, networking, RBAC, or
deployment behavior works.
