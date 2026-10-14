# thesis_runtime/sessions

The provider-independent model and tool loop. It consumes neutral provider and persistence ports; application composition selects native provider adapters.

- `__init__.py`: sessions capability package.
- `session.py`: Unified durable model/tool loop; steering and recoverable provider failures preserve received partial output without ACK or tool dispatch. Transient provider failures use a fresh audited request after bounded backoff; completed tool receipts and job identities are retained. Three consecutive rounds repeating the same argument-schema failures interrupt the turn. New operator input or execution progress resets this repair budget.
- `recovery.py`: Three cooperative recovery delays (1, 2, 4 seconds), a 60-second maximum provider-requested wait, and cancellation/steering/ownership checks during backoff. It never invokes models, dispatches tools, or schedules another worker.

- `model_invocation.py`: Neutral streaming provider and invocation persistence ports; audited model lifecycle, stable output identity, fenced bounded progress batching, terminal publication, and cooperative interruption with received reasoning/text retained as canonical evidence. A committed failed audit is required before provider recovery; persistence faults escape instead of being retried. Buffered fragments are removed only after successful publication.
