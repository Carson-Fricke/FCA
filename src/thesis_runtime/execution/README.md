# thesis_runtime/execution

Runtime execution events, durable runnable references and renewable fenced workers. It must not select FCA workflow implementations.

- `__init__.py`: Durable Thesis workflow execution, events, checkpoints, and recovery.
- `checkpoints.py`: Versioned, dependency-injected workflow checkpoints.
- `events.py`: Ordered, durable execution-event contracts and replay semantics.
- `queue.py`: Local durable workflow execution projection with fencing and recovery.
- `recovery.py`: Restart orchestration for the local durable execution projection.
- `sub_agents.py`: Sub-agent event contracts and the event-log projection of a sub-agent tree.
- `worker.py`: Durable runnable references, notification-driven dispatch, retained busy work, and leased execution. Discovery, bounded retained-work scans and bounded workflow repair run independently of dispatch, with at most one of each in flight; failures retain work and back off. No model or workflow policy.
- `workflow_events.py`: Workflow-scoped event vocabulary and appenders (REQ-C2).

Checkpoint storage requires the complete current projection. New empty stores create that projection; existing incomplete files are rejected.
