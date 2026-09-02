"""Queue delivery and dead-letter facade."""

from lake.queue.adapters import AzureDeadLetterSink, AzureQueueTransport
from lake.queue.dead_letter import (
    DEAD_LETTER_SCHEMA_VERSION,
    DeadLetterEnvelope,
    DeadLetterFormatError,
    DeadLetterReplayer,
    DeadLetterStore,
    InMemoryDeadLetterStore,
    QueueDeadLetterSink,
    ReplayResult,
)
from lake.queue.worker import (
    AsyncioWorkerClock,
    DeadLetterSink,
    DeliveryOutcome,
    DeliveryResult,
    QueueDelivery,
    QueueHandler,
    QueueMessage,
    QueueTransport,
    QueueWorker,
    QueueWorkerPolicy,
    WorkerClock,
)

__all__ = [
    "DEAD_LETTER_SCHEMA_VERSION",
    "AsyncioWorkerClock",
    "AzureDeadLetterSink",
    "AzureQueueTransport",
    "DeadLetterEnvelope",
    "DeadLetterFormatError",
    "DeadLetterReplayer",
    "DeadLetterSink",
    "DeadLetterStore",
    "DeliveryOutcome",
    "DeliveryResult",
    "InMemoryDeadLetterStore",
    "QueueDeadLetterSink",
    "QueueDelivery",
    "QueueHandler",
    "QueueMessage",
    "QueueTransport",
    "QueueWorker",
    "QueueWorkerPolicy",
    "ReplayResult",
    "WorkerClock",
]
