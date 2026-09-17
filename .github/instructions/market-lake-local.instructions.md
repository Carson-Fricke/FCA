---
applyTo: "src/lake/local/**/*.py,src/lake/cli/local.py,tests/lake/local/**/*.py,docs/local-market-lake.md,.env.example"
---

# Local Market-Lake Agent Instructions

Use `lake local` for workstation API, Alpaca ingest, local persistence, and
bounded-query work when Azure behavior is not under test. Do not substitute
fixtures for a supported manual ingest smoke, and do not use local results to
claim Azure identity, storage, queue, lease, or deployment validation.

## Safety

- Require Python 3.12 and install with `uv sync --python 3.12 --extra dev`.
- Read real Alpaca credentials only from `LAKE_PROVIDER__API_KEY` and
  `LAKE_PROVIDER__API_SECRET`. Never print, persist, hard-code, or commit them.
- Require `LAKE_API__BEARER_TOKEN` for the local API. Never include its value in
  logs or test snapshots.
- Keep the real smoke to a single closed session. The lake pages each fetch
  internally, so there is no record-count flag: callers request a symbol window
  and the lake decides page sizing. Automated tests must use injected fake
  providers and must never call Alpaca or Azure.
- Leave `LAKE_LOCAL_DATA_DIR` unset to use ignored `.local/market-lake`, or pass
  one dedicated empty path consistently. Never add the ownership marker by hand.

## Exact PowerShell Procedure

```powershell
uv sync --python 3.12 --extra dev
$env:LAKE_API__BEARER_TOKEN = "replace-with-a-random-local-token"
$env:LAKE_PROVIDER__API_KEY = "replace-with-real-alpaca-key-id"
$env:LAKE_PROVIDER__API_SECRET = "replace-with-real-alpaca-secret"
$lakeLocal = Start-Process uv -ArgumentList @("run", "lake", "local", "api", "--host", "127.0.0.1", "--port", "8000") -PassThru
Start-Sleep -Seconds 2
Invoke-RestMethod -Uri "http://127.0.0.1:8000/health"
$headers = @{ Authorization = "Bearer $env:LAKE_API__BEARER_TOKEN" }
Invoke-RestMethod -Uri "http://127.0.0.1:8000/v1/auth/check" -Headers $headers
uv run lake local ingest --symbol AAPL
& taskkill.exe /PID $lakeLocal.Id /T /F
Wait-Process -Id $lakeLocal.Id -ErrorAction SilentlyContinue
uv run lake local reset --yes
Remove-Item Env:LAKE_API__BEARER_TOKEN
Remove-Item Env:LAKE_PROVIDER__API_KEY
Remove-Item Env:LAKE_PROVIDER__API_SECRET
```

If a smoke command fails, stop the process before cleanup. Inspect
`.local/market-lake/state/catalog.json` and the command's credential-free error;
do not paste environment values into diagnostics.

## Exact POSIX Procedure

```sh
uv sync --python 3.12 --extra dev
export LAKE_API__BEARER_TOKEN='replace-with-a-random-local-token'
export LAKE_PROVIDER__API_KEY='replace-with-real-alpaca-key-id'
export LAKE_PROVIDER__API_SECRET='replace-with-real-alpaca-secret'
uv run lake local api --host 127.0.0.1 --port 8000 &
lake_local_pid=$!
sleep 2
curl --fail --silent --show-error http://127.0.0.1:8000/health
curl --fail --silent --show-error -H "Authorization: Bearer ${LAKE_API__BEARER_TOKEN}" http://127.0.0.1:8000/v1/auth/check
uv run lake local ingest --symbol AAPL
kill "${lake_local_pid}"
wait "${lake_local_pid}" || true
uv run lake local reset --yes
unset LAKE_API__BEARER_TOKEN LAKE_PROVIDER__API_KEY LAKE_PROVIDER__API_SECRET
```

For a failed smoke, run the `kill` and `wait` lines before reset. The detailed
data lifecycle, authenticated coverage request, and troubleshooting table are
in `docs/local-market-lake.md`.
