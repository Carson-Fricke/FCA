# Conversation records and lifecycle

This package owns conversation records, scoped persistence ports, and chat
application behavior. Workspace artifact bytes and model execution do not belong
here. Authorization retains workspace identity; record retrieval uses immediate
and root conversation ownership.

| File | Purpose |
| --- | --- |
| `__init__.py` | Public contracts, services, and persistence facades. |
| `contracts.py` | Immutable records, conversation ownership, lifecycle enums, hashes, and legacy ownership defaults. |
| `service.py` | Intake, scoped reads, workflow lifecycle, root logical deletion, and one bounded cleanup worker. |
| `persistence.py` | Canonical record decoding, ownership locks, and filesystem adapter. |
| `azure.py` | Blob adapter with root-indexed new records and legacy path reads. |
| `responder.py` | Provider-neutral reply chunks and reply transport protocols. |
| `streaming.py` | Ephemeral reply broker, not another durable inbox. |
| `workflow_launcher.py` | Registered workflow launch composition. |

Deletion retains a durable root tombstone to fence late writes and restart
recovery. Catalog descendants and timeline records are physically reclaimed in
bounded background batches. Published workspace artifacts are never reclaimed
by conversation cleanup. Blob/filesystem legacy enumeration is not covered by
the Cosmos latency acceptance.
