# Thesis Local Stack

Run the complete Thesis stack locally with no Azure dependency: the
quant-harness API (`harness api`), the Thesis API (workspace data routes with
chat/SSE), and the Svelte UI in real mode. The provider behind the Thesis
model calls is either OpenRouter (env key) or Azure AI Foundry (preflight);
both are optional for the connectivity chain this runbook verifies. This is a
local loopback stack: bearer credential names below are references, never
values — never echo a secret value into a terminal transcript, log, issue, or
task evidence.

## 1. Prerequisites

- Windows PowerShell 5.1 and this repository checked out.
- `uv` (Python 3.12 project environment): `uv sync --extra dev`.
- Node.js LTS with npm for the UI: `npm --version`; install once per checkout
  with `npm install` inside `src/ui`.
- One bearer secret per API boundary, generated locally (for example
  `[Convert]::ToBase64String((New-Object byte[] 32)).TrimEnd('=').Replace('+','-').Replace('/','_')`)
  and kept only in the environment of each terminal.
- The harness chain needs a runnable backtest config; the repository default
  is the deterministic synthetic fixture `config/harness/backtest.yaml`. Its
  engine path alone is verifiable without any server:
  `uv run backtest-job --config config/harness/backtest.yaml`.

## 2. Provider Setup

The Thesis backend resolves its model provider through a fail-closed preflight
(`thesis.providers`); workspace surfaces stay usable offline either way.

### OpenRouter (env key, default choice)

The TH6.9 adapter consumes an `EnvironmentKeySource`; the key lives only in
the process environment:

| Variable | Consumer |
| --- | --- |
| `OPENROUTER_API_KEY` | OpenRouter key source (`thesis/providers/openrouter.py`) |

Set it with a masked prompt so the value never lands in shell history:

```powershell
function Set-MaskedEnv([string] $name) {
  $secure = Read-Host "Enter $name" -AsSecureString
  $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
  try {
    Set-Item -LiteralPath "Env:$name" -Value ([Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr))
  } finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
  }
}
Set-MaskedEnv OPENROUTER_API_KEY
```

The provider preflight probes `POST /chat/completions` with `max_tokens: 1`.
Outcomes: key missing or rejected -> `authentication_unavailable`; network
failure -> `provider_unreachable`; otherwise `ready`.

### Azure AI Foundry (preflight)

Foundry needs no key in the environment; it authenticates with an Entra
credential and probes the deployment:

```powershell
az login
```

The non-secret profile lives in `config/thesis/runtime.yaml`
(`provider_profiles.*`): `endpoint` (HTTPS service URL), `project_name`,
`model_deployment`, `credential_mode` (`default_azure_credential` or
`managed_identity`), and `enabled: true`. Startup preflight obtains an access
token for scope `https://ai.azure.com/.default` and checks the deployment via
`deployment_available(project_name=..., deployment_name=...)`; a reachable
provider with a missing deployment reports `model_unavailable`.

## 3. Start The Stack

Three terminals, in order. All commands run from the repository root unless
stated.

### Terminal 1 — harness quant API (port 8001)

```powershell
Set-MaskedEnv HARNESS_API_BEARER_TOKEN
uv run harness api --config config/harness/api.yaml
```

`config/harness/api.yaml` binds `127.0.0.1:8001` and resolves its bearer from
`${HARNESS_API_BEARER_TOKEN}`; the process exits 2 when that variable is
unset. Unauthenticated `GET /health` answers `{"status": "ok"}`;
`GET /v1/auth/check` requires the bearer. `--host`/`--port` override the bind
address if 8001 is taken.

Optional preflight of the chain's backtest config in the same terminal or a
spare one:

```powershell
uv run backtest-job --config config/harness/backtest.yaml
```

### Terminal 2 — Thesis API (port 8000)

The `thesis serve` process boundary is scaffolded; until it composes the
application itself, run the equivalent composition inline. It wires the
T231 workspace data services (`build_local_workspace_data`) plus the T211
chat/SSE routers, and delegates backtest launch to the real local
`harness backtest-job --config <path>` subprocess:

```powershell
Set-MaskedEnv THESIS_API__BEARER_TOKEN
@'
import subprocess

import uvicorn

from thesis.http.application import create_app
from thesis.http.workspaces import build_local_workspace_data
from thesis.integrations.harness import ProcessResult


def runner(argv):
    done = subprocess.run(argv, capture_output=True, text=True)
    return ProcessResult(done.returncode, done.stdout, done.stderr)


uvicorn.run(
    create_app(workspace_data=build_local_workspace_data(harness_runner=runner)),
    host="127.0.0.1",
    port=8000,
)
'@ | uv run python -
```

Notes on the composition:

- The bearer is read from `THESIS_API__BEARER_TOKEN` (double underscore); the
  application fails closed with 401 when it is unset or blank.
- Add `workspace_root="<path>"` to `build_local_workspace_data(...)` to
  persist dashboard widgets and workspace state under `<path>`; omit it for
  in-memory storage. Workspaces themselves are created through the API (see
  section 4).
- Screener data context: with no `market_port` configured the screener
  surface answers 503 by design (fail closed when no data source is
  configured). Chat/SSE and the workspace data routes do not depend on it.

### Terminal 3 — UI dev server (port 5173)

```powershell
Set-Location src/ui
npm install
$env:PUBLIC_THESIS_API_BASE = 'http://127.0.0.1:8000'
Set-MaskedEnv PUBLIC_THESIS_API_BEARER_TOKEN
npm run dev
```

`src/ui/src/lib/api.ts` reads `PUBLIC_THESIS_API_BASE` and
`PUBLIC_THESIS_API_BEARER_TOKEN` at dev-server start; with a non-empty base
the UI boots in real-fetch mode (fixture mode is the default without one).
These are build-time variables: set them before `npm run dev`.

Browser cross-origin note: the UI origin (`http://localhost:5173`) differs
from the API origin (`http://127.0.0.1:8000`), and the Thesis API publishes no
CORS layer yet (browser hardening is deferred to the authenticated/TLS
deployment mode, see `.github/architecture/fca-thesis.md`). If the browser
blocks the fetches, use the fixture-mode escape hatch (section 5) or put the
dev server behind a same-origin proxy; the verification script in section 4
proves the API chain end to end without a browser.

## 4. Verify

With terminals 1 and 2 running, from the repository root:

```powershell
$env:THESIS_API_BEARER_TOKEN = $env:THESIS_API__BEARER_TOKEN
uv run python scripts/local/run_local_stack.py
```

Never pass or echo the token itself; the script reads it from the environment
(`THESIS_API_BEARER_TOKEN`, falling back to `THESIS_API__BEARER_TOKEN`) or
from `--token`, and redacts every value before printing.

Expected output with a healthy stack:

```
[PASS] harness-api-health: harness quant API healthy and authenticated
[PASS] thesis-api-auth-boundary: Thesis API healthy, anonymous 401, bearer accepted
[PASS] workspace-catalog: workspace 'local-stack' found in the catalog
[PASS] chat-message: chat chat-001 accepted message msg-001
[PASS] backtest-launch: backtest launch delegated through the harness boundary (exit_code=0); job_id is populated only by the remote harness API
stages: 5/5 passed
```

The first run has no workspace yet, so `workspace-catalog` fails with the
exact create call. Create the workspace once (the token is referenced by
name, never shown):

```powershell
Invoke-RestMethod -Method Post `
  -Uri 'http://127.0.0.1:8000/v1/workspaces' `
  -Headers @{ Authorization = "Bearer $env:THESIS_API_BEARER_TOKEN" } `
  -ContentType 'application/json' `
  -Body '{"name": "Local Stack"}'
```

Then re-run with the returned id:

```powershell
uv run python scripts/local/run_local_stack.py --workspace <returned-id>
```

Stages and flags:

| Stage | Probe | Skippable |
| --- | --- | --- |
| `harness-api-health` | `GET <harness-url>/health` -> 200 `ok`; bearer verified via `GET /v1/auth/check` when a harness token is set | no |
| `thesis-api-auth-boundary` | `GET /health` -> 200; anonymous `GET /v1/auth/check` -> 401; bearer -> 200 | no |
| `workspace-catalog` | `GET /v1/workspaces` contains `--workspace`; failure prints the create call | no |
| `chat-message` | `POST /v1/workspaces/{ws}/chats` then `POST .../chats/{chat}/messages` with the bearer | `--skip-chat` |
| `backtest-launch` | `POST /v1/workspaces/{ws}/backtests/launch` -> job id (remote harness) or process exit code (local `backtest-job`) | `--skip-backtest` |

Other flags: `--thesis-url`, `--harness-url`, `--workspace`,
`--bundle-ref`, `--config-path`, `--timeout`. Exit code 0 means every enabled
stage passed.

## 5. Troubleshooting

- **Port conflicts.** Terminal 1: `uv run harness api --config
  config/harness/api.yaml --port <port>` and pass `--harness-url
  http://127.0.0.1:<port>` to the verifier. Terminal 2: change `port=8000`
  in the composition and pass `--thesis-url http://127.0.0.1:<port>`.
  Terminal 3: `npm run dev -- --port <port>` (update
  `allowed_origins` in `config/thesis/runtime.yaml` if you serve the UI from
  a non-default origin against a CORS-enabled deployment). Confirm a free
  port first: `Get-NetTCPConnection -LocalPort <port>` (empty output means
  free).
- **401 on the Thesis API.** The server compares against
  `THESIS_API__BEARER_TOKEN` only; blank or unset variables fail closed. If
  the verifier reports a rejected bearer with `--token` supplied, the
  presented token differs from the one terminal 2 was started with — restart
  terminal 2 after correcting the variable. The UI's bearer is baked in at
  dev-server start: re-run `npm run dev` after changing
  `PUBLIC_THESIS_API_BEARER_TOKEN`.
- **Module not found.** Run Python commands through `uv` from the repository
  root (`uv sync --extra dev` first). For the UI run `npm install` inside
  `src/ui`. A wrong port or base URL in `--thesis-url`/`--harness-url` shows
  up as `unreachable` in the first two stages, not as an import error.
- **Screener 503 (`screener_unavailable`-style errors on `/v1/screeners/run`
  and the screener routes).** This is the designed fail-closed state when no
  screener data ports are configured: the local composition passes no
  `market_port`, so the screener surface refuses instead of fabricating
  results. Workspace data routes, chat, and backtest launch are unaffected.
- **Backtest launch 503 (`harness_unavailable`).** The Thesis process was
  started without a harness boundary: re-run the terminal 2 composition with
  `build_local_workspace_data(harness_runner=...)` and confirm
  `--config-path` (default `config/harness/backtest.yaml`) exists. A failing
  `harness backtest-job --config config/harness/backtest.yaml` run reports
  its own exit code in the launch acknowledgement.
- **UI shows fixture data instead of API data.** Real mode requires
  `PUBLIC_THESIS_API_BASE` at dev-server start; a persisted in-browser
  override (`localStorage['thesis.fixtures'] = 'true'`) or
  `PUBLIC_THESIS_UI_FIXTURES='true'` forces fixtures regardless. Restore real
  mode by removing the override (`localStorage.removeItem('thesis.fixtures')`)
  and restarting the dev server. Fixture mode is the sanctioned escape hatch
  while the API CORS layer is pending (see the browser cross-origin note in
  section 3).
- **Provider preflight failures.** OpenRouter `authentication_unavailable`
  means `OPENROUTER_API_KEY` is missing or rejected (401/403);
  `provider_unreachable` is network/DNS. Foundry `authentication_unavailable`
  means the Entra credential failed (`az login` again); `model_unavailable`
  means the endpoint/project/deployment names in `config/thesis/runtime.yaml`
  do not resolve to an enabled deployment. Never paste provider keys into
  this runbook, the UI, or any repository file.
