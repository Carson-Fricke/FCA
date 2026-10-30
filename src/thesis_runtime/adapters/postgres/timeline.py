"""The single per-chat log, sequenced atomically with admission and work."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from psycopg import Connection

from thesis_runtime.adapters.receipt_projection import postgres_receipt_header
from thesis_runtime.conversations.streaming import CommitNotifier

from .store import PostgresStore, identity


class PostgresTimeline:
    def __init__(self, store: PostgresStore, notifier: CommitNotifier) -> None:
        self.store = store
        self._notifier = notifier

    @property
    def commits(self) -> CommitNotifier:
        return self._notifier

    def hydrate(self, document: Mapping[str, Any]) -> dict[str, Any]:
        result = dict(document)
        if result.get("kind") == "execution":
            result["payload"] = result["initialPayload"]
        elif result.get("kind") == "event":
            result["payload"] = {**result["payload"], "cursor": result["seq"]}
        return result

    def latest_sequence(self, *, workspace_id: str, chat_id: str) -> int:
        counter = self.store.read(workspace_id, "counter:" + chat_id)
        return int(counter["nextSeq"]) if counter else 0

    def next_sequence(
        self, connection: Connection, workspace_id: str, chat_id: str
    ) -> int:
        counter = self.store.read(
            workspace_id, "counter:" + chat_id, connection=connection
        )
        return 1 if counter is None else int(counter["nextSeq"]) + 1

    def publish(
        self, connection: Connection, document: Mapping[str, Any]
    ) -> dict[str, Any]:
        document = dict(document)
        workspace_id, chat_id = document["workspaceId"], document["chatId"]
        previous = self.store.read(workspace_id, document["id"], connection=connection)
        if previous is not None:
            return previous
        seq = self.next_sequence(connection, workspace_id, chat_id)
        document["seq"] = seq
        self.store.put(
            connection,
            {
                "id": "counter:" + chat_id,
                "workspaceId": workspace_id,
                "chatId": chat_id,
                "nextSeq": seq,
            },
        )
        self.store.put(connection, document, create=True)
        if document.get("runTurn") is True:
            anchor = document.get("conversationId")
            anchor = "" if anchor == chat_id else anchor or ""
            key = "input:" + document["messageId"]
            self.store.put(
                connection,
                {
                    "id": identity("RuntimeWork:", key),
                    "workspaceId": workspace_id,
                    "recordType": "RuntimeWork",
                    "key": key,
                    "resource": f"turn:{chat_id}:{anchor}",
                    "workKind": "conversation",
                    "state": "pending",
                    "createdAt": document["ts"],
                    "payload": {
                        "chat_id": chat_id,
                        "anchor_id": anchor,
                        "input_position": document["inputPosition"],
                    },
                },
                create=True,
            )
        return document

    def append(
        self,
        *,
        chat_id: str,
        workspace_id: str,
        kind: str,
        payload: Mapping[str, object],
        item_id: str,
        execution_id: str | None = None,
        thread_id: str | None = None,
        occurred_at: str | None = None,
    ) -> dict[str, object]:
        if item_id == "counter:" + chat_id:
            raise ValueError("counter is reserved for timeline sequencing")
        document: dict[str, Any] = {
            "id": item_id,
            "workspaceId": workspace_id,
            "chatId": chat_id,
            "kind": kind,
            "payload": dict(payload),
            "ts": occurred_at or datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        }
        if execution_id is not None:
            document["executionId"] = execution_id
        if thread_id is not None:
            document["threadId"] = thread_id
        with self.store.transaction(workspace_id, chat_id=chat_id) as connection:
            committed = self.publish(connection, document)
            if kind == "output" and any(
                committed.get(k) != document.get(k)
                for k in ["payload", "chatId", "threadId"]
            ):
                raise ValueError(
                    "model output identity was reused for different content or scope"
                )
        self._notifier.notify(workspace_id, chat_id)
        return self.hydrate(committed)

    def read_after(
        self,
        *,
        workspace_id: str,
        chat_id: str,
        after_seq: int = 0,
        limit: int | None = None,
        thread_id: str | None = None,
    ) -> list[dict[str, object]]:
        return self._read(workspace_id, chat_id, after_seq, limit, thread_id, False)

    def read_before(
        self,
        *,
        workspace_id: str,
        chat_id: str,
        before_seq: int,
        limit: int,
        thread_id: str | None = None,
    ) -> list[dict[str, object]]:
        return self._read(workspace_id, chat_id, before_seq, limit, thread_id, True)

    def _read(
        self,
        workspace_id: str,
        chat_id: str,
        cursor: int,
        limit: int | None,
        thread_id: str | None,
        before: bool,
    ) -> list[dict[str, object]]:
        predicate = (
            "workspace_id=%s AND chat_id=%s AND seq " + ("<" if before else ">") + " %s"
        )
        args: list[object] = [workspace_id, chat_id, cursor]
        if before:
            # Closed fragments remain audit evidence in storage. History uses
            # the canonical message instead of transferring its provisional text.
            predicate += " AND (kind IS DISTINCT FROM 'output' OR NOT EXISTS (SELECT 1 FROM thesis_documents message WHERE message.collection=thesis_documents.collection AND message.workspace_id=thesis_documents.workspace_id AND message.chat_id=thesis_documents.chat_id AND message.id='ChatMessage:' || (thesis_documents.body#>>'{payload,messageId}')))"
        if thread_id is not None:
            predicate += " AND thread_id=%s"
            args.append(thread_id)
        rows = self.store.query(
            predicate, args, order="seq " + ("DESC" if before else "ASC"), limit=limit
        )
        if before:
            rows.reverse()
        return [self.hydrate(row) for row in rows]

    def output_prefixes(
        self, *, workspace_id: str, chat_id: str, items: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        prefixes: list[dict[str, object]] = []
        starts: dict[str, int] = {}
        for item in items:
            if item.get("kind") == "output":
                payload = item["payload"]
                assert isinstance(payload, dict)
                key = str(payload["messageId"])
                seq = item["seq"]
                assert isinstance(seq, int)
                starts[key] = min(starts.get(key, seq), seq)
        for message_id, first in starts.items():
            rows = self.store.query(
                "workspace_id=%s AND chat_id=%s AND kind='output' AND body#>>'{payload,messageId}'=%s AND seq<%s",
                [workspace_id, chat_id, message_id, first],
                order="seq",
            )
            prefixes.extend(self.hydrate(row) for row in rows)
        return prefixes

    def read_receipts(
        self,
        *,
        workspace_id: str,
        chat_id: str,
        after_seq: int,
        through_seq: int,
        limit: int,
        result_types: tuple[str, ...],
        structured_kinds: tuple[str, ...],
        metadata_fields: tuple[str, ...],
        metadata_arrays: tuple[tuple[str, str], ...],
    ) -> list[dict[str, object]]:
        """Page selected canonical receipts rather than streaming fragments."""
        rows = self.store.query(
            "workspace_id=%s AND chat_id=%s AND kind='message' AND seq>%s AND seq<=%s "
            "AND ((thread_id='root' AND (body#>>'{payload,role}'='user' OR "
            "jsonb_array_length(COALESCE(body#>'{payload,data,sessionMessage,content,requests}','[]'))>0)) "
            "OR jsonb_path_query_array(body,'$.payload.data.sessionMessage.content.result.**.datumType') ?| %s "
            "OR jsonb_path_query_array(body,'$.payload.data.sessionMessage.content.result.**.structured.kind') ?| %s)",
            [
                workspace_id,
                chat_id,
                after_seq,
                through_seq,
                list(result_types),
                list(structured_kinds),
            ],
            order="seq",
            limit=limit,
            columns=postgres_receipt_header(metadata_fields, metadata_arrays),
        )
        return [row[0] for row in rows]
