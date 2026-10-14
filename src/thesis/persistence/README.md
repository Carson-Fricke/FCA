# thesis/persistence

FCA investigation storage and retained harness-submission identity. Generic runtime persistence and ownership implementations live in thesis_runtime.

- `__init__.py`: Durable workspace journaling and fenced Git publication.
- `cosmos_submissions.py`: Persists retained FCA harness submissions in their domain-owned Cosmos container.
- `git.py`: Process-safe, compare-and-swap fenced Git publication.
- `investigations.py`: FCA-owned Cosmos persistence for research investigations.
- `journal.py`: Append-only workspace command journal with a rebuildable publication outbox.
- `reconcile.py`: Reconciliation of accepted workspace state, Git history, and queue state.
- `submissions.py`: Immutable submission references; this module neither dispatches nor polls.
