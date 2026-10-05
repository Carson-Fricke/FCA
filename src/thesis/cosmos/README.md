# Cosmos conversation persistence

This package implements durable storage adapters. There is one authoritative
conversation store: the workspace-partitioned `catalog` container holds every
record, message, execution event, and per-chat sequence counter. Messages and
events carry the indexed `chatId`/`threadId`/`seq` fields that form the unified
per-chat timeline, so the timeline is a read view over the same documents, not a
second container. Agent scheduling and workspace artifact storage do not belong
here.

| File | Purpose |
| --- | --- |
| `__init__.py` | Cosmos storage facade. |
| `client.py` | Environment settings, client construction, and container provisioning. |
| `chunks.py` | Immutable content-addressed JSON leaves and hierarchical reference pages, bounded manifests, digest validation, and inline payload decoding. |
| `records.py` | The authoritative catalog adapter: scoped record queries, atomic message/event commits, root tombstones, and bounded conversation cleanup. |
| `timeline.py` | Sequence allocation and the ordered timeline read/shape facade over the catalog, with the post-commit wake channel. |
| `events.py` | Execution-event projection into the same catalog timeline with deletion fencing. |
| `execution_store.py` | Independent workflow execution and phase artifact storage, retained after chat deletion. |
| `stack.py` | Concrete chat/catalog/event composition. |

A message or event create commits the per-chat sequence counter CAS and the item
in one transactional batch in the workspace partition. A failed batch consumes
no sequence; concurrent writers retry conflicts. Messages and immutable
first-save execution snapshots are the same documents `get` and the timeline
read, so there is no projection outbox, repair scan, or cross-container
uncertainty. Timeline readers snapshot the in-process commit version before
reading durable items and wait for a version change; the database sequence
remains the only cursor, and a recovery interval bounds a lost wake. Legacy
catalog ownership remains readable through indexed top-level predicates.

The composed timeline uses the catalog chunk store. Point reads, history pages,
and execution-event replay hydrate stored manifests before returning payloads.
Event cursors come from the committed timeline sequence. Execution documents
keep their own chunk trees in the execution partition and preserve revision and
ETag concurrency checks.

Large provider audits are chunked separately from invocation lifecycle metadata,
so terminal updates reuse immutable audit content. New invocation request-message
entries are ordered identity/direction/position references; historical inline
full-message entries remain readable without reconstructing provider requests.
All chunks are durable before their manifest record is published. Missing or
corrupt chunks fail reads explicitly. Shared and orphaned chunks are retained;
per-record deletion must not delete content still referenced by another record.
Stack opening retains the existing `ensure_database` bootstrap in `client.py`;
chunk storage itself never provisions databases or containers.
