"""Azure adapters for the provider-independent queue worker."""

from __future__ import annotations

import asyncio
from typing import Protocol, cast

from azure.core.credentials import TokenCredential
from azure.storage.queue import QueueClient

from lake.queue.worker import QueueMessage
from lake.storage.ports import QueueSender


class AzureQueueMessage(Protocol):
    id: str
    pop_receipt: str
    content: str
    dequeue_count: int


class AzureQueueTransport:
    def __init__(self, queue: QueueClient) -> None:
        self._queue = queue

    @classmethod
    def from_account_url(
        cls,
        account_url: str,
        queue_name: str,
        *,
        credential: TokenCredential,
    ) -> AzureQueueTransport:
        return cls(
            QueueClient(
                account_url=account_url,
                queue_name=queue_name,
                credential=credential,
            )
        )

    async def receive_one(
        self, *, visibility_timeout_seconds: int
    ) -> QueueMessage | None:
        def receive() -> object | None:
            pages = self._queue.receive_messages(
                visibility_timeout=visibility_timeout_seconds,
                messages_per_page=1,
            )
            return next(iter(pages), None)

        raw = await asyncio.to_thread(receive)
        return None if raw is None else _from_azure_message(raw)

    async def renew(
        self,
        message: QueueMessage,
        *,
        visibility_timeout_seconds: int,
    ) -> QueueMessage:
        raw = await asyncio.to_thread(
            self._queue.update_message,
            message=message.message_id,
            pop_receipt=message.pop_receipt,
            visibility_timeout=visibility_timeout_seconds,
        )
        return _from_azure_message(raw)

    async def delete(self, message: QueueMessage) -> None:
        await asyncio.to_thread(
            self._queue.delete_message,
            message=message.message_id,
            pop_receipt=message.pop_receipt,
        )


class AzureDeadLetterSink:
    def __init__(self, sender: QueueSender) -> None:
        self._sender = sender

    async def send(self, message: QueueMessage, *, error: str) -> None:
        await asyncio.to_thread(
            self._sender.send,
            {
                "message_id": message.message_id,
                "content": message.content,
                "dequeue_count": message.dequeue_count,
                "error": error,
            },
        )


def _from_azure_message(raw: object) -> QueueMessage:
    message = cast(AzureQueueMessage, raw)
    return QueueMessage(
        message_id=message.id,
        pop_receipt=message.pop_receipt,
        content=message.content,
        dequeue_count=message.dequeue_count,
    )


__all__ = ["AzureDeadLetterSink", "AzureQueueTransport"]
