"""Cosmos leases and transactional write fences in the workspace catalog."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from azure.core import MatchConditions
from azure.core.exceptions import HttpResponseError
from azure.cosmos import ContainerProxy
from azure.cosmos.exceptions import (
    CosmosAccessConditionFailedError,
    CosmosResourceExistsError,
    CosmosResourceNotFoundError,
)

from thesis_runtime.storage.ownership import Claim, OwnershipLost, held_claims


def lease_id(resource: str) -> str:
    return "Ownership:" + hashlib.sha256(resource.encode()).hexdigest()


class CosmosOwnershipStore:
    """ETag admission, renewable expiry, and monotonically increasing fences."""

    def __init__(self, container: ContainerProxy) -> None:
        self.container = container

    def _read(self, workspace_id: str, resource: str) -> dict[str, Any] | None:
        try:
            return dict(
                self.container.read_item(
                    item=lease_id(resource), partition_key=workspace_id
                )
            )
        except CosmosResourceNotFoundError:
            return None

    def acquire(self, workspace_id: str, resource: str, seconds: float) -> Claim | None:
        previous = self._read(workspace_id, resource)
        now = datetime.now(UTC).timestamp()
        if previous is not None and previous["expiresAt"] > now:
            return None
        fence = 1 if previous is None else int(previous["fence"]) + 1
        token = uuid4().hex
        document = {
            "id": lease_id(resource),
            "workspaceId": workspace_id,
            "recordType": "Ownership",
            "resource": resource,
            "token": token,
            "fence": fence,
            "expiresAt": now + seconds,
        }
        try:
            if previous is None:
                self.container.create_item(document)
            else:
                self.container.replace_item(
                    item=document["id"],
                    body=document,
                    etag=previous["_etag"],
                    match_condition=MatchConditions.IfNotModified,
                )
        except (CosmosResourceExistsError, CosmosAccessConditionFailedError):
            return None
        return Claim(workspace_id, resource, token, fence)

    def checked(self, claim: Claim) -> dict[str, Any]:
        document = self._read(claim.workspace_id, claim.resource)
        if (
            document is None
            or document["token"] != claim.token
            or document["fence"] != claim.fence
            or document["expiresAt"] <= datetime.now(UTC).timestamp()
        ):
            raise OwnershipLost(f"stale ownership: {claim.resource}")
        return document

    def check(self, claim: Claim) -> None:
        self.checked(claim)

    def renew(self, claim: Claim, seconds: float) -> None:
        for _ in range(16):
            document = self.checked(claim)
            document["expiresAt"] = datetime.now(UTC).timestamp() + seconds
            try:
                self.container.replace_item(
                    item=document["id"],
                    body=document,
                    etag=document["_etag"],
                    match_condition=MatchConditions.IfNotModified,
                )
            except CosmosAccessConditionFailedError:
                continue
            return
        raise OwnershipLost("ownership renewal remained contended")

    def release(self, claim: Claim) -> None:
        for _ in range(16):
            document = self.checked(claim)
            document["expiresAt"] = 0
            try:
                self.container.replace_item(
                    item=document["id"],
                    body=document,
                    etag=document["_etag"],
                    match_condition=MatchConditions.IfNotModified,
                )
            except CosmosAccessConditionFailedError:
                continue
            return
        raise OwnershipLost("ownership release remained contended")


def fence_operations(container: ContainerProxy, workspace_id: str) -> list[Any]:
    """CAS every enclosing owner in the same transaction as its published writes."""
    operations = []
    for held in held_claims():
        if held.lost.is_set():
            raise OwnershipLost("owner heartbeat failed")
        store = held.store
        if (
            not isinstance(store, CosmosOwnershipStore)
            or store.container is not container
            or held.claim.workspace_id != workspace_id
        ):
            raise OwnershipLost("durable mutation crossed its ownership partition")
        document = store.checked(held.claim)
        operations.append(
            (
                "replace",
                (document["id"], document),
                {"if_match_etag": document["_etag"]},
            )
        )
    return operations


def fenced_batch(
    container: ContainerProxy, workspace_id: str, operations: list[Any]
) -> list[Any]:
    """Retry fence-heartbeat conflicts; never accept a stale attempt's mutation."""
    for _ in range(32):
        fences = fence_operations(container, workspace_id)
        # A delete on another replica must race atomically with every child
        # write, including events and workflow execution documents.
        chat_ids = {
            op[1][0 if op[0] in {"create", "upsert"} else 1].get("chatId")
            for op in operations
            if op[0] in {"create", "upsert", "replace"}
        }
        written_ids = {
            op[1][0]["id"] if op[0] in {"create", "upsert"} else op[1][0]
            for op in operations
        }
        for chat_id in chat_ids - {None}:
            root_id = "ChatThread:" + str(chat_id)
            if root_id in written_ids:
                continue
            try:
                root = dict(
                    container.read_item(item=root_id, partition_key=workspace_id)
                )
            except CosmosResourceNotFoundError:
                continue
            from thesis_runtime.adapters.cosmos.chunks import ChunkedPayloadStore
            from thesis_runtime.contracts.records import ChatContractError

            if (
                ChunkedPayloadStore(container)
                .load(root["payload"], workspace_id)
                .get("state")
                == "deleted"
            ):
                raise ChatContractError("conversation has been deleted")
            fences.append(
                ("replace", (root_id, root), {"if_match_etag": root["_etag"]})
            )
        try:
            # Every writer must acquire shared document locks in the same order.
            # Admission supplies its own root replacement whereas progress adds
            # one as a fence; prepending only the latter inverted root/counter
            # locking and deadlocked concurrent message and output commits.
            ordered = sorted(
                [(operation, True) for operation in fences]
                + [(operation, False) for operation in operations],
                key=lambda entry: (
                    entry[0][1][0]["id"]
                    if entry[0][0] in {"create", "upsert"}
                    else entry[0][1][0]
                ),
            )
            fence_indexes = {
                index for index, (_, is_fence) in enumerate(ordered) if is_fence
            }
            return container.execute_item_batch(
                # The Cosmos SDK pops conditional options while serializing.
                # Give each attempt fresh dictionaries so a fence retry cannot
                # silently discard the caller's counter/data CAS conditions.
                batch_operations=[
                    (operation[0], operation[1], dict(operation[2]))
                    if len(operation) > 2
                    else operation
                    for operation, _ in ordered
                ],
                partition_key=workspace_id,
            )
        except HttpResponseError as error:
            if error.status_code != 412 or not fences:
                raise
            # A failed data CAS is still a conflict. Retrying its unchanged ETag
            # cannot commit; the caller must reread its domain state.
            if getattr(error, "error_index", 0) not in fence_indexes:
                raise
    raise OwnershipLost("fenced commit remained contended")
