# SDK ABI 8 live activation — 2026-10-28

## Rebuild and restart

Rebuilt the optimized harness API and execution worker and restarted both the
harness and Thesis through `scripts/local/start-stack.ps1`, selecting
`harness,thesis`. The launcher also refreshed its local ingestion worker.
The detached worker used the Visual Studio `vcvars64.bat` environment and
the repository-pinned Rust 1.98.1 toolchain. Its build commands were:

```powershell
cargo +1.98.1 build --release --locked --jobs 2 --manifest-path src/harness/Cargo.toml -p api --bin harness --bin harness-worker
cargo +1.98.1 build --release --locked --jobs 2 --manifest-path src/lake/Cargo.toml -p lake-ingest-worker --bin lake-ingest-worker
```

The restart completed at 17:25:40 PDT. Final health verification at
17:33:09 PDT (2026-10-29T00:33:09Z) confirmed:

| Service | Port | Previous PID | Current PID | Health | Authenticated check |
| --- | --- | --- | --- | --- | --- |
| Harness | 8001 | 16688 | 38896 | 200 / ok | 200 / ok |
| Thesis | 8000 | 33440 | 20680 | 200 / ok | 200 / ok |

Both services reject unauthenticated `/v1/auth/check` requests with 401.
The existing UI on port 5173 responds with 200. Thesis connected to the local
harness MCP endpoint and loaded its 12-tool catalog.

Release executable SHA-256 identities:

- `harness.exe`: `b9665cd917a5d564e87a69420788b72e47c2efdae2ebe432ca5950010f0ba4f2`
- `harness-worker.exe`: `758e75a0ad3e65d2f1459e12d48dc0e7424e9080c97722604d4e6a9eae8f2d08`

## Live verification

The serving MCP initialization and SDK reference report ABI **8** and SDK
identity `a12111975d83b96fb3aa1e861ab12050ec567660e0e0f50ef000f46fa36dd87e`.
The current causal reference exposes `DecisionContext::capture`. The rebuilt
worker compiled and loaded the ABI 8 authored probe both directly and through
the serving MCP `validate_study` call. Metadata alone was not used as proof.

Seven executions used cached MSFT data for 2026-01-02, with one-minute bars,
a 60-second observation horizon, and a ten-minute replay beginning at
14:30:00Z. The prefix replay stopped after five minutes. Bars, trades and
quotes had complete local coverage; no provider ingestion was requested.
Each immutable configuration and job ID was durably retained before its one
submission. Every acknowledgement matched its retained ID.

| Probe | Job ID | Verified result |
| --- | --- | --- |
| Full study | `causal-activation-8e561e1ffc04467cac9379bbf3e92f8c` | Succeeded: 10 captures, 9 observed outcomes, 1 end-of-run null outcome |
| Prefix study | `causal-activation-aabeda876f134d558add7ce7d06df80d` | Succeeded: 5 captures, 4 observed outcomes, 1 end-of-run null outcome |
| Strategy backtest | `causal-activation-596753b586cd44ca95663d5f99bb42ed` | Succeeded: 10 captures, 9 observed outcomes, 1 end-of-run null outcome |
| Duplicate capture | `causal-activation-9be45832381747c29543e0daa46f3fbd` | Failed as required: duplicate dataset/symbol capture in one frame |
| Schema rewrite | `causal-activation-8fd30e9722ca49019e6a9bd1ac75ef29` | Failed as required: feature schema and horizon are immutable |
| Forged causal output | `causal-activation-2863037c10b34983864458d6502d25bc` | Failed as required: only the host may publish causal observations |
| Reserved namespace | `causal-activation-0aa5a09af3274437a6f091acc1850c1b` | Failed as required: `causal/` is reserved for host-sealed observations |

The successful outputs verified host observation identities, decision times,
exact horizon arithmetic and close-to-close return labels. Observed labels
were available exactly at their due time; unresolved final rows retained null
returns. Stop-time mutation to 999999 did not change captured features.
Matching prefix and full-run observations retained identical identities,
features, reference prices and decision/due times. Strategy captures matched
the study path. Ignoring SDK capture/publication errors did not bypass any of
the four negative checks.

The restarted Thesis API returned `harness_replay` authority for sealed
observations and `authored` for the retrospective table. Its inspector paged
the nested causal dataset successfully for both study and backtest outputs.
The complete live assertion run exited successfully at 17:32:19 PDT.

## Operational caveat and evidence

Thesis startup/recovery rejects existing outdated workflow records, including
`hypothesis-development@5.0.0`, `research-investigation@2.0.0`, and histories
missing explicit activation, route and output. These recovery errors recur in
the Thesis error log. Both APIs and the new execution/inspection paths above
remain healthy. No old workflow/conversation data was deleted or converted.

The causal guarantee applies to the supported replay/capture path. Native
authored code, external future knowledge and input-data availability remain
outside this boundary, as documented in the implementation verification.
This activation task did not repeat repository-wide source verification or
change production code.

Detailed evidence is retained locally under
`.local/reviews/causal-activation-2026-10-28/`: `summary.json`, `plan.json`,
acknowledgements, terminal job records, exploratory artifacts, Thesis catalogs
and pages, SDK references, `validation.json`, `system-health.json`, probe
sources and `live-probe.log`. The restart transcript is
`.local/logs/causal-activation-20261028-165902.log`; Thesis recovery diagnostics
are in `.local/logs/thesis-20261028-165907.err.log`. Credentials were excluded
from the evidence.
