# thesis/persistence

FCA investigation storage and retained harness-submission identity. Generic runtime persistence and ownership implementations live in thesis_runtime.

- `__init__.py`: Durable workspace journaling and fenced Git publication.
- `cosmos_submissions.py`: Persists retained FCA harness submissions in their domain-owned Cosmos container.
- `git.py`: Process-safe, compare-and-swap fenced Git publication.
- `investigations.py`: investigation decoding and selected research-store composition, including the Cosmos adapter.
- `postgres.py`: native PostgreSQL investigation and immutable submission stores, using the runtime's shared pool and ownership fences.
- `stack.py`: selects the FCA submission adapter using the configured runtime backend.
- `journal.py`: Append-only workspace command journal with a rebuildable publication outbox.
- `reconcile.py`: Reconciliation of accepted workspace state, Git history, and queue state.
- `submissions.py`: Immutable submission references; this module neither dispatches nor polls.

Retained agent submissions include the trusted conversation recipient before
dispatch. Completion intake resolves a globally unique job ID through the selected
submission adapter; ambiguous IDs fail closed. No submission reader polls jobs.
