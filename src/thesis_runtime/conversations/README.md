# thesis_runtime/conversations

Conversation handles, canonical incoming delivery, inbox ordering, durable records and streaming. This slice must not contain FCA research or workspace operations.

- `__init__.py`: Public workspace conversation and workflow contracts.
- `admission.py`: Shared identity, cursor assignment, chat bookkeeping, and message ordering rules for storage adapters and session reads.
- `conversation.py`: Public service composition, canonical incoming admission, child lifecycle and wake routing. It creates an isolated attempt for each session turn; upstream callers keep using the service and its handles.
- `handle.py`: Public handle for one anchored transcript, user input, observation and turn execution; delegates effects to the service.
- `execution.py`: One bounded session attempt and its shared input state, durable reconciliation, steering, consumption acknowledgements and fault handling. Bootstrap instructions respect the durable completion mode; explicit completion never instructs the agent to finish through prose alone. It reloads the complete context since the committed checkpoint and atomically advances scoped selectors after durable summary output. A complete scoped transcript read derives pending call and child returns even before the checkpoint boundary. A waiting result bypasses task completion and automatic continuation; existing incoming delivery wakes the conversation. Its worker adapter remains internal to conversation composition.
- `output.py`: Outgoing model-envelope and runtime-tool activity persistence. It rejects incoming turns; runtime results return through the injected canonical incoming writer.
- `recovery.py`: Continuation timing and message construction, retry-streak rules and supervisor selection. Recovery notices reach only an explicit conversational return target; workflow record boundaries contain phase recovery. It neither writes nor schedules; the service delivers its messages through the canonical writer. Deliberate approval pauses remain distinct from automatic technical recovery.
- `delivery.py`: One delivery path for datums and answers addressed to a conversation anchor.
- `inbox.py`: Canonical delivery and success-bound consumption for one live session.
- `incoming.py`: The singular storage boundary for canonical incoming session messages, plus detached presentation-field preparation without admission authority.
- `service.py`: Workspace chat and workflow lifecycle commands, neutral retained tool-job reads, an injected workspace deletion fence, and all-root conversation cleanup; storage owns atomic message admission, conditional root updates, and ordered input/context reads without intake leases. `ContextCheckpoint` stores only the current covered boundary and canonical summary/evidence references; transcript records remain intact.
- `streaming.py`: Local commit notification and subscription lifecycle.
- `timeline.py`: Database-independent timeline read/write and notification ports, including scoped caller-selected receipt metadata reads.
- `turns.py`: Serialize and coalesce the turns of one conversation.

`timeline.py` also defines the optional output-prefix history read port. Open
provisional output can backfill its UTF-8 prefix without changing the selected
page cursor; it remains presentation evidence. `service.py` can list all chat
workflow metadata, including nested owners, independently of transcript history.
