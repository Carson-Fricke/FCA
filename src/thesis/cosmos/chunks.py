"""Immutable, content-addressed JSON payload trees in an existing container."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Mapping
from typing import Any, cast

from azure.cosmos import ContainerProxy
from azure.cosmos.exceptions import (
    CosmosResourceExistsError,
    CosmosResourceNotFoundError,
)

INLINE_BYTES = 256 * 1024
CHUNK_BYTES = 192 * 1024
REFERENCE_FANOUT = 128
MAX_ITEM_BYTES = 1024 * 1024
MANIFEST_KEY = "_cosmosChunks"
CHUNK_TYPE = "ImmutablePayloadChunk"
_INDEX_FIELDS = (
    "chat_id",
    "root_conversation_id",
    "conversation_id",
    "parent_conversation_id",
    "sub_agent_id",
    "state",
    "updated_at",
)


def json_bytes(value: object) -> bytes:
    """Use deterministic UTF-8 JSON for content identity and size accounting."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def ensure_bounded_item(document: Mapping[str, object]) -> None:
    """Reserve ample room below Cosmos's 2 MiB item limit for SDK encoding."""
    # ASCII escaping is the worst-case wire representation for Unicode text.
    size = len(json.dumps(document, ensure_ascii=True).encode("utf-8"))
    if size > MAX_ITEM_BYTES:
        raise ValueError("Cosmos item metadata exceeds the bounded item budget")


class ChunkedPayloadStore:
    """Store bounded leaves and reference pages before publishing a root manifest.

    Chunks are workspace-scoped and create-only. Unreferenced chunks from failed
    writes or replaced records are retained: deleting shared content requires a
    separate reachability-based collector, never per-record deletion.
    """

    def __init__(
        self, container: ContainerProxy, *, partition_field: str = "workspaceId"
    ) -> None:
        self._container = container
        self._partition_field = partition_field

    def dump(
        self, payload: Mapping[str, object], workspace_id: str, *, force: bool = False
    ) -> dict[str, Any]:
        """Return an inline payload or bounded manifest, after all chunks are durable."""
        if MANIFEST_KEY in payload:
            # A previously persisted projection already contains a durable manifest.
            ensure_bounded_item(payload)
            return dict(payload)
        raw = json_bytes(payload)
        if (
            not force
            and len(raw) <= INLINE_BYTES
            and len(json.dumps(payload, ensure_ascii=True)) <= INLINE_BYTES
        ):
            return dict(payload)
        references = [
            self._create(
                {
                    "data": base64.b64encode(raw[offset : offset + CHUNK_BYTES]).decode(
                        "ascii"
                    )
                },
                workspace_id,
            )
            for offset in range(0, len(raw), CHUNK_BYTES)
        ]
        while len(references) > 1:
            references = [
                self._create(
                    {"children": references[offset : offset + REFERENCE_FANOUT]},
                    workspace_id,
                )
                for offset in range(0, len(references), REFERENCE_FANOUT)
            ]
        manifest: dict[str, Any] = {
            MANIFEST_KEY: {
                "version": 1,
                "root": references[0],
                "sha256": hashlib.sha256(raw).hexdigest(),
                "bytes": len(raw),
            },
            **{
                key: payload[key]
                for key in _INDEX_FIELDS
                if key in payload
                and (
                    payload[key] is None
                    or isinstance(payload[key], str)
                    and len(cast(str, payload[key])) <= 4096
                )
            },
        }
        data = payload.get("data")
        if (
            isinstance(data, Mapping)
            and isinstance(data.get("subAgentId"), str)
            and len(data["subAgentId"]) <= 4096
        ):
            manifest["data"] = {"subAgentId": data["subAgentId"]}
        ensure_bounded_item(manifest)
        return manifest

    def _create(self, content: dict[str, Any], workspace_id: str) -> str:
        identity = "payload-chunk:" + hashlib.sha256(json_bytes(content)).hexdigest()
        document = {
            "id": identity,
            self._partition_field: workspace_id,
            "recordType": CHUNK_TYPE,
            **content,
        }
        ensure_bounded_item(document)
        try:
            self._container.create_item(document)
        except CosmosResourceExistsError:
            existing = self._container.read_item(
                item=identity, partition_key=workspace_id
            )
            if any(existing.get(key) != value for key, value in document.items()):
                raise ValueError("immutable payload chunk content mismatch") from None
        return identity

    def load(self, payload: Mapping[str, Any], workspace_id: str) -> dict[str, Any]:
        """Hydrate JSON without interpreting provider contents; inline records still work."""
        manifest = payload.get(MANIFEST_KEY)
        if manifest is None:
            return dict(payload)
        if not isinstance(manifest, Mapping) or manifest.get("version") != 1:
            raise ValueError("unsupported Cosmos payload manifest")
        pending = [(manifest["root"], frozenset())]
        parts: list[bytes] = []
        while pending:
            identity, ancestors = pending.pop()
            if identity in ancestors:
                raise ValueError("cyclic Cosmos payload references")
            try:
                chunk = self._container.read_item(
                    item=identity, partition_key=workspace_id
                )
            except CosmosResourceNotFoundError as error:
                raise ValueError(
                    f"missing Cosmos payload chunk {identity!r}"
                ) from error
            content = {key: chunk[key] for key in ("data", "children") if key in chunk}
            if (
                chunk.get("recordType") != CHUNK_TYPE
                or chunk.get(self._partition_field) != workspace_id
                or len(content) != 1
                or identity
                != "payload-chunk:" + hashlib.sha256(json_bytes(content)).hexdigest()
            ):
                raise ValueError("corrupt Cosmos payload chunk")
            if "children" in content:
                children = content["children"]
                if (
                    not isinstance(children, list)
                    or not 1 <= len(children) <= REFERENCE_FANOUT
                ):
                    raise ValueError("invalid Cosmos payload reference page")
                pending.extend(
                    (child, ancestors | {identity}) for child in reversed(children)
                )
            else:
                parts.append(base64.b64decode(content["data"], validate=True))
        raw = b"".join(parts)
        if (
            len(raw) != manifest["bytes"]
            or hashlib.sha256(raw).hexdigest() != manifest["sha256"]
        ):
            raise ValueError("Cosmos payload digest mismatch")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError("Cosmos payload is not an object")
        # Tombstone patches update these indexed fields without rewriting shared chunks.
        for key in ("state", "updated_at"):
            if key in payload:
                result[key] = payload[key]
        return result
