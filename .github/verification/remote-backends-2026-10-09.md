# Remote backend repair verification — 2026-10-09

## Scope and source

The operator authorized remote backend prerequisites 1–5: deployment repair,
Thesis-to-harness MCP wiring, reuse of the local model credential without
disclosing it, durable Gitea workspaces, and authenticated workstation access.
Changes were pushed as `0da5b45`, `0e23515`, `1a24a78`, `f5ea800`, and
`fe8d663`, `62e7786`, `6e12dfa`, `87804ba`, `6b9adae`, and `2e71dfd`.

The live baseline was `7e8fe7d`. Verification used Azure deployments rather
than local fixtures. Separate concurrent harness/UI edits in the workstation
were excluded from these commits and their deployment claims.

## Final status

All five authorized setup items are deployed and verified. Final
[main run 38017776041](https://github.com/Carson-Fricke/FCA/actions/runs/38017776041)
passed all four jobs, including the real bounded backtest.
[Thesis run 38012804702](https://github.com/Carson-Fricke/FCA/actions/runs/38012804702)
passed its deployment and runtime smoke. The unchanged Rust/Python source
passed all four jobs in
[CI run 38016929058](https://github.com/Carson-Fricke/FCA/actions/runs/38016929058).
The final revision changes only the backtest resource allocation; its
[CI run 38017775871](https://github.com/Carson-Fricke/FCA/actions/runs/38017775871)
has passed Python and Bicep, with repeat Rust jobs still running at handoff.

The normal workflow execution `fca-ml-dev-backtest-lscscm9` succeeded at
02:45:50 UTC with 4,708,592 events, 2 orders, 15 fills, and identical manifest,
artifact, and outcome digests to the independent successful verification.
Final health checks returned HTTP 200 for lake, harness, and Thesis; anonymous
auth checks returned 401 for all three, and the authenticated loopback proxy
returned 200. Deployed Thesis rediscovered all 12 harness MCP tools after the
final main deployment. The running workstation UI is at
`http://127.0.0.1:5174`; restart with
`uv run --extra dev python scripts/local/run_remote_ui.py`.

## Repairs

- The shared runtime image includes the sibling Rust contracts crate required
  to fingerprint and compile authored strategies at runtime.
- Thesis uses the authenticated HTTPS harness endpoint; the harness receives
  the market-lake endpoint and its bearer through infrastructure composition.
- Unsupported per-app `allLogs` diagnostics were removed; centralized
  environment logging remains configured.
- Deployment workflows retain all pending runs in the shared deployment
  concurrency group instead of cancelling each other's pending deployment.
- The local OpenRouter credential was copied into Key Vault using captured
  process memory and a secure API request. Local/vault equality was verified
  without printing either value. The running model uses a Key Vault reference.
- Internal Gitea uses Azure Files for repositories and PostgreSQL 16 for its
  database. SQLite on SMB failed with `database is locked` and was replaced.
  PostgreSQL uses verified TLS, password authentication, Azure-service firewall
  access, and seven-day backups. Repository/configuration recovery remains an
  operator backup responsibility.
- A private `thesis-workspaces` organization and scoped service token were
  provisioned. Operator copies of the token and generated passwords are retained
  in Key Vault; the database password is also delivered through an ACA secret
  for Gitea's database connection.
- `scripts/local/run_remote_ui.py` resolves the remote endpoint and API bearer
  privately and starts the loopback UI proxy. No bearer is embedded in the
  browser bundle. This is workstation access, not a public multi-user UI host.

## Completed behavior checks

- Azure lake, harness, and Thesis `/health` endpoints returned HTTP 200.
- Unauthenticated `/v1/auth/check` returned HTTP 401 on all three APIs.
- The loopback proxy authenticated successfully against remote Thesis.
- Thesis reached authenticated harness `/mcp/health` with HTTP 200.
- A real MCP composition initialized from inside the deployed Thesis container
  and discovered 12 harness tools.
- The configured model answered `REMOTE_OK` through remote Thesis in chat
  `chat-3819ec4c0e964753bfb6a72054ca6a58` in `equity-momentum`.
- After the corrected Thesis deployment, the same chat recovered its two
  earlier messages and answered a fresh request with `REMOTE_AFTER_DEPLOY`;
  the persisted message count became four.
- The browser loaded that remote workspace and displayed the model reply.
- A fresh independent Gitea workspace checkout recovered the initialized
  `equity-momentum` manifest from its private repository.
- The deployed SDK source fingerprint matched the configured bundle hash.
- Temporary Log Analytics public query access used for diagnosis was restored
  to `Disabled`.

## Local integration verification

- `uv lock --check` — passed.
- `uv run --extra dev ruff check .` — passed.
- `uv run --extra dev ruff format --check .` — passed, 558 files formatted.
- `uv run --extra dev ty check` — passed.
- `uv run --extra dev pytest -p no:cacheprovider` — 1618 passed, 1 skipped,
  1 warning in 165.44 seconds. Includes the mandatory delivery architecture
  and named session behavior suites. The opt-in live Cosmos latency check was
  skipped. This run preceded the separate concurrent workstation edits.
- Bicep build/lint and shell syntax validation for 22 workflow steps passed;
  existing unrelated Bicep nullable/unused warnings remain.

## Deployment results

Baseline main run `38006839254` built and deployed successfully. Its Azure
backtest execution `fca-ml-dev-backtest-3efi8cj` reproduced the exact missing
`/opt/rust/sdk/strategy-sdk/../contracts/Cargo.toml` failure. Once deployment
and image retention were complete, the superseded workflow and its bounded
job retries were stopped so repaired deployments could proceed.

Thesis run `38009424550` built and deployed successfully. Its smoke failed
an obsolete assertion requiring exactly one secret, although the enabled
backend correctly had four Key Vault references and no inline values.
Commit `f5ea800` replaces the assertion with an exact configured-name check
and requires every secret to have an HTTPS Key Vault URL, managed identity,
and no value field. YAML and all 16 Thesis shell steps passed syntax checks.

Final results are recorded above; the following entries retain the repair
sequence and evidence from superseded deployments.
Corrected [Thesis run 38012804702](https://github.com/Carson-Fricke/FCA/actions/runs/38012804702)
passed image build, Azure deployment, runtime smoke (configured Key Vault
references, configured workspace storage, health/auth posture), and the
public-UI job. The public host itself remains unconfigured, so that job
reported its explicit skip and did not publish a Static Web App.
Main run `38009425089` built and deployed successfully. Backtest execution
`fca-ml-dev-backtest-wep5n1f` passed SDK initialization but failed because the
lake acknowledged its ensure job while all three requested MSFT quote sessions
remained missing. The published `lake api` CLI was constructing a separate
in-memory router instead of the existing hosted composition. Commit `fe8d663`
removes that router and delegates to `lake-hosted::api::create_api_router`.
`cargo test -p lake-cli --test api_composition` passed both behavior checks;
`cargo fmt -p lake-cli -- --check` and
`cargo clippy -p lake-cli --all-targets -- -D warnings` passed.

Thesis run `38011824073` passed the corrected credential assertion, then
failed storage discovery: it searched for `product=thesis`, although the root
composition tags these resources `product=market-lake`. Commit `62e7786`
reads `THESIS_STORAGE_ACCOUNT_NAME` from the deployed app instead. The exact
JMESPath lookup returned `thesisobj3kwwfxxfxu`; YAML and 16 shell steps passed
syntax checks. The superseded `fe8d663` Thesis run was cancelled before its
deployment began, and its redundant CI run was cancelled in favor of latest CI.

Main run `38012694346` built and deployed the hosted API successfully.
Backtest execution `fca-ml-dev-backtest-8arjkl7` then exposed the next actual
Azure boundary: the pinned `azure_identity` 0.21 App Service credential requests
only the system identity. It ignores `AZURE_CLIENT_ID`, although this app has
only the user-assigned runtime identity. Direct token probes from the live
container returned HTTP 400 without `client_id` and HTTP 200 with the configured
client ID; token bodies and identity headers were never displayed.
The live container also read a page from the Azure coverage table using
explicit user-assigned identity selection, confirming table permissions.
Commit `6e12dfa` selects that client in the platform identity endpoint in a
short process launcher, then executes the native API. Bicep build passed with
existing warnings. This follows the documented
[ACA identity selection protocol](https://learn.microsoft.com/en-us/azure/container-apps/managed-identity).

The repair commit's CI run `38008569234` and credential-smoke CI run
`38011824052` passed. The newest deployment runs are main `38015102618` and
successful Thesis `38012804702`, with CI `38015102358`. The redundant
`38015102352` Thesis run (triggered by lake-only comment/README changes) was
cancelled during image build before deployment. Main owns application of the
lake startup change; functional Thesis source is unchanged from the green run.
[CI run 38012804635](https://github.com/Carson-Fricke/FCA/actions/runs/38012804635)
passed Python quality/tests/package, harness Rust, market-lake Rust (including
the new CLI composition behavior tests), and Bicep validation.
[CI run 38009424535](https://github.com/Carson-Fricke/FCA/actions/runs/38009424535)
passed all four jobs: Python quality/tests/package, harness Rust, market-lake
Rust, and Bicep validation.

Main `38015102618` built and deployed revision `0000161`. Authenticated native
coverage now returned HTTP 200, proving the identity selection repair. During
the real MSFT ensure request its process was killed with exit 137 at
02:11:54 UTC. The limit was 512 MiB; no requested session coverage had been
committed. The persisted ensure ID remained `running`. After confirming the
owning process was gone and stopping the failed backtest retries, the operator
reconciliation marked that exact row `failed` with an ETag condition and the
termination evidence, retaining its ID and creation time. No unknown-outcome
execution was blindly replayed or replaced.

Commit `87804ba` gives the hosted lake 2 CPUs and 4 GiB for full-session SIP
ingestion. Bicep build passed with existing warnings. Latest CI `38015102358`
passed all four jobs. The resource repair's main run is `38016671774` and CI is
`38016671584`; the independent bounded verification execution is
`fca-ml-dev-backtest-iziw808`.

The independent execution and workflow backtest `fca-ml-dev-backtest-i4xrxyy`
both had containers terminated with exit 137 under the backtest job's 1 GiB
allocation, before producing results. Their retries were stopped. Commit
`6b9adae` increases the backtest job to 2 CPUs and 4 GiB as well. Superseded main
`38016671774` was cancelled after its successful image/deployment/retention
steps so latest main `38016929190` could verify the complete resource repair.
Latest CI is `38016929058`. Bicep build passed with existing warnings.
The loopback UI, authenticated Thesis probe, and recovered verification chat
were rechecked after these deployments and returned HTTP 200.

Main `38016929190` passed build/deployment but its 4 GiB backtest
`fca-ml-dev-backtest-dwf7wmb` was killed with exit 137 on both attempts. An
execution-only diagnostic wrapper isolated memory use without logging any
credentials: JIT compilation stayed below 512 MiB; replay data loading crossed
4 GiB. The unchanged three-session workload was rerun with 4 CPUs and 8 GiB
as `fca-ml-dev-backtest-67xu7m4`. It completed successfully at 02:39:26 UTC:
4,708,592 market events, 2 orders, 15 fills, backtest artifacts, and exit 0.
Peak cgroup memory was 8,083,881,984 bytes (about 7.53 GiB); no memory-limit
failures were observed. The temporary wrapper did not alter the job template.
Commit `2e71dfd` persists the proven allocation. Its main run is `38017776041`
and CI is `38017775871`.

Authenticated native lake coverage returned HTTP 200 with complete MSFT quote
partitions for January 2–4, 2024: 1,254,107; 1,336,280; and 982,191 rows
(3,572,578 quotes total). Real provider ingestion and Azure catalog/Parquet
persistence are therefore verified independently of health probes.

## Separate observed migration gap

The scheduled ingestion job still invokes `lake ingest`, but the native CLI
exposes top-level `api` and `local` commands only. This scheduled-job repair
is separate from the five authorized setup items; on-demand hosted API
ingestion is the path exercised by the deployment backtest smoke. Do not
interpret successful API/model/MCP checks as proof that the nightly job works.
