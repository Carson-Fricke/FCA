# PostgreSQL persistence

`__init__.py` marks the adapter package; composition is through `adapters/persistence.py`.

`store.py` owns the pool, indexed JSONB catalog, workspace transactions and
ownership fences. `records.py` admits canonical messages, updates root metadata,
advances inbox positions and publishes runnable work in one transaction.
`timeline.py` sequences the single log; `events.py` exposes execution replay.
`execution_store.py` retains workflow revision CAS and reads recovery identities
in keyset pages without hydrating launches or phase results. `ownership.py`
implements renewable leases. `work.py` selects bounded runnable references;
payloads are fetched only after ownership is claimed. `notifications.py` uses
transactional PostgreSQL notifications as hints, with durable reconciliation
after reconnect. `stack.py` composes these ports over the same store instance.

JSON payloads, including opaque provider request audits, are stored losslessly
inline; this adapter does not interpret Cosmos chunk manifests. Selection is
explicit through `THESIS_PERSISTENCE=postgres` and `THESIS_POSTGRES_DSN`.
