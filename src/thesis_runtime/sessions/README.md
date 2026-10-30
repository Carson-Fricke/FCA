# thesis_runtime/sessions

The provider-independent model and tool loop. It consumes neutral provider and persistence ports; application composition selects native provider adapters.

- `__init__.py`: sessions capability package.
- `session.py`: Unified durable model/tool loop; steering and recoverable provider failures preserve received partial output without ACK or tool dispatch. Transient provider failures use a fresh audited request after bounded backoff; completed tool receipts and job identities are retained. Three consecutive rounds repeating the same argument-schema failures interrupt the execution slice; the conversation service schedules continued recovery. Iteration exhaustion yields a new slice, never ending the agent lifetime. Normal model responses return `waiting` while durable requests or spawned-child handles lack returns. Existing incoming deliveries resume execution without wait tools, polling, extra pending-work storage or automatic continuations. New arrivals during the response are consumed at another model boundary before the turn can finish. New operator input or execution progress resets the repair budget.
- `recovery.py`: Three cooperative recovery delays (1, 2, 4 seconds), a 60-second maximum provider-requested wait, and cancellation/steering/ownership checks during backoff. It never invokes models, dispatches tools, or schedules another worker.

- `model_invocation.py`: Neutral streaming provider and invocation persistence ports; audited model lifecycle, stable output identity, fenced bounded progress batching, terminal publication, and cooperative interruption with received reasoning/text retained as canonical evidence. A committed failed audit is required before provider recovery; persistence faults escape instead of being retried. Buffered fragments are removed only after successful publication.

- `compaction.py`: Pure request-size estimation, consumed-prefix selection and complete evidence-group retention; outstanding calls can cross a checkpoint because their durable records remain authoritative. Installs no effects and never filters the summarizer's source.
- `context_manager.py`: Context usage notices, the intrinsic `compact_context` control tool, audited same-model summarization with dedicated instructions and a bounded, tool-disabled transcript request, optional audited evidence selection and checkpoint installation through injected ports. Rejected responses remain non-executable invocation audit evidence with a precise validation reason. Pending inputs are never ACKed by compaction. Successful compaction and newly delivered notices return to reconciliation before another model request.

Every normal request contains current configured instructions, the last committed
summary and selected verbatim evidence, and every canonical message after its
covered boundary. There is no message-count history cap. Model summaries are
bounded; the original transcript remains durable. Capacity estimates include the
projected tool catalog and opaque continuation and reserve reply/safety space.
The model can request an earlier checkpoint; runtime capacity checks provide a
backstop. Semantic task preservation belongs to the summary model's instructions,
not domain-specific runtime field extraction. Irreducible context fails visibly
without truncation or an automatic retry loop.
