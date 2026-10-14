# thesis_runtime/conversations

Conversation handles, canonical incoming delivery, inbox ordering, durable records and streaming. This slice must not contain FCA research or workspace operations.

- `__init__.py`: Public workspace conversation and workflow contracts.
- `admission.py`: Shared identity, cursor assignment, chat bookkeeping, and admission ordering rules for storage adapters.
- `conversation.py`: Anchored transcripts, committed-record delivery receipts, and serialized turns with bounded history and incremental input reads. Record-only anchors retain incoming evidence without admitting runnable work; turn execution also rejects their queued wakes. Active generation checks the durable scoped head for operator steering, including input accepted by another replica. Unexpected root faults are logged, persisted as interruptions when storage is available, and propagated to the hosting worker.
- `delivery.py`: One delivery path for datums and answers addressed to a conversation anchor.
- `inbox.py`: Canonical delivery and success-bound consumption for one live session.
- `incoming.py`: The singular storage boundary for canonical incoming session messages.
- `service.py`: Workspace chat and workflow lifecycle commands, an injected workspace deletion fence, and all-root conversation cleanup; storage owns atomic message admission, conditional root updates, and ordered input reads without intake leases.
- `streaming.py`: Local commit notification and subscription lifecycle.
- `timeline.py`: Database-independent timeline read/write and notification ports.
- `turns.py`: Serialize and coalesce the turns of one conversation.
