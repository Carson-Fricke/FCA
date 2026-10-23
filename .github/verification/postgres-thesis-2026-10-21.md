# Local PostgreSQL, bounded recovery and selective history

## Delivered scope

Local Thesis now uses native PostgreSQL 17 on loopback port 5433. The shared
psycopg pool implements workspace transactions, canonical admission/counters/work,
ownership fences, execution revision CAS, durable timeline/events, invocation
audits, investigations and retained harness submissions. Azure harness selection
remains remote and was verified reachable. Azure Thesis retains its Cosmos adapter.

Discovery, retained-work reconciliation and workflow recovery run independently
of dispatch, with one of each background operation in flight. Work scans return
64 references per batch; workflow repair inspects eight identities per pass.
Hydration follows selection/ownership. Full batches continue promptly and completed
sweeps back off. Unknown running attempts are interrupted and reconciled, not replayed.

The UI initially loads a 200-record selected-thread page, starts the same canonical
SSE cursor, and loads older pages explicitly. Children load when opened. Current
workflow metadata and node trees are independent of transcripts. PostgreSQL history
uses canonical completed messages instead of their provisional fragments. Open
output prefixes retain byte-offset continuity without moving history cursors.

## Preserved data

The quiesced emulator snapshot contained 84,514 catalog documents, including 6,501
immutable payload chunks, and 129 submission documents. The target contains all
78,013 hydrated non-chunk catalog documents and 129 submissions. Every imported
document was compared against its source after hydration. The normal Cosmos API
snapshot independently matched 48 records. Job IDs, canonical content, invocation
audits, counters and consumption positions were preserved; no submission was replayed.

The emulator SQL exporter adds `_propertyOrder` metadata not returned by its API.
The offline copy removed that export metadata before comparison/import. It did not
add a compatibility reader or infer historical canonical messages. Temporary
conversion code was removed. Compressed source snapshots and the stopped emulator
data volume remain local; credentials remain in ignored environment files.

Current-contract validation passed for 7,099 invocations, 7,436 messages, 46 roots,
112 subagents, 105 tool jobs and 24 workflow executions. Seventeen older
`local-stack` smoke messages lack required references; they remain preserved and
rejected by the current reader. Existing executions requesting retired workflow
versions still produce explicit recovery diagnostics; this task does not rewrite
their workflow history or replay them under another version.

## Verification

- Final full Python suite: **1,695 passed, 1 opt-in live Cosmos test skipped** in
  **166.75 seconds**, including the mandatory delivery architecture guard and
  required delivery, audit, acknowledgement and interruption behavior suites.
- Five real PostgreSQL HTTP behaviors verify replica replacement/idempotency,
  exact audit retention, atomic rollback, stale-owner fencing, open-output history,
  lightweight workflow metadata and execution revision conflicts. Tests use one
  disposable database; CI supplies a PostgreSQL service, local runs can use Docker.
- Repository lock check, Ruff lint/format and ty: passed.
- UI check: zero errors; two existing CSS compatibility warnings. The complete UI
  run passed 284 tests with four failures from superseded transport/paging test
  expectations. After updating those expectations, all affected suites passed
  focused reruns; 288 UI tests are verified passing, with one existing opt-in skip.
- Browser history-loading and workflow-hierarchy flows passed. The live retained
  T5 root and selected discovery thread rendered successfully. PowerShell startup
  and the dedicated PostgreSQL launcher were exercised; workflow YAML parsed and
  the six CI change-selection checks passed.

## Live read latency

Three warm HTTP measurements against the retained data, in milliseconds:

| Read | Samples |
| --- | --- |
| Workspace list | 731.84, 725.64, 742.56 |
| T7 conversation list | 5.14, 4.13, 3.94 |
| T5 recent root history | 15.69, 15.34, 16.02 |
| T5 selected discovery history | 33.89, 31.99, 32.20 |

The earlier emulator root-history measurement was approximately ten seconds.
These numbers measure local HTTP reads, not external model inference. Local
evidence is retained under `.local/verification/postgres-thesis-20261021/`.
